"""Distance enrichment functions for training and profiling pipelines.

Provides DataFrame-level operations that use the shared coordinates module
to fill NULL distances and geocode unknown addresses.
"""

import logging
import os

import pandas as pd

from .coordinates import (
    parse_coordinates_sql,
    build_lookup,
    resolve_address,
    haversine,
    geocode_nominatim,
    append_to_sql_file,
    FACILITY_COORDS,
    FACILITY_COORDS_NORMALIZED,
    default_sql_path,
)

logger = logging.getLogger(__name__)


def enrich_distances(
    df: pd.DataFrame,
    coords_sql_path: str | None = None,
) -> pd.DataFrame:
    """Fill NULL ``distance_to_appointment_km`` using coordinates lookup + haversine.

    Requires columns: ``patient_address``, ``facility_name``,
    ``distance_to_appointment_km``.
    """
    if "distance_to_appointment_km" not in df.columns:
        logger.info("Distance enrichment skipped: column not present")
        return df

    if coords_sql_path is None:
        coords_sql_path = default_sql_path()
    if coords_sql_path is None or not os.path.exists(coords_sql_path):
        logger.warning("Distance enrichment skipped: coordinates SQL not found")
        return df

    df = df.copy()
    null_mask = df["distance_to_appointment_km"].isna()
    null_count = int(null_mask.sum())
    if null_count == 0:
        logger.info("Distance enrichment: no NULL distances to fill")
        return df

    raw_coords = parse_coordinates_sql(coords_sql_path)
    lookup = build_lookup(raw_coords)

    facility_cache: dict[str, tuple[float, float] | None] = {}
    for name, coord in FACILITY_COORDS.items():
        facility_cache[name] = coord

    null_indices = df.index[null_mask]
    patient_addrs = (
        df.loc[null_indices, "patient_address"]
        if "patient_address" in df.columns
        else pd.Series(dtype=str)
    )
    facility_names = (
        df.loc[null_indices, "facility_name"]
        if "facility_name" in df.columns
        else pd.Series(dtype=str)
    )

    if patient_addrs.empty or facility_names.empty:
        logger.info("Distance enrichment skipped: patient_address or facility_name column missing")
        return df

    # Cache address resolutions
    unique_addrs = patient_addrs.dropna().unique()
    addr_cache: dict[str, tuple[float, float] | None] = {}
    for addr in unique_addrs:
        addr_cache[addr] = resolve_address(addr, lookup)

    enriched = 0
    distances = df["distance_to_appointment_km"].copy()
    for idx, p_addr, f_name in zip(null_indices, patient_addrs, facility_names):
        p_coord = addr_cache.get(p_addr) if isinstance(p_addr, str) else None
        f_coord = facility_cache.get(f_name) if isinstance(f_name, str) else None
        if f_coord is None and isinstance(f_name, str):
            f_coord = FACILITY_COORDS_NORMALIZED.get(f_name.strip().lower())
        if p_coord is not None and f_coord is not None:
            dist = haversine(p_coord[0], p_coord[1], f_coord[0], f_coord[1])
            distances.at[idx] = round(dist, 3)
            enriched += 1

    df["distance_to_appointment_km"] = distances
    remaining = int(df["distance_to_appointment_km"].isna().sum())
    logger.info(
        "Distance enrichment: filled %d of %d NULL rows (%.1f%%), %d still NULL",
        enriched, null_count, enriched / null_count * 100, remaining,
    )
    return df


def geocode_unknown_addresses(
    df: pd.DataFrame,
    coords_sql_path: str | None = None,
) -> int:
    """Geocode patient addresses not in the coordinates lookup via Nominatim.

    Finds unique patient addresses with NULL distance that can't be resolved
    from the existing coordinates file, geocodes them via OpenStreetMap
    Nominatim, and appends successful results to the SQL file.

    This runs before ``enrich_distances()`` so the enrichment step benefits
    from the newly geocoded addresses.
    """
    if coords_sql_path is None:
        coords_sql_path = default_sql_path()

    if coords_sql_path is None or not os.path.exists(coords_sql_path):
        logger.warning("Geocoding skipped: coordinates SQL not found")
        return 0

    raw_coords = parse_coordinates_sql(coords_sql_path)
    lookup = build_lookup(raw_coords)

    # Find unique patient addresses with NULL distance
    null_mask = df["distance_to_appointment_km"].isna()
    null_addrs = df.loc[null_mask, "patient_address"].dropna().unique()

    # Filter to truly unresolved addresses
    unresolved = [addr for addr in null_addrs if resolve_address(addr, lookup) is None]

    if not unresolved:
        logger.info("Geocoding: all addresses already resolved")
        return 0

    logger.info("Geocoding %d unknown addresses via Nominatim...", len(unresolved))

    results = []
    ok_count = 0
    fail_count = 0
    for i, addr in enumerate(unresolved, 1):
        coords = geocode_nominatim(addr)
        if coords:
            results.append({"address": addr, "lat": coords[0], "lon": coords[1], "status": "ok"})
            ok_count += 1
        else:
            results.append({"address": addr, "lat": None, "lon": None, "status": "failed"})
            fail_count += 1

        if i % 50 == 0:
            logger.info("  Geocoded %d/%d... (%d ok, %d failed)", i, len(unresolved), ok_count, fail_count)

    # Persist successful results to SQL file
    appended = 0
    ok_results = [r for r in results if r["status"] == "ok"]
    if ok_results:
        appended = append_to_sql_file(coords_sql_path, ok_results)

    logger.info(
        "Geocoding complete: %d ok, %d failed, appended %d to SQL file",
        ok_count, fail_count, appended,
    )

    return appended
