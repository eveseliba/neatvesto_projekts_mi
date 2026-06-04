import json
import datetime


# Manual research baseline patterns for comparison
MANUAL_PATTERNS = {
    "toddler_optimal": {"age_group": "0-6", "hours": "8-12", "note": "Best in morning"},
    "school_age_local": {"age_group": "7-18", "distance": "same_city", "hours": "12-15"},
    "school_age_remote": {"age_group": "7-18", "distance": "not same_city", "hours": "15-17"},
    "adult_morning": {"age_group": "18+", "hours": "8-10", "note": "First peak"},
    "adult_afternoon": {"age_group": "18+", "hours": "15-17", "note": "Second peak"},
}


def generate_profiling_report(
    profiles: dict,
    dt_metrics: dict,
    rf_metrics: dict,
    shap_importances: dict | None,
    attendance_rates_summary: dict | None = None,
) -> dict:
    """Generate a human-readable validation report comparing ML with manual research.

    Args:
        profiles: Full profile rules dict (output of generate_profiles).
        dt_metrics: Decision Tree model metrics.
        rf_metrics: Random Forest model metrics.
        shap_importances: SHAP analysis results or None.
        attendance_rates_summary: Optional summary stats from attendance rates.

    Returns:
        Report dict (also suitable for JSON serialisation).
    """
    report = {
        "generated_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "model_metrics": {
            "decision_tree": dt_metrics,
            "random_forest": rf_metrics,
        },
        "shap_analysis": shap_importances,
        "ml_vs_manual": _compare_with_manual(profiles),
        "distance_feature_comparison": _distance_comparison(profiles, rf_metrics, shap_importances),
        "service_id_feature_analysis": _service_analysis(rf_metrics, shap_importances),
        "profile_summary": _profile_summary(profiles),
    }

    return report


def _compare_with_manual(profiles: dict) -> list:
    """Compare ML-discovered patterns with manual research baselines."""
    comparisons = []
    ml_profiles = profiles.get("profiles", {})

    for name, manual in MANUAL_PATTERNS.items():
        match_found = False
        ml_match = None

        for pkey, pval in ml_profiles.items():
            if manual["age_group"] == pval.get("age_group") and pval.get("recommendations"):
                recs = pval["recommendations"]
                for rec in recs:
                    ml_range = f"{rec['start_hour']}-{rec['end_hour']}"
                    comparisons.append({
                        "pattern": name,
                        "manual_expectation": manual,
                        "ml_profile_key": pkey,
                        "ml_hours": ml_range,
                        "ml_score": rec.get("score"),
                        "ml_lift": rec.get("lift"),
                        "ml_rank": rec.get("rank"),
                    })
                    match_found = True

        if not match_found:
            comparisons.append({
                "pattern": name,
                "manual_expectation": manual,
                "ml_profile_key": None,
                "ml_hours": None,
                "note": "No ML match found for this manual pattern",
            })

    return comparisons


def _distance_comparison(profiles: dict, rf_metrics: dict, shap_importances: dict | None) -> dict:
    """Compare same_city vs distance_group predictive power."""
    rf_fi = rf_metrics.get("feature_importances", {})
    same_city_imp = rf_fi.get("same_city", 0)
    distance_group_imp = rf_fi.get("distance_group", 0)

    shap_comparison = {}
    if shap_importances and "mean_abs_shap" in shap_importances:
        shap_vals = shap_importances["mean_abs_shap"]
        shap_comparison = {
            "same_city_shap": shap_vals.get("same_city", 0),
            "distance_group_shap": shap_vals.get("distance_group", 0),
        }

    return {
        "rf_feature_importance": {
            "same_city": same_city_imp,
            "distance_group": distance_group_imp,
        },
        "shap_comparison": shap_comparison,
        "more_predictive": (
            "distance_group" if distance_group_imp > same_city_imp else "same_city"
        ),
    }


def _service_analysis(rf_metrics: dict, shap_importances: dict | None) -> dict:
    """Assess whether pakalpojuma_id (service_id) adds signal beyond specialitates_id."""
    rf_fi = rf_metrics.get("feature_importances", {})
    spec_imp = rf_fi.get("specialitates_id", 0)
    svc_imp = rf_fi.get("service_id", 0)

    return {
        "specialitates_id_importance": spec_imp,
        "service_id_importance": svc_imp,
        "service_id_adds_signal": svc_imp > 0.02,
        "note": (
            "Service ID adds meaningful signal beyond specialty"
            if svc_imp > 0.02
            else "Service ID provides minimal additional signal"
        ),
    }


def _profile_summary(profiles: dict) -> list:
    """Summarise all generated profiles."""
    summaries = []
    for pkey, pval in profiles.get("profiles", {}).items():
        recs = pval.get("recommendations", [])
        summaries.append({
            "profile": pkey,
            "age_group": pval.get("age_group"),
            "distance_group": pval.get("distance_group"),
            "num_windows": len(recs),
            "top_window": (
                f"{recs[0]['start_hour']}-{recs[0]['end_hour']} (score={recs[0]['score']})"
                if recs
                else None
            ),
        })
    return summaries
