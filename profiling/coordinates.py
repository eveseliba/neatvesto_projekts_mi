"""Re-export from shared.coordinates for backwards compatibility."""
from shared.coordinates import *  # noqa: F401,F403
from shared.coordinates import (
    strip_diacritics,
    extract_city,
    FACILITY_COORDS,
    parse_coordinates_sql,
    build_lookup,
    resolve_address,
    haversine,
    calculate_distance,
    geocode_nominatim,
    append_to_sql_file,
    default_sql_path,
    _FACILITY_COORDS_NORMALIZED,
)
