import datetime


def generate_profiles(
    optimal_time_ranges: dict,
    optimal_days: dict,
    ml_metrics: dict,
    total_records: int,
    data_range: tuple | None = None,
    distance_feature_used: str = "distance_group",
    distance_importances: dict | None = None,
    ml_discovered_patterns: list | None = None,
) -> dict:
    """Combine all analyses into the final profile rules JSON.

    Args:
        optimal_time_ranges: Output from extract_optimal_time_ranges().
        optimal_days: Output from extract_optimal_days().
        ml_metrics: Combined metrics from DT + RF models.
        total_records: Total records used in analysis.
        data_range: (min_date, max_date) tuple.
        distance_feature_used: Which distance feature the ML found more predictive.
        distance_importances: Feature importance comparison for same_city vs distance_group.
        ml_discovered_patterns: List of pattern descriptions from ML.
    """
    all_days = ["monday", "tuesday", "wednesday", "thursday", "friday"]

    # Build profiles from optimal time ranges
    profiles = {}
    for (age_group, distance_group), windows in optimal_time_ranges.items():
        profile_key = f"{age_group}_{distance_group}"
        days = optimal_days.get((age_group, distance_group), all_days)

        recommendations = []
        for w in windows:
            rec = {
                "days": days,
                "start_hour": min(w["hours"]),
                "end_hour": max(w["hours"]),
                "score": w["rate"],
                "lift": w["lift"],
                "sample_size": w["n"],
                "rank": w["rank"],
            }
            # Pass through tier, CI, and low_sample fields when present
            for field in ("tier", "tier_ci", "ci_lower", "ci_upper", "attended", "low_sample"):
                if field in w:
                    rec[field] = w[field]
            recommendations.append(rec)

        profiles[profile_key] = {
            "age_group": _age_group_label(age_group),
            "distance_group": distance_group,
            "recommendations": recommendations,
        }

    # Metadata
    metadata = {
        "trained_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "data_range": (
            [str(data_range[0]), str(data_range[1])] if data_range else None
        ),
        "total_records_used": total_records,
        "model_accuracy": ml_metrics,
        "distance_feature_used": distance_feature_used,
        "distance_feature_importance": distance_importances or {},
        "ml_discovered_patterns": ml_discovered_patterns or [],
    }

    result = {
        "profiles": profiles,
        "metadata": metadata,
    }

    print(f"Generated {len(profiles)} profiles")
    return result


def _age_group_label(age_group: str) -> str:
    """Map internal age group name to display label."""
    return {"toddler": "0-6", "school_age": "7-18", "adult": "18+"}.get(age_group, age_group)
