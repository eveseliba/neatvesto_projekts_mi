"""Patient visit history queries and empirical Bayes attendance probability.

Uses three-level hierarchical empirical Bayes shrinkage:

    Level 1 (overall):  rate_overall  = attended_all / n_all
    Level 2 (day):      rate_day      = (attended_day + k × rate_overall) / (n_day + k)
    Level 3 (day+time): attendance_probability = (attended_dt + k × rate_day) / (n_dt + k)

Each level shrinks toward the next level up when observations are sparse.
When no data exists at a given level, the estimate falls through to the
prior above it:
- No day+time visits → uses day rate
- No day visits → uses overall rate
- No visits at all → returns None (caller applies a default)

The k parameter means: "I need ~k observations at this level before I trust
the specific rate over the prior." With fewer observations, the estimate
is pulled toward the parent level.
"""

import logging
import time as _time
from datetime import date, timedelta

from psycopg2 import sql

from scheduling.db import get_connection, get_config, qualify_table

logger = logging.getLogger(__name__)

# Default shrinkage parameter
DEFAULT_K = 5

# How far back to look for patient history
HISTORY_YEARS = 2


def fetch_patient_history(patient_id: int) -> list[dict]:
    """Fetch visit history for a patient from the last 2 years.

    Returns list of dicts with keys: day_of_week, appointment_hour, attended.
    Each row represents a single visit.
    """
    config = get_config()
    history_tables = config.get("tables", {}).get("visits_history", [])

    if not history_tables:
        logger.warning("No visit history tables configured")
        return []

    cutoff_date = date.today() - timedelta(days=HISTORY_YEARS * 365)

    # Build UNION ALL across all history tables
    union_parts = []
    params = []
    for table_name in history_tables:
        part = sql.SQL(
            "SELECT "
            "EXTRACT(ISODOW FROM vizites_datums)::int AS day_of_week, "
            "(sakuma_laiks / 60)::int AS appointment_time_hour, "
            "CASE WHEN statuss = 2 THEN 1 ELSE 0 END AS appointment_attended "
            "FROM {table} "
            "WHERE pacienta_id = %s AND vizites_datums >= %s"
        ).format(table=qualify_table(table_name))
        union_parts.append(part)
        params.extend([patient_id, cutoff_date])

    query = sql.SQL(" UNION ALL ").join(union_parts)

    t0 = _time.monotonic()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
    elapsed_ms = round((_time.monotonic() - t0) * 1000)

    visits = [dict(zip(columns, row)) for row in rows]
    logger.info(
        "Patient history: patient_id=%s visits=%d query_ms=%d",
        patient_id, len(visits), elapsed_ms,
    )

    return visits


def compute_attendance_probability(
    visits: list[dict],
    weekday: int,
    hours: list[int],
    k: int = DEFAULT_K,
) -> tuple[float | None, int]:
    """Compute attendance probability for a specific weekday + time window.

    Uses three-level hierarchical shrinkage:
        overall → day → day+time

    Args:
        visits: patient visit history (from fetch_patient_history)
        weekday: ISO weekday (1=Mon, 5=Fri)
        hours: list of hours in the time window (e.g., [12, 13])
        k: shrinkage parameter

    Returns:
        (attendance_probability, attendance_history_count)
        - attendance_probability is None only if patient has no visits at all
        - attendance_history_count is the number of specific day+time visits
    """
    if not visits:
        return None, 0

    # Level 1: overall stats (the root prior)
    n_all = len(visits)
    attended_all = sum(1 for v in visits if v["appointment_attended"] == 1)
    rate_overall = attended_all / n_all

    # Level 2: day-level stats, shrunk toward overall
    day_visits = [v for v in visits if v["day_of_week"] == weekday]
    n_day = len(day_visits)
    attended_day = sum(1 for v in day_visits if v["appointment_attended"] == 1)
    rate_day = (attended_day + k * rate_overall) / (n_day + k)

    # Level 3: day + time window stats, shrunk toward day
    hours_set = set(hours)
    day_time_visits = [
        v for v in day_visits if v["appointment_time_hour"] in hours_set
    ]
    n_day_time = len(day_time_visits)
    attended_day_time = sum(
        1 for v in day_time_visits if v["appointment_attended"] == 1
    )

    probability = (attended_day_time + k * rate_day) / (n_day_time + k)

    return round(probability, 4), n_day_time


def build_attendance_cache(
    visits: list[dict],
    day_time_cells: list[dict],
    k: int = DEFAULT_K,
) -> dict[tuple[int, tuple[int, ...]], tuple[float, int]]:
    """Precompute attendance probabilities for all profile windows.

    Builds a dict keyed by (weekday, hours_tuple) so that scoring loops
    can do O(1) lookups instead of recomputing over the full visit list
    for every slot.

    Returns empty dict when *visits* is empty (caller treats as "no history").
    """
    if not visits:
        return {}

    n_all = len(visits)
    attended_all = sum(1 for v in visits if v["appointment_attended"] == 1)
    rate_overall = attended_all / n_all

    # Precompute day-level rates (Level 2)
    day_rates: dict[int, float] = {}
    day_visit_lists: dict[int, list[dict]] = {}
    for weekday in range(1, 8):
        day_visits = [v for v in visits if v["day_of_week"] == weekday]
        day_visit_lists[weekday] = day_visits
        n_day = len(day_visits)
        attended_day = sum(1 for v in day_visits if v["appointment_attended"] == 1)
        day_rates[weekday] = (attended_day + k * rate_overall) / (n_day + k)

    # Precompute per (weekday, hours) combo (Level 3)
    cache: dict[tuple[int, tuple[int, ...]], tuple[float, int]] = {}
    for cell in day_time_cells:
        weekday = cell["day_of_week"]
        hours = tuple(sorted(cell["hours"]))
        key = (weekday, hours)
        if key in cache:
            continue

        rate_day = day_rates[weekday]
        hours_set = set(hours)
        day_time_visits = [
            v for v in day_visit_lists[weekday]
            if v["appointment_time_hour"] in hours_set
        ]
        n_dt = len(day_time_visits)
        attended_dt = sum(
            1 for v in day_time_visits if v["appointment_attended"] == 1
        )
        probability = (attended_dt + k * rate_day) / (n_dt + k)
        cache[key] = (round(probability, 4), n_dt)

    logger.info("Attendance cache built: %d entries from %d visits", len(cache), n_all)
    return cache


def lookup_attendance(
    cache: dict[tuple[int, tuple[int, ...]], tuple[float, int]],
    weekday: int,
    hours: list[int],
) -> tuple[float | None, int]:
    """O(1) lookup of precomputed attendance probability.

    Returns (None, 0) when cache is empty or key not found.
    """
    if not cache:
        return None, 0
    key = (weekday, tuple(sorted(hours)))
    return cache.get(key, (None, 0))
