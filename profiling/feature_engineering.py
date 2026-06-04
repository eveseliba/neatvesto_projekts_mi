import re
import pandas as pd
import numpy as np


def engineer_profiling_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add all profiling-specific features."""
    df = df.copy()
    df = _add_patient_age(df)
    df = _add_age_group(df)
    df = _add_same_city(df)
    df = _add_distance_group(df)
    return df


# ---------------------------------------------------------------------------
# Public utility functions (used by both pipeline and API)
# ---------------------------------------------------------------------------

def get_age_group(age: int | float) -> str:
    """Return age group label for a single patient age."""
    if age is None or np.isnan(age):
        return "unknown"
    if age <= 6:
        return "toddler"
    if age <= 18:
        return "school_age"
    return "adult"


AGE_GROUP_LABELS = {"toddler": "0-6", "school_age": "7-18", "adult": "18+"}


def extract_city(location: str) -> str:
    """Extract city name from Latvian address formats.

    Handles formats like:
      "Rīga" → "rīga"
      "Valmieras novads, Valmiera" → "valmiera"
      "Rīgas novads, Mārupe" → "mārupe"
      "Liepāja" → "liepāja"
    """
    if not isinstance(location, str) or not location.strip():
        return ""
    location = location.strip()
    if "," in location:
        parts = [p.strip() for p in location.split(",")]
        city = parts[-1]
    else:
        city = re.sub(r"\s+(novads|pagasts|pilsēta)$", "", location, flags=re.IGNORECASE)
    return city.strip().lower()


def check_same_city(patient_address: str, facility_name: str) -> bool:
    """Check if patient address and facility are in the same city."""
    p = extract_city(patient_address)
    f = extract_city(facility_name)
    return p != "" and p == f


def get_distance_group(distance_km: float | None) -> str:
    """Return distance group label for a single distance value."""
    if distance_km is None or np.isnan(distance_km):
        return "unknown"
    if distance_km < 10:
        return "0-10km"
    if distance_km < 30:
        return "10-30km"
    if distance_km < 100:
        return "30-100km"
    return "100+km"


# ---------------------------------------------------------------------------
# Internal DataFrame-level functions
# ---------------------------------------------------------------------------

def _add_patient_age(df: pd.DataFrame) -> pd.DataFrame:
    """Compute patient age at appointment time."""
    appointment_year = df["appointment_date"].dt.year
    df["patient_age"] = appointment_year - df["patient_birth_year"]
    df.loc[df["patient_age"] < 0, "patient_age"] = np.nan
    df.loc[df["patient_age"] > 120, "patient_age"] = np.nan
    return df


def _add_age_group(df: pd.DataFrame) -> pd.DataFrame:
    """Categorise patients into age groups."""
    conditions = [
        df["patient_age"].between(0, 6),
        df["patient_age"].between(7, 18),
        df["patient_age"] > 18,
    ]
    choices = ["toddler", "school_age", "adult"]
    df["age_group"] = np.select(conditions, choices, default="unknown")
    return df


def _add_same_city(df: pd.DataFrame) -> pd.DataFrame:
    """Determine if patient lives in the same city as the facility."""
    patient_city = df["patient_address"].apply(extract_city)
    facility_city = df["facility_name"].apply(extract_city)
    df["same_city"] = (patient_city == facility_city) & (patient_city != "")
    return df


def _add_distance_group(df: pd.DataFrame) -> pd.DataFrame:
    """Categorise distance into groups."""
    conditions = [
        df["distance_to_appointment_km"].between(0, 10, inclusive="left"),
        df["distance_to_appointment_km"].between(10, 30, inclusive="left"),
        df["distance_to_appointment_km"].between(30, 100, inclusive="left"),
        df["distance_to_appointment_km"] >= 100,
    ]
    choices = ["0-10km", "10-30km", "30-100km", "100+km"]
    df["distance_group"] = np.select(conditions, choices, default="unknown")
    return df
