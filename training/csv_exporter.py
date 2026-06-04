"""Export training.csv and profiling_input.csv from the PostgreSQL database.

Both queries use the same CTE base (saule.vizites UNION ALL saule.vizites_jn)
with a rolling N-month window ending at the 1st of the current month.

Distance is left NULL — the Python-side haversine enrichment fills it later.
"""

import logging
import os
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _rolling_window(window_months: int = 8) -> tuple[date, date]:
    """Return (start_date, end_date) for the rolling window.

    end_date   = 1st of the current month
    start_date = end_date minus *window_months* months
    """
    today = date.today()
    end = today.replace(day=1)  # first of this month

    # Subtract months (handles year wrap)
    m = end.month - window_months
    y = end.year
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)

    return start, end


# ---------------------------------------------------------------------------
# Shared CTE (used by both training and profiling queries)
# ---------------------------------------------------------------------------

_VIZITES_CTE = """
WITH vizites AS (
    -- active visits
    SELECT
          v.vizites_datums
        , v.sakuma_laiks
        , v.pacienta_id
        , date_trunc('year', p.gebdat)   AS dzimsanas_gads
        , p.ort                           AS pacienta_dzivesvieta
        , a.arsta_id
        , s.nosaukums                     AS spec_nosaukums
        , sl.nosaukums                    AS novietne
        , CASE
              WHEN v.veids = 0 THEN 'vizite bez pieraksta'
              WHEN v.veids = 3 THEN 'rezervetais laiks'
              WHEN v.veids = 4 THEN 'hronisks pacients'
              WHEN v.veids = 5 THEN 'prioritaras konsultacijas pacients'
              WHEN v.veids = 6 THEN 'attalinata konsultacija'
              ELSE 'zalais laiks'
          END                             AS vizites_veids
        , CASE
              WHEN v.statuss = ANY (ARRAY[1, 2]) THEN 1
              ELSE 0
          END                             AS pacients_ir_ieradies
        , v.pieraksta_datums
        , v.statuss                       AS vizites_statuss
        , v.specialitates_id
        , v.appointment_id
        , sp.nosaukums                    AS pakalpojums
        , v.pakalpojuma_id
        , v.pakalpojuma_sveids
    FROM saule.vizites v
    JOIN stammdaten p             ON v.pacienta_id      = p.pid
    JOIN saule.arsti_vw a         ON v.arsta_id         = a.arsta_id
    JOIN saule.specialitates_vw s ON v.specialitates_id = s.specialitates_id
    JOIN saule.pakalpojumi sp     ON v.pakalpojuma_id   = sp.id
    LEFT JOIN saule.slimnicas_vw sl ON v.slimnicas_id   = sl.slimnicas_id
    WHERE v.vizites_datums >= %(start)s
      AND v.vizites_datums  < %(end)s
      AND v.pacienta_id > 0
      AND v.statuss IN (0, 1, 2, 3, 4)
      AND NOT lower(s.nosaukums) LIKE '%%sporta%%'

    UNION ALL

    -- deleted visits
    SELECT
          v.vizites_datums
        , v.sakuma_laiks
        , v.pacienta_id
        , date_trunc('year', p.gebdat)   AS dzimsanas_gads
        , p.ort                           AS pacienta_dzivesvieta
        , a.arsta_id
        , s.nosaukums                     AS spec_nosaukums
        , sl.nosaukums                    AS novietne
        , CASE
              WHEN v.veids = 0 THEN 'vizite bez pieraksta'
              WHEN v.veids = 3 THEN 'rezervetais laiks'
              WHEN v.veids = 4 THEN 'hronisks pacients'
              WHEN v.veids = 5 THEN 'prioritaras konsultacijas pacients'
              WHEN v.veids = 6 THEN 'attalinata konsultacija'
              ELSE 'zalais laiks'
          END                             AS vizites_veids
        , 0                               AS pacients_ir_ieradies
        , v.pieraksta_datums
        , v.statuss                       AS vizites_statuss
        , v.specialitates_id
        , v.appointment_id
        , sp.nosaukums                    AS pakalpojums
        , v.pakalpojuma_id
        , v.pakalpojuma_sveids
    FROM saule.vizites_jn v
    JOIN stammdaten p             ON v.pacienta_id      = p.pid
    JOIN saule.arsti_vw a         ON v.arsta_id         = a.arsta_id
    JOIN saule.specialitates_vw s ON v.specialitates_id = s.specialitates_id
    JOIN saule.pakalpojumi sp     ON v.pakalpojuma_id   = sp.id
    LEFT JOIN saule.slimnicas_vw sl ON v.slimnicas_id   = sl.slimnicas_id
    WHERE v.vizites_datums >= %(start)s
      AND v.vizites_datums  < %(end)s
      AND v.pacienta_id > 0
      AND v.statuss IN (0, 1, 2, 3, 4)
      AND v.operation = 'D'
      AND NOT lower(s.nosaukums) LIKE '%%sporta%%'
)
"""

# ---------------------------------------------------------------------------
# Training CSV query
# ---------------------------------------------------------------------------

_TRAINING_SELECT = """
SELECT
      v.pacients_ir_ieradies                      AS appointment_attended
    , v.pacienta_id                               AS patient_id
    , CASE
          WHEN v.pacienta_id IN (
              SELECT h.pacienta_id FROM saule.pac_hronisks h WHERE h.statuss = 1
          ) THEN 1
          ELSE 0
      END                                         AS chronic_patient
    , EXTRACT(YEAR FROM v.dzimsanas_gads)         AS patient_birth_year
    , v.pacienta_dzivesvieta                      AS patient_address
    , v.novietne                                  AS facility_name
    , NULL::numeric                               AS distance_to_appointment_km
    , v.pieraksta_datums                          AS appointment_date_registration
    , v.vizites_datums                            AS appointment_date
    , (v.sakuma_laiks - 1) / 60                   AS appointment_time_hour
    , v.vizites_veids                             AS appointment_type
    , v.arsta_id                                  AS doctor_id
    , v.specialitates_id                          AS doctor_type_id
    , v.pakalpojums                               AS service
    , v.pakalpojuma_sveids                        AS service_type_id
    , v.vizites_statuss                           AS appointment_status
    , 0                                           AS appointment_approved
FROM vizites v
WHERE v.pieraksta_datums <= v.vizites_datums
ORDER BY v.vizites_datums, v.pacienta_id ASC
"""

# ---------------------------------------------------------------------------
# Profiling CSV query
# ---------------------------------------------------------------------------

_PROFILING_SELECT = """
SELECT
      v.pacients_ir_ieradies                      AS appointment_attended
    , v.pacienta_id                               AS patient_id
    , EXTRACT(YEAR FROM v.dzimsanas_gads)         AS patient_birth_year
    , v.pacienta_dzivesvieta                      AS patient_address
    , v.novietne                                  AS facility_name
    , NULL::numeric                               AS distance_to_appointment_km
    , v.specialitates_id
    , v.pakalpojuma_id                            AS service_id
    , v.spec_nosaukums                            AS specialty_name
    , (v.sakuma_laiks - 1) / 60                   AS appointment_hour
    , v.vizites_datums                            AS appointment_date
    , EXTRACT(DOW FROM v.vizites_datums)          AS day_of_week
FROM vizites v
WHERE v.pieraksta_datums <= v.vizites_datums
  AND EXTRACT(DOW FROM v.vizites_datums) BETWEEN 1 AND 5
  AND (v.sakuma_laiks - 1) / 60 BETWEEN 8 AND 17
  AND v.pakalpojuma_sveids = '0001'
ORDER BY v.vizites_datums, v.pacienta_id ASC
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_training_csv(
    conn,
    output_path: str,
    window_months: int = 8,
) -> int:
    """Run the training query and write result to *output_path*.

    Returns the number of rows exported.
    """
    start, end = _rolling_window(window_months)
    sql = _VIZITES_CTE + _TRAINING_SELECT
    params = {"start": start, "end": end}

    logger.info(
        "Exporting training CSV  [%s → %s)  → %s",
        start.isoformat(), end.isoformat(), output_path,
    )

    df = pd.read_sql(sql, conn, params=params)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    df.to_csv(output_path, index=False)

    logger.info("Training CSV exported: %d rows", len(df))
    return len(df)


def export_profiling_csv(
    conn,
    output_path: str,
    window_months: int = 8,
) -> int:
    """Run the profiling query and write result to *output_path*.

    Returns the number of rows exported.
    """
    start, end = _rolling_window(window_months)
    sql = _VIZITES_CTE + _PROFILING_SELECT
    params = {"start": start, "end": end}

    logger.info(
        "Exporting profiling CSV [%s → %s)  → %s",
        start.isoformat(), end.isoformat(), output_path,
    )

    df = pd.read_sql(sql, conn, params=params)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    df.to_csv(output_path, index=False)

    logger.info("Profiling CSV exported: %d rows", len(df))
    return len(df)
