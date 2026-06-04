import math

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.preprocessing import LabelEncoder


# Maximum within-tier rate spread before forcing a new tier.
MAX_TIER_SPREAD = 0.05


# ---------------------------------------------------------------------------
# Tier Assignment Helpers
# ---------------------------------------------------------------------------

def _wilson_ci(attended: int, total: int) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a binomial proportion.

    Returns (ci_lower, ci_upper).  Returns (0.0, 0.0) when *total* is 0.
    """
    if total == 0:
        return 0.0, 0.0
    z = 1.96
    p = attended / total
    n = total
    denom = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * n)) / n) / denom
    ci_lower = max(0.0, centre - margin)
    ci_upper = min(1.0, centre + margin)
    return ci_lower, ci_upper


def assign_tiers_ci_overlap(
    windows: list[dict],
    max_spread: float = MAX_TIER_SPREAD,
) -> list[dict]:
    """Assign ``tier_ci`` based on confidence-interval overlap.

    Windows are sorted by *rate* descending.  A window joins the current tier
    if its CI overlaps with the previous window's CI **and** adding it would
    not cause the within-tier rate spread to exceed *max_spread*.

    Mutates *windows* in-place (adds ``tier_ci`` key) and returns them.
    """
    if not windows:
        return windows

    windows.sort(key=lambda w: -w["rate"])
    tier = 1
    tier_top_rate = windows[0]["rate"]
    windows[0]["tier_ci"] = tier
    for i in range(1, len(windows)):
        prev = windows[i - 1]
        curr = windows[i]
        overlap = (prev["ci_lower"] <= curr["ci_upper"]) and (curr["ci_lower"] <= prev["ci_upper"])
        spread_ok = (tier_top_rate - curr["rate"]) <= max_spread
        if overlap and spread_ok:
            curr["tier_ci"] = tier
        else:
            tier += 1
            tier_top_rate = curr["rate"]
            curr["tier_ci"] = tier
    return windows


# ---------------------------------------------------------------------------
# Step A: Attendance Rate Analysis by Segment
# ---------------------------------------------------------------------------

def compute_attendance_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Compute attendance rate for every combination of segment dimensions.

    Groups by (age_group, same_city, distance_group, hour, day_of_week) and
    calculates attendance rate with Wilson-score 95 % confidence intervals.
    """
    group_cols = ["age_group", "same_city", "distance_group", "appointment_hour", "day_of_week"]
    agg = (
        df.groupby(group_cols)["appointment_attended"]
        .agg(["sum", "count"])
        .rename(columns={"sum": "attended", "count": "total"})
        .reset_index()
    )
    agg["attendance_rate"] = agg["attended"] / agg["total"]

    # Wilson score 95 % CI
    z = 1.96
    n = agg["total"]
    p = agg["attendance_rate"]
    denom = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / denom
    margin = z * np.sqrt((p * (1 - p) + z ** 2 / (4 * n)) / n) / denom
    agg["ci_lower"] = (centre - margin).clip(lower=0)
    agg["ci_upper"] = (centre + margin).clip(upper=1)
    agg["sample_size"] = agg["total"]

    print(f"Computed attendance rates for {len(agg)} segment combinations")
    return agg


# ---------------------------------------------------------------------------
# Step B: Decision Tree Model
# ---------------------------------------------------------------------------

def train_pattern_model(df: pd.DataFrame, max_depth: int = 7) -> tuple:
    """Train an interpretable Decision Tree on attendance.

    Returns (model, metrics_dict, extracted_rules_text).
    """
    feature_cols = ["age_group", "same_city", "distance_group",
                    "appointment_hour", "day_of_week", "specialitates_id"]
    X, encoders = _encode_features(df, feature_cols)
    y = df["appointment_attended"].values

    model = DecisionTreeClassifier(
        max_depth=max_depth,
        min_samples_leaf=50,
        class_weight="balanced",
        random_state=42,
    )

    # 5-fold stratified CV
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")

    model.fit(X, y)

    rules_text = export_text(model, feature_names=list(X.columns), max_depth=10)

    metrics = {
        "cv_auc_mean": round(float(scores.mean()), 4),
        "cv_auc_std": round(float(scores.std()), 4),
        "train_accuracy": round(float(model.score(X, y)), 4),
        "feature_importances": dict(zip(X.columns, [round(float(v), 4) for v in model.feature_importances_])),
    }
    print(f"Decision Tree - CV AUC: {metrics['cv_auc_mean']:.4f} +/- {metrics['cv_auc_std']:.4f}")
    return model, metrics, rules_text


# ---------------------------------------------------------------------------
# Step C: Random Forest + Feature Importance
# ---------------------------------------------------------------------------

def train_ensemble_model(df: pd.DataFrame) -> tuple:
    """Train a Random Forest for accuracy + feature importance.

    Returns (model, metrics_dict, shap_importances | None).
    """
    feature_cols = ["age_group", "same_city", "distance_group",
                    "appointment_hour", "day_of_week",
                    "specialitates_id", "service_id"]
    X, encoders = _encode_features(df, feature_cols)
    y = df["appointment_attended"].values

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=10,
        min_samples_leaf=30,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")

    model.fit(X, y)

    metrics = {
        "cv_auc_mean": round(float(scores.mean()), 4),
        "cv_auc_std": round(float(scores.std()), 4),
        "train_accuracy": round(float(model.score(X, y)), 4),
        "feature_importances": dict(zip(X.columns, [round(float(v), 4) for v in model.feature_importances_])),
    }
    print(f"Random Forest - CV AUC: {metrics['cv_auc_mean']:.4f} +/- {metrics['cv_auc_std']:.4f}")

    # SHAP analysis (optional - import only if available)
    shap_importances = _compute_shap(model, X)

    return model, metrics, shap_importances


def _compute_shap(model, X: pd.DataFrame, sample_size: int = 5000) -> dict | None:
    """Compute SHAP values on a subsample. Returns dict or None if shap unavailable."""
    try:
        import shap
    except ImportError:
        print("shap not installed - skipping SHAP analysis")
        return None

    sample = X.sample(n=min(sample_size, len(X)), random_state=42)
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(sample)

    # For binary classification shap_values is a list [class0, class1]
    if isinstance(shap_values, list):
        vals = np.abs(shap_values[1]).mean(axis=0)
    else:
        vals = np.abs(shap_values).mean(axis=0)

    importance = dict(zip(X.columns, [round(float(v), 4) for v in vals]))
    print(f"SHAP feature importances: {importance}")

    # Interaction values (top pairs)
    interaction_importance = {}
    try:
        shap_interaction = explainer.shap_interaction_values(sample)
        if isinstance(shap_interaction, list):
            inter = np.abs(shap_interaction[1]).mean(axis=0)
        else:
            inter = np.abs(shap_interaction).mean(axis=0)
        cols = list(X.columns)
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                pair_key = f"{cols[i]} × {cols[j]}"
                interaction_importance[pair_key] = round(float(inter[i, j]), 4)
        # Sort by importance
        interaction_importance = dict(sorted(interaction_importance.items(), key=lambda x: -x[1]))
    except Exception:
        pass

    return {"mean_abs_shap": importance, "interactions": interaction_importance}


# ---------------------------------------------------------------------------
# Step D: Optimal Time Range Extraction
# ---------------------------------------------------------------------------

def extract_optimal_time_ranges(
    attendance_rates: pd.DataFrame,
    min_samples_per_hour: int = 30,
    min_window_hours: int = 2,
    min_lift_above_baseline: float = 0.05,
    significance_threshold: float = 0.05,
    min_volume_share_pct: float = 4.0,
) -> dict:
    """Find optimal scheduling windows using 5 fixed clinical time slots.

    Fixed windows reflecting clinical scheduling patterns:
      - Morning:         8-9   (early morning block)
      - Late morning:    10-11 (mid-morning block)
      - Early afternoon: 12-13 (post-lunch block)
      - Mid afternoon:   14-15 (after-school block)
      - Late afternoon:  16-17 (end-of-day block)

    For each segment (age_group x distance_group) the function computes the
    volume-weighted attendance rate per window, filters out windows with too
    few samples, and ranks the remaining windows by attendance rate.
    """
    TIME_WINDOWS = [
        ("8-9",   list(range(8, 10))),    # hours 8, 9
        ("10-11", list(range(10, 12))),   # hours 10, 11
        ("12-13", list(range(12, 14))),   # hours 12, 13
        ("14-15", list(range(14, 16))),   # hours 14, 15
        ("16-17", list(range(16, 18))),   # hours 16, 17
    ]
    results = {}

    for (ag, dg), seg in attendance_rates.groupby(["age_group", "distance_group"]):
        hourly = (
            seg.groupby("appointment_hour")
            .agg(attended=("attended", "sum"), total=("total", "sum"))
            .reset_index()
        )
        hourly["rate"] = hourly["attended"] / hourly["total"]

        if hourly.empty:
            continue

        total_attended = hourly["attended"].sum()
        total_all = hourly["total"].sum()
        baseline_rate = total_attended / total_all if total_all > 0 else 0

        windows = []
        for label, hours in TIME_WINDOWS:
            wh = hourly[hourly["appointment_hour"].isin(hours)]
            if wh.empty:
                total_n = 0
                attended_n = 0
            else:
                total_n = int(wh["total"].sum())
                attended_n = int(wh["attended"].sum())
            vol_rate = attended_n / total_n if total_n > 0 else 0.0
            lift = vol_rate - baseline_rate
            low_sample = total_n < min_samples_per_hour * len(hours)
            ci_lo, ci_hi = _wilson_ci(attended_n, total_n)
            windows.append({
                "hours": hours,
                "rate": round(float(vol_rate), 4),
                "lift": round(float(lift), 4),
                "n": total_n,
                "attended": attended_n,
                "low_sample": low_sample,
                "ci_lower": round(float(ci_lo), 4),
                "ci_upper": round(float(ci_hi), 4),
            })

        # Rank by volume-weighted attendance rate; low-sample windows last
        windows.sort(key=lambda w: (w["low_sample"], -w["rate"]))
        for rank, w in enumerate(windows, 1):
            w["rank"] = rank

        # Assign tiers
        assign_tiers_ci_overlap(windows)
        for w in windows:
            w["tier"] = w["tier_ci"]

        results[(ag, dg)] = windows

    print(f"Extracted optimal time ranges for {len(results)} segments")
    return results


# ---------------------------------------------------------------------------
# Step E: Day-of-Week Analysis
# ---------------------------------------------------------------------------

def extract_optimal_days(
    attendance_rates: pd.DataFrame,
    min_samples_per_day: int = 30,
    min_lift_above_baseline: float = 0.03,
) -> dict:
    """Determine if certain weekdays are significantly better for each segment."""
    day_names = {1: "monday", 2: "tuesday", 3: "wednesday", 4: "thursday", 5: "friday"}
    all_days = list(day_names.values())
    results = {}

    for (ag, dg), seg in attendance_rates.groupby(["age_group", "distance_group"]):
        daily = (
            seg.groupby("day_of_week")
            .agg(attended=("attended", "sum"), total=("total", "sum"))
            .reset_index()
        )
        daily["rate"] = daily["attended"] / daily["total"]
        daily = daily[daily["total"] >= min_samples_per_day]

        if daily.empty:
            results[(ag, dg)] = all_days
            continue

        baseline = daily["attended"].sum() / daily["total"].sum()
        good_days = []
        for _, row in daily.iterrows():
            lift = row["rate"] - baseline
            if lift >= min_lift_above_baseline:
                good_days.append(day_names.get(int(row["day_of_week"]), str(int(row["day_of_week"]))))

        # If no days are significantly better, all days are equal
        results[(ag, dg)] = good_days if good_days else all_days

    return results


# ---------------------------------------------------------------------------
# Step F: Day × Time Interaction Analysis
# ---------------------------------------------------------------------------

def extract_day_time_interactions(
    attendance_rates: pd.DataFrame,
    min_samples: int = 30,
) -> dict:
    """Compute attendance rate for each of 25 day×window combinations per segment.

    Aggregates away the same_city dimension and computes Wilson CI,
    composite score, and sample size for each (day_of_week, time_window) cell.
    Assigns tier rankings to the cells.

    Segments with ``distance_group == "unknown"`` are excluded.

    Returns dict keyed by (age_group, distance_group) → list of 25 dicts
    sorted by rate descending with tier/rank fields.
    """
    TIME_WINDOWS = [
        ("8-9",   list(range(8, 10))),
        ("10-11", list(range(10, 12))),
        ("12-13", list(range(12, 14))),
        ("14-15", list(range(14, 16))),
        ("16-17", list(range(16, 18))),
    ]
    DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri"}

    results = {}

    for (ag, dg), seg in attendance_rates.groupby(["age_group", "distance_group"]):
        if dg == "unknown":
            continue

        # Aggregate away same_city: sum attended/total across same_city values
        hourly_day = (
            seg.groupby(["day_of_week", "appointment_hour"])
            .agg(attended=("attended", "sum"), total=("total", "sum"))
            .reset_index()
        )

        # Baseline for this segment
        total_attended = hourly_day["attended"].sum()
        total_all = hourly_day["total"].sum()
        baseline_rate = total_attended / total_all if total_all > 0 else 0

        cells = []
        for day_num in sorted(DAY_NAMES.keys()):
            for window_label, hours in TIME_WINDOWS:
                mask = (hourly_day["day_of_week"] == day_num) & (
                    hourly_day["appointment_hour"].isin(hours)
                )
                subset = hourly_day[mask]

                n = int(subset["total"].sum()) if not subset.empty else 0
                attended = int(subset["attended"].sum()) if not subset.empty else 0
                rate = attended / n if n > 0 else 0.0

                ci_lower, ci_upper = _wilson_ci(attended, n)

                # Composite score: rate × log(n+1)
                composite = rate * np.log1p(n)
                low_sample = n < min_samples

                cells.append({
                    "day_of_week": day_num,
                    "day_name": DAY_NAMES[day_num],
                    "window": window_label,
                    "hours": hours,
                    "rate": round(float(rate), 4),
                    "ci_lower": round(float(ci_lower), 4),
                    "ci_upper": round(float(ci_upper), 4),
                    "composite_score": round(float(composite), 4),
                    "n": n,
                    "attended": attended,
                    "low_sample": low_sample,
                    "lift": round(float(rate - baseline_rate), 4),
                })

        # Assign tiers — sorts by rate descending
        assign_tiers_ci_overlap(cells)
        # Re-sort so low-sample cells come last, then by rate descending
        cells.sort(key=lambda c: (c["low_sample"], -c["rate"]))
        for i, c in enumerate(cells):
            c["rank"] = i + 1
            c["tier"] = c["tier_ci"]

        results[(ag, dg)] = cells

    print(f"Extracted day×time interactions for {len(results)} segments "
          f"({len(results) * 25} cells)")
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_features(df: pd.DataFrame, feature_cols: list) -> tuple:
    """Label-encode categorical columns and return (X DataFrame, encoders dict)."""
    X = df[feature_cols].copy()
    encoders = {}
    for col in X.columns:
        if X[col].dtype == object or X[col].dtype.name == "category":
            le = LabelEncoder()
            X[col] = le.fit_transform(X[col].astype(str))
            encoders[col] = le
        elif X[col].dtype == bool:
            X[col] = X[col].astype(int)
    X = X.fillna(-1)
    return X, encoders
