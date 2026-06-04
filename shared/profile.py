"""Reusable patient profile resolution — used by /profile and /schedule endpoints."""

from profiling.feature_engineering import (
    get_age_group,
    check_same_city,
    get_distance_group,
)
from shared.coordinates import (
    calculate_distance,
    geocode_nominatim,
    HOSPITAL_MAP,
)


class UnknownHospitalError(ValueError):
    """Raised when an unrecognised hospital_id is provided."""


def resolve_patient_profile(
    patient_age: int,
    patient_address: str,
    hospital_id: int,
    coords_lookup: dict,
    day_time_interactions: dict,
) -> tuple[str, float | None, list[dict]]:
    """Reusable: calculate distance, determine profile, return day x time cells.

    Returns: (profile_key, distance_km, day_time_cells)
    - day_time_cells: list of ranked entries from day_time_interactions.json

    Raises ``UnknownHospitalError`` if *hospital_id* is not recognised.
    """
    facility_name = HOSPITAL_MAP.get(hospital_id)
    if facility_name is None:
        valid_ids = sorted(HOSPITAL_MAP.keys())
        raise UnknownHospitalError(
            f"Unknown hospital_id: {hospital_id}. Valid IDs: {valid_ids}"
        )

    age_group = get_age_group(patient_age)
    same_city = check_same_city(patient_address, facility_name)

    # 1. Try in-memory lookup + haversine
    distance_km = calculate_distance(patient_address, facility_name, coords_lookup)

    # 2. If address unknown, try Nominatim real-time (~1s)
    if distance_km is None:
        nominatim_coords = geocode_nominatim(patient_address)
        if nominatim_coords:
            coords_lookup[patient_address.strip().lower()] = nominatim_coords
            distance_km = calculate_distance(patient_address, facility_name, coords_lookup)

    # 3. Determine distance group with fallback
    if distance_km is not None:
        distance_group = get_distance_group(distance_km)
    elif same_city:
        distance_group = "0-10km"
    else:
        distance_group = "30-100km"

    # Look up profile by age_group + distance_group
    profile_key = f"{age_group}_{distance_group}"
    cells = day_time_interactions.get(profile_key)

    if not cells or len(cells) == 0:
        fallback = _find_fallback_profile(day_time_interactions, age_group)
        if fallback:
            profile_key, cells = fallback
        else:
            return profile_key, distance_km, []

    return profile_key, distance_km, cells


def _find_fallback_profile(profiles: dict, age_group: str):
    """Find any profile matching the age group as a fallback."""
    for key, val in profiles.items():
        if key.startswith(age_group) and len(val) > 0:
            return key, val
    return None
