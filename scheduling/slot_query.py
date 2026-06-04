"""SQL queries for fetching available appointment slots from saule.vizisu_laiki."""

import logging
import zoneinfo
from datetime import date, datetime, timedelta

from psycopg2 import sql

from scheduling.db import get_connection, get_config, qualify_table

logger = logging.getLogger(__name__)

# Status codes — support both zero-padded and bare integer formats
_FREE_STATUSES = ("0001", "1")
_FREE_AND_RESERVED_STATUSES = ("0001", "0002", "1", "2")

# Default reception mode — ambulatory
_DEFAULT_RECEPTION_MODE = "0001"


def _expand_reception_modes(raw: str | list[str] | None) -> tuple[str, ...]:
    """Expand reception mode codes to include both zero-padded and bare forms.

    Accepts a single code string or a list.  Returns a tuple suitable for
    SQL ``IN`` clauses.  E.g. ``"0002"`` → ``("0002", "2")``.
    """
    if raw is None or (isinstance(raw, list) and not raw):
        raw = _DEFAULT_RECEPTION_MODE
    if isinstance(raw, str):
        raw = [raw]
    expanded: set[str] = set()
    for code in raw:
        expanded.add(code)
        stripped = code.lstrip("0") or "0"
        expanded.add(stripped)
        # Also add zero-padded form if bare int was given
        expanded.add(code.zfill(4))
    return tuple(sorted(expanded))

# Sentinel value: pass as date_to to remove the upper date bound.
NO_UPPER_DATE = object()


def query_available_slots(
    hospital_id: int,
    service_id: int | None,
    specialist_id: int | None,
    appointment_type: list[int] | None,
    slot_filter: str,
    paid_visit: bool | None,
    excluded_dates: list[str],
    date_from: date | None = None,
    date_to: date | object | None = None,
    reception_mode: str | list[str] | None = None,
) -> list[dict]:
    """Query saule.vizisu_laiki for available slots within the search window.

    Args:
        date_from: Override start date (default: today).
        date_to: Override end date (default: today + search_months).
                 Pass ``NO_UPPER_DATE`` to remove the upper bound
                 (used for earliest-slot queries).

    Returns list of slot dicts ordered by (vizites_datums, sakuma_laiks).
    """
    config = get_config()
    search_months = config.get("search_months", 4)
    max_slots = config.get("max_slots_per_query", 5000)
    slots_table = config.get("tables", {}).get("slots", "saule.vizisu_laiki")

    now = datetime.now(zoneinfo.ZoneInfo("Europe/Riga"))
    today = now.date()
    start = date_from if date_from is not None else today

    unbounded = date_to is NO_UPPER_DATE
    if unbounded:
        end = None
    elif date_to is not None:
        end = date_to
    else:
        end = today + timedelta(days=search_months * 30)

    # Build WHERE clauses
    sveids = _expand_reception_modes(reception_mode)
    conditions = [
        sql.SQL("slimnicas_id = %s"),
        sql.SQL("pakalpojuma_sveids IN %s"),
    ]
    if end is not None:
        conditions.append(sql.SQL("vizites_datums BETWEEN %s AND %s"))
        params: list = [hospital_id, sveids, start, end]
    else:
        conditions.append(sql.SQL("vizites_datums >= %s"))
        params: list = [hospital_id, sveids, start]

    # Status filter
    if slot_filter == "free_and_reserved":
        conditions.append(sql.SQL("statuss IN %s"))
        params.append(_FREE_AND_RESERVED_STATUSES)
    else:
        conditions.append(sql.SQL("statuss IN %s"))
        params.append(_FREE_STATUSES)

    # Optional filters
    if service_id is not None:
        conditions.append(sql.SQL("pakalpojuma_id = %s"))
        params.append(service_id)

    if specialist_id is not None:
        conditions.append(sql.SQL("arsta_id = %s"))
        params.append(specialist_id)

    if appointment_type is not None and len(appointment_type) > 0:
        if len(appointment_type) == 1:
            conditions.append(sql.SQL("vizites_veids = %s"))
            params.append(appointment_type[0])
        else:
            placeholders = sql.SQL(", ").join([sql.Placeholder()] * len(appointment_type))
            conditions.append(sql.SQL("vizites_veids IN ({})").format(placeholders))
            params.extend(appointment_type)

    if paid_visit is not None:
        conditions.append(sql.SQL("maksas = %s"))
        params.append(paid_visit)

    if excluded_dates:
        conditions.append(sql.SQL("vizites_datums NOT IN %s"))
        params.append(tuple(excluded_dates))

    where_clause = sql.SQL(" AND ").join(conditions)

    query = sql.SQL(
        "SELECT id, vizites_datums, sakuma_laiks, beigu_laiks, "
        "arsta_id, specialitates_id, slimnicas_id, pakalpojuma_id, "
        "vizites_veids, maksas, statuss "
        "FROM {table} WHERE {where} "
        "ORDER BY vizites_datums, sakuma_laiks "
        "LIMIT %s"
    ).format(
        table=qualify_table(slots_table),
        where=where_clause,
    )
    params.append(max_slots)

    import time as _time

    t0 = _time.monotonic()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
    elapsed_ms = round((_time.monotonic() - t0) * 1000)

    slots = [dict(zip(columns, row)) for row in rows]

    # Filter out slots in the past (must be at least 1 hour from now)
    min_start = now.hour * 60 + now.minute + 60
    slots = [
        s for s in slots
        if s["vizites_datums"] > today or s["sakuma_laiks"] >= min_start
    ]

    logger.info(
        "Slot query: service_id=%s specialist_id=%s slots=%d query_ms=%d",
        service_id, specialist_id, len(slots), elapsed_ms,
    )

    return slots


def format_time(minutes_since_midnight: int) -> str:
    """Convert minutes-since-midnight to HH:MM string."""
    return f"{minutes_since_midnight // 60:02d}:{minutes_since_midnight % 60:02d}"


def slot_hour(sakuma_laiks: int) -> int:
    """Extract the hour for profile matching (matching export_training.sql logic)."""
    return max(0, (sakuma_laiks - 1) // 60)
