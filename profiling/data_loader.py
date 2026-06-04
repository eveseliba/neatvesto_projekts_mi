import pandas as pd


REQUIRED_COLUMNS = [
    "appointment_attended",
    "patient_id",
    "patient_birth_year",
    "patient_address",
    "facility_name",
    "distance_to_appointment_km",
    "specialitates_id",
    "service_id",
    "specialty_name",
    "appointment_hour",
    "appointment_date",
    "day_of_week",
]


def load_profiling_data(csv_path: str) -> pd.DataFrame:
    """Load profiling CSV and validate required columns."""
    df = pd.read_csv(csv_path)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Type conversions
    df["appointment_attended"] = df["appointment_attended"].astype(int)
    df["patient_birth_year"] = pd.to_numeric(df["patient_birth_year"], errors="coerce")
    df["distance_to_appointment_km"] = pd.to_numeric(
        df["distance_to_appointment_km"], errors="coerce"
    )
    df["specialitates_id"] = pd.to_numeric(df["specialitates_id"], errors="coerce")
    df["appointment_hour"] = df["appointment_hour"].astype(int)
    df["day_of_week"] = df["day_of_week"].astype(int)
    df["appointment_date"] = pd.to_datetime(df["appointment_date"], errors="coerce")

    # Drop rows with null attendance target
    df = df.dropna(subset=["appointment_attended"])

    print(f"Loaded profiling data: {len(df)} rows, {len(df.columns)} columns")
    return df
