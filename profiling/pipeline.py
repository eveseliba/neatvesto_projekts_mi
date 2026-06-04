import os
import json
import pickle

import pandas as pd

from .data_loader import load_profiling_data
from shared.distance_enrichment import enrich_distances, geocode_unknown_addresses
from .feature_engineering import engineer_profiling_features
from .pattern_discovery import (
    compute_attendance_rates,
    train_pattern_model,
    train_ensemble_model,
    extract_optimal_time_ranges,
    extract_optimal_days,
    extract_day_time_interactions,
)
from .profile_generator import generate_profiles
from .report_generator import generate_profiling_report


class ProfilingPipeline:
    """Orchestrates the full profiling pipeline in discrete phases."""

    def __init__(self, csv_path: str, model_dir: str, reports_dir: str):
        self.csv_path = csv_path
        self.model_dir = model_dir
        self.reports_dir = reports_dir
        os.makedirs(reports_dir, exist_ok=True)

    def run(self) -> dict:
        """Run the full pipeline and return generated profiles."""
        df = self._prepare_data()
        patterns = self._discover_patterns(df)
        profiles, report = self._generate_profiles(df, patterns)
        self._save_artifacts(profiles, patterns, report)
        return profiles

    def _prepare_data(self) -> pd.DataFrame:
        """Phase 1: Load, geocode, enrich distances, engineer features, filter."""
        print("=" * 60)
        print("PROFILING PIPELINE - Phase 1: Data Preparation")
        print("=" * 60)

        df = load_profiling_data(self.csv_path)
        geocode_unknown_addresses(df)
        df = enrich_distances(df)
        df = engineer_profiling_features(df)

        # Drop rows where key features are missing
        required = ["age_group", "appointment_hour", "day_of_week"]
        before = len(df)
        df = df.dropna(subset=required)
        df = df[df["age_group"] != "unknown"]

        # Keep only weekdays (1=Mon..5=Fri) and working hours (8-17)
        df = df[df["day_of_week"].between(1, 5)]
        df = df[df["appointment_hour"].between(8, 17)]
        print(f"After filtering: {len(df)} rows (dropped {before - len(df)})")

        return df

    def _discover_patterns(self, df: pd.DataFrame) -> dict:
        """Phase 2: ML pattern discovery — attendance rates, DT, RF, SHAP, day x time."""
        print()
        print("=" * 60)
        print("PROFILING PIPELINE - Phase 2: ML Pattern Discovery")
        print("=" * 60)

        print("\n-- 2.1 Attendance Rate Analysis --")
        attendance_rates = compute_attendance_rates(df)

        print("\n-- 2.2 Decision Tree Model --")
        dt_model, dt_metrics, dt_rules = train_pattern_model(df)

        print("\n-- 2.3 Random Forest + SHAP --")
        rf_model, rf_metrics, shap_importances = train_ensemble_model(df)

        print("\n-- 2.4 Optimal Time Ranges --")
        optimal_ranges = extract_optimal_time_ranges(attendance_rates)

        print("\n-- 2.5 Day-of-Week Analysis --")
        optimal_days = extract_optimal_days(attendance_rates)

        print("\n-- 2.6 Day × Time Interaction Analysis --")
        day_time_interactions = extract_day_time_interactions(attendance_rates)

        # Determine which distance feature is more predictive
        rf_fi = rf_metrics.get("feature_importances", {})
        same_city_imp = rf_fi.get("same_city", 0)
        distance_group_imp = rf_fi.get("distance_group", 0)
        distance_feature = "distance_group" if distance_group_imp > same_city_imp else "same_city"

        # Collect ML-discovered patterns from DT rules
        ml_patterns = []
        if dt_rules:
            for line in dt_rules.split("\n"):
                line = line.strip()
                if "class:" in line.lower() or "<=>" in line:
                    ml_patterns.append(line)

        return {
            "attendance_rates": attendance_rates,
            "dt_model": dt_model,
            "dt_metrics": dt_metrics,
            "dt_rules": dt_rules,
            "rf_model": rf_model,
            "rf_metrics": rf_metrics,
            "shap_importances": shap_importances,
            "optimal_ranges": optimal_ranges,
            "optimal_days": optimal_days,
            "day_time_interactions": day_time_interactions,
            "distance_feature": distance_feature,
            "distance_importances": {"same_city": same_city_imp, "distance_group": distance_group_imp},
            "ml_patterns": ml_patterns[:20],
        }

    def _generate_profiles(self, df: pd.DataFrame, patterns: dict) -> tuple[dict, dict]:
        """Phase 3: Combine ML results into profiles and generate validation report."""
        print()
        print("=" * 60)
        print("PROFILING PIPELINE - Profile Generation")
        print("=" * 60)

        combined_metrics = {
            "decision_tree": {
                "cv_auc": patterns["dt_metrics"]["cv_auc_mean"],
                "train_accuracy": patterns["dt_metrics"]["train_accuracy"],
            },
            "random_forest": {
                "cv_auc": patterns["rf_metrics"]["cv_auc_mean"],
                "train_accuracy": patterns["rf_metrics"]["train_accuracy"],
            },
        }

        total_records = len(df)
        data_range = (df["appointment_date"].min(), df["appointment_date"].max())

        profiles = generate_profiles(
            optimal_time_ranges=patterns["optimal_ranges"],
            optimal_days=patterns["optimal_days"],
            ml_metrics=combined_metrics,
            total_records=total_records,
            data_range=data_range,
            distance_feature_used=patterns["distance_feature"],
            distance_importances=patterns["distance_importances"],
            ml_discovered_patterns=patterns["ml_patterns"],
        )

        print("\n-- Generating Report --")
        report = generate_profiling_report(
            profiles=profiles,
            dt_metrics=patterns["dt_metrics"],
            rf_metrics=patterns["rf_metrics"],
            shap_importances=patterns["shap_importances"],
        )

        return profiles, report

    def _save_artifacts(self, profiles: dict, patterns: dict, report: dict):
        """Phase 4: Save all artifacts to disk."""
        print("\n-- Saving Artefacts --")

        # Profile rules JSON (main lookup for API)
        rules_path = os.path.join(self.model_dir, "profiling_rules.json")
        _save_json(rules_path, profiles)

        # Metadata
        meta_path = os.path.join(self.model_dir, "profiling_metadata.json")
        _save_json(meta_path, profiles.get("metadata", {}))

        # Decision Tree model
        dt_path = os.path.join(self.model_dir, "profiling_decision_tree.pkl")
        _save_pickle(dt_path, patterns["dt_model"])

        # Random Forest model
        rf_path = os.path.join(self.model_dir, "profiling_random_forest.pkl")
        _save_pickle(rf_path, patterns["rf_model"])

        # Report
        report_path = os.path.join(self.reports_dir, "profiling_report.json")
        _save_json(report_path, report)

        # Day × Time interactions
        dti_path = os.path.join(self.model_dir, "day_time_interactions.json")
        dti_serialisable = {
            f"{ag}_{dg}": cells
            for (ag, dg), cells in patterns["day_time_interactions"].items()
        }
        _save_json(dti_path, dti_serialisable)

        # DT rules text
        rules_txt_path = os.path.join(self.reports_dir, "decision_tree_rules.txt")
        with open(rules_txt_path, "w", encoding="utf-8") as f:
            f.write(patterns["dt_rules"])

        print(f"\nArtefacts saved to: {self.model_dir}")
        print(f"Report saved to: {report_path}")
        print("PROFILING PIPELINE COMPLETE")


def run_profiling_pipeline(
    csv_path: str | None = None,
    model_dir: str | None = None,
    reports_dir: str | None = None,
) -> dict:
    """Run the full profiling pipeline end-to-end.

    Args:
        csv_path: Path to profiling CSV. Defaults to MODEL_DIR/profiling_input.csv.
        model_dir: Directory to save model artefacts. Defaults to MODEL_DIR env var.
        reports_dir: Directory to save reports. Defaults to model_dir/../reports.

    Returns:
        The generated profiles dict.
    """
    model_dir = model_dir or os.getenv("MODEL_DIR", "/app")
    csv_path = csv_path or os.path.join(model_dir, "profiling_input.csv")
    reports_dir = reports_dir or os.path.join(model_dir, "reports")

    pipeline = ProfilingPipeline(csv_path, model_dir, reports_dir)
    return pipeline.run()


def _save_json(path: str, obj):
    """Write JSON file with serialisation of non-standard types."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str, ensure_ascii=False)
    print(f"  Saved: {path}")


def _save_pickle(path: str, obj):
    with open(path, "wb") as f:
        pickle.dump(obj, f)
    print(f"  Saved: {path}")
