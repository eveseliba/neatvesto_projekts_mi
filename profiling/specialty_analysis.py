import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta


def extract_specialty_working_hours(
    df: pd.DataFrame,
    recency_months: int = 12,
) -> dict:
    """Extract working hours and days for each specialitates_id from recent data.

    Args:
        df: DataFrame with columns specialitates_id, patient_id,
            appointment_hour, day_of_week, appointment_date.
        recency_months: Only use data from the last N months.

    Returns:
        Dict keyed by specialitates_id with working hours details.
    """
    # Filter to recent data
    cutoff = df["appointment_date"].max() - pd.DateOffset(months=recency_months)
    recent = df[df["appointment_date"] >= cutoff].copy()

    results = {}
    for spec_id, group in recent.groupby("specialitates_id"):
        spec_id = int(spec_id)
        working_hours = [int(group["appointment_hour"].min()), int(group["appointment_hour"].max())]
        working_days = sorted(group["day_of_week"].unique().tolist())
        patient_count = group["patient_id"].nunique()

        results[spec_id] = {
            "working_hours": working_hours,
            "working_days": working_days,
            "patient_count": patient_count,
        }

    print(f"Extracted working hours for {len(results)} specialties")
    return results
