from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import pandas as pd
from pycaret.classification import load_model, predict_model
from pycaret.internal.persistence import load_model as load_model_from_path
import math
import uvicorn
import os
import json
import uuid
import logging
import time as _time
from collections import defaultdict
from datetime import date, datetime, timedelta

# Import functions from training modules
from training.data_preprocessing import (
    handle_patient_birth_year,
    convert_dates
)
from training.feature_engineering import (
    create_days_until_appointment,
    add_time_period,
    add_day_of_week,
    add_children_holiday,
    calculate_patient_age
)

from profiling.feature_engineering import (
    get_age_group,
    extract_city,
    check_same_city,
    get_distance_group,
    AGE_GROUP_LABELS,
)

from shared.coordinates import (
    parse_coordinates_sql,
    build_lookup,
    calculate_distance,
    geocode_nominatim,
    HOSPITAL_MAP,
    default_sql_path,
)
from shared.profile import resolve_patient_profile, UnknownHospitalError

from scheduling.schedule_request import ScheduleRequest
from scheduling.schedule_response import ScheduleResponse, ServiceResult
from scheduling.health import HealthResponse
from scheduling.slot_query import query_available_slots
from scheduling.patient_history import fetch_patient_history
from scheduling.scorer import score_slots_for_service
from scheduling.multi_request import MultiScheduleRequest
from scheduling.multi_response import MultiScheduleResponse, Plan, PlanVisit
from scheduling.multi_scorer import score_multi_schedule, group_slots_by_start, group_all_slots, apply_adaptive_cap
from scheduling.regular_request import RegularScheduleRequest
from scheduling.regular_response import RegularScheduleResponse, RegularPlanVisit
from scheduling.regular_scorer import (
    score_regular_single, score_regular_multi, _select_best_specialists,
)
from scheduling import db as scheduling_db

from fsstate import read_status, update_status, now_iso

logger = logging.getLogger(__name__)

MAIN_API_TOKEN = os.getenv("MAIN_API_TOKEN", "")
MODEL_DIR = os.getenv("MODEL_DIR", "/app")
MODEL_BASENAME = os.getenv("MODEL_BASENAME", "model")
MODEL_PATH = os.path.join(MODEL_DIR, f"{MODEL_BASENAME}.pkl")
DAY_TIME_PATH = os.path.join(MODEL_DIR, "day_time_interactions.json")
COORDS_SQL_PATH = os.path.join(
    MODEL_DIR, "latvian_places_with_coordinates_v2.sql"
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


def verify_token(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if not MAIN_API_TOKEN:
        raise HTTPException(status_code=500, detail="API token not configured")
    if creds.credentials != MAIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return creds.credentials


model = None
day_time_interactions: dict | None = None
coords_lookup: dict[str, tuple[float, float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(MODEL_PATH):
        try:
            _safe_load_model()
        except Exception:
            # Non-fatal; /reload can be called later
            pass
    _load_day_time_interactions()
    _load_coordinates()

    # Initialize scheduling DB pool (non-fatal if unavailable)
    if not scheduling_db.init_pool():
        logger.warning(
            "Scheduling DB unavailable at startup — /schedule will return 503"
        )
    scheduling_db.start_health_check_thread()

    yield

    scheduling_db.close_pool()


# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models — prediction
# ---------------------------------------------------------------------------

class PredictionFeature(BaseModel):
    appointment_date: str
    appointment_date_registration: str
    chronic_patient: bool
    patient_birth_year: int
    distance_to_appointment_km: float
    appointment_time_hour: int
    appointment_type: str
    doctor_type_id: int
    service: str
    is_repeated_patient: int
    past_appointments_count: int
    past_missed_appointments: int

class TrainRequest(BaseModel):
    # Optional fields for future flexibility; your worker may ignore these
    csv_path: str | None = None
    target_column: str | None = None

# ---------------------------------------------------------------------------
# Pydantic models — profiling
# ---------------------------------------------------------------------------

class ProfileRequest(BaseModel):
    patient_age: int
    patient_address: str
    hospital_id: int

class DayTimeCell(BaseModel):
    day_of_week: int
    day_name: str
    window: str
    hours: list[int]
    rate: float
    ci_lower: float
    ci_upper: float
    composite_score: float
    n: int
    attended: int
    low_sample: bool
    lift: float
    tier_ci: int
    rank: int
    tier: int

class ProfileResponse(BaseModel):
    profile_key: str
    distance_km: float | None
    recommendations: list[DayTimeCell]

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _safe_load_model():
    """Load model from /app/model.pkl using PyCaret's load_model(basepath)."""
    global model
    base = os.path.join(MODEL_DIR, MODEL_BASENAME)
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(MODEL_PATH)
    model = load_model_from_path(base)

def _load_day_time_interactions():
    """Load day_time_interactions.json if available."""
    global day_time_interactions
    if os.path.exists(DAY_TIME_PATH):
        with open(DAY_TIME_PATH, "r", encoding="utf-8") as f:
            day_time_interactions = json.load(f)
        logger.info("Loaded day_time_interactions: %d profiles", len(day_time_interactions))


def _load_coordinates():
    """Parse coordinates SQL file into in-memory lookup dict."""
    global coords_lookup
    sql_path = COORDS_SQL_PATH if os.path.exists(COORDS_SQL_PATH) else default_sql_path()
    if sql_path and os.path.exists(sql_path):
        raw = parse_coordinates_sql(sql_path)
        coords_lookup = build_lookup(raw)
        logger.info("Loaded coordinates: %d entries from %s", len(coords_lookup), sql_path)
    else:
        logger.info("Coordinates SQL not found, distance calculation will rely on Nominatim")

# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------


@app.post("/predict")
def predict(input_data: PredictionFeature, _token: str = Depends(verify_token)):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded. Train + /reload first.")
    # Convert incoming request data to a pandas DataFrame
    df = pd.DataFrame([input_data.model_dump()])

    df = handle_patient_birth_year(df)
    df = convert_dates(df, ['appointment_date_registration', 'appointment_date'])

    df = create_days_until_appointment(df)
    df = add_time_period(df)
    df = add_day_of_week(df)
    df = add_children_holiday(df)
    df = calculate_patient_age(df)

    # Generate prediction using PyCaret's loaded model
    predictions_df = predict_model(model, data=df)
    prediction = predictions_df['prediction_label'][0]
    prediction_score = predictions_df['prediction_score'][0]
    return {"prediction": int(prediction), "prediction_score": float(prediction_score)}

@app.post("/train", status_code=status.HTTP_202_ACCEPTED)
def train(req: TrainRequest, _token: str = Depends(verify_token)):
    st = read_status()
    # Allow only one job at a time
    if st.get("processing") and not st.get("done") and not st.get("error"):
        raise HTTPException(status_code=409, detail="A training job is already in progress")

    job_id = str(uuid.uuid4())

    def mut(s: dict):
        s.update({
            "job_id": job_id,
            "csv_path": req.csv_path,
            "target": req.target_column,
            "processing": now_iso(),
            "done": None,
            "reloaded": None,
            "stage": "queued",
            "error": None,
            "claimed_by": None
        })
    update_status(mut)
    return {"message": "Training started", "job_id": job_id}

@app.get("/jobs")
def job_status(_token: str = Depends(verify_token)):
    return read_status()

@app.post("/reload")
def reload_model(_token: str = Depends(verify_token)):
    try:
        _safe_load_model()
        _load_day_time_interactions()
        _load_coordinates()
        update_status(lambda s: s.update({"reloaded": now_iso()}))
        return {"message": "Model, day-time interactions, and coordinates reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reload failed: {e}")

# ---------------------------------------------------------------------------
# Profiling endpoints
# ---------------------------------------------------------------------------

@app.post("/profile", response_model=ProfileResponse)
def get_profile(req: ProfileRequest, _token: str = Depends(verify_token)):
    """Return ranked day x time-window recommendations for a patient.

    Distance is calculated server-side from patient_address + hospital_id
    using the in-memory coordinates lookup. If the address is unknown,
    Nominatim is called in real-time and the result is cached.

    Returns all ranked day x time-window cells for the patient's profile
    (age_group x distance_group) from day_time_interactions.json.
    """
    if day_time_interactions is None:
        raise HTTPException(
            status_code=503,
            detail="Day-time interactions not loaded. Run training pipeline first.",
        )

    try:
        profile_key, distance_km, cells = resolve_patient_profile(
            req.patient_age, req.patient_address, req.hospital_id,
            coords_lookup, day_time_interactions,
        )
    except UnknownHospitalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    recs = [DayTimeCell(**c) for c in cells]

    return ProfileResponse(
        profile_key=profile_key,
        distance_km=distance_km,
        recommendations=recs,
    )


@app.get("/profiles")
def list_profiles(_token: str = Depends(verify_token)):
    """Return all day-time interaction profiles."""
    if day_time_interactions is None:
        raise HTTPException(
            status_code=503,
            detail="Day-time interactions not loaded. Run training pipeline first.",
        )
    return day_time_interactions


# ---------------------------------------------------------------------------
# Scheduling endpoints
# ---------------------------------------------------------------------------

@app.post("/schedule", response_model=ScheduleResponse)
def schedule(req: ScheduleRequest, _token: str = Depends(verify_token)):
    """Find optimal appointment slots for a single service.

    Queries available slots from the scheduling DB, scores them using
    the patient's profile (day x time attendance rates) and individual
    visit history (empirical Bayes shrinkage), and returns the earliest
    slot plus top-3 recommended slots.

    For multi-service scheduling use ``POST /schedule/multi``.
    """
    request_id = str(uuid.uuid4())[:8]
    t0 = _time.monotonic()

    if day_time_interactions is None:
        raise HTTPException(
            status_code=503,
            detail="Day-time interactions not loaded. Run training pipeline first.",
        )
    if not scheduling_db.is_scheduling_available():
        raise HTTPException(
            status_code=503,
            detail="Scheduling database is not available.",
        )

    logger.info(
        "[%s] /schedule request: patient_id=%s service_id=%s hospital_id=%s",
        request_id, req.patient_id, req.service_id, req.hospital_id,
    )

    # Step 1: Resolve patient profile
    try:
        profile_key, distance_km, day_time_cells = resolve_patient_profile(
            req.patient_age, req.patient_address, req.hospital_id,
            coords_lookup, day_time_interactions,
        )
    except UnknownHospitalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(
        "[%s] Profile resolved: %s (distance=%.1fkm)",
        request_id, profile_key, distance_km or 0,
    )

    # Step 2: Fetch patient history
    patient_visits = None
    if req.patient_id is not None:
        try:
            patient_visits = fetch_patient_history(req.patient_id)
        except Exception as e:
            logger.warning(
                "[%s] Failed to fetch patient history: %s", request_id, e
            )

    # Step 3: Query slots — two queries (earliest from today, recommended from tomorrow)
    config = scheduling_db.get_config()
    search_months = config.get("search_months", 4)
    tomorrow = date.today() + timedelta(days=1)
    end_4month = tomorrow + timedelta(days=search_months * 30)
    end_earliest = date.today() + timedelta(days=search_months * 30)

    earliest_slots = query_available_slots(
        hospital_id=req.hospital_id,
        service_id=req.service_id,
        specialist_id=req.specialist_id,
        appointment_type=req.appointment_type,
        slot_filter=req.slot_filter,
        paid_visit=req.paid_visit,
        excluded_dates=req.excluded_dates,
        date_from=date.today(),
        date_to=end_earliest,
        reception_mode=req.reception_mode,
    )
    recommended_slots = query_available_slots(
        hospital_id=req.hospital_id,
        service_id=req.service_id,
        specialist_id=req.specialist_id,
        appointment_type=req.appointment_type,
        slot_filter=req.slot_filter,
        paid_visit=req.paid_visit,
        excluded_dates=req.excluded_dates,
        date_from=tomorrow,
        date_to=end_4month,
        reception_mode=req.reception_mode,
    )

    # Step 3b: Group overlapping slots from different specialists
    earliest_slots = group_slots_by_start(earliest_slots)
    recommended_slots = group_slots_by_start(recommended_slots)

    # Step 4: Score
    result = score_slots_for_service(
        slots=recommended_slots,
        day_time_cells=day_time_cells,
        patient_visits=patient_visits,
        service_id=req.service_id,
        specialist_id=req.specialist_id,
        earliest_slots=earliest_slots,
    )

    elapsed_ms = round((_time.monotonic() - t0) * 1000)
    logger.info(
        "[%s] /schedule response: %dms, %d slots evaluated",
        request_id, elapsed_ms, result.total_slots_evaluated,
    )

    return ScheduleResponse(
        patient_profile=profile_key,
        results=[result],
    )


@app.post("/schedule/multi", response_model=MultiScheduleResponse)
def schedule_multi(req: MultiScheduleRequest, _token: str = Depends(verify_token)):
    """Find optimal multi-visit appointment groupings for a patient.

    Groups multiple services into the fewest hospital visits by finding
    dates where services overlap and validating 15-minute gaps between
    consecutive appointments.
    """
    request_id = str(uuid.uuid4())[:8]
    t0 = _time.monotonic()

    if day_time_interactions is None:
        raise HTTPException(
            status_code=503,
            detail="Day-time interactions not loaded. Run training pipeline first.",
        )
    if not scheduling_db.is_scheduling_available():
        raise HTTPException(
            status_code=503,
            detail="Scheduling database is not available.",
        )

    logger.info(
        "[%s] /schedule/multi request: patient_id=%s services=%d hospital_id=%s",
        request_id, req.patient_id, len(req.services), req.hospital_id,
    )

    # Step 1: Resolve patient profile
    try:
        profile_key, distance_km, day_time_cells = resolve_patient_profile(
            req.patient_age, req.patient_address, req.hospital_id,
            coords_lookup, day_time_interactions,
        )
    except UnknownHospitalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(
        "[%s] Profile resolved: %s (distance=%.1fkm)",
        request_id, profile_key, distance_km or 0,
    )

    # Step 2: Fetch patient history
    patient_visits = None
    if req.patient_id is not None:
        try:
            patient_visits = fetch_patient_history(req.patient_id)
        except Exception as e:
            logger.warning(
                "[%s] Failed to fetch patient history: %s", request_id, e
            )

    # Step 3: Query slots — two queries per service (concurrent)
    config = scheduling_db.get_config()
    search_months = config.get("search_months", 4)
    tomorrow = date.today() + timedelta(days=1)
    end_4month = tomorrow + timedelta(days=search_months * 30)
    end_earliest = date.today() + timedelta(days=search_months * 30)

    def _query_4month(svc):
        return query_available_slots(
            hospital_id=req.hospital_id,
            service_id=svc.service_id,
            specialist_id=svc.specialist_id,
            appointment_type=req.appointment_type,
            slot_filter=req.slot_filter,
            paid_visit=req.paid_visit,
            excluded_dates=req.excluded_dates,
            date_from=tomorrow,
            date_to=end_4month,
            reception_mode=req.reception_mode,
        )

    def _query_earliest(svc):
        return query_available_slots(
            hospital_id=req.hospital_id,
            service_id=svc.service_id,
            specialist_id=svc.specialist_id,
            appointment_type=req.appointment_type,
            slot_filter=req.slot_filter,
            paid_visit=req.paid_visit,
            excluded_dates=req.excluded_dates,
            date_from=date.today(),
            date_to=end_earliest,
            reception_mode=req.reception_mode,
        )

    max_workers = min(len(req.services) * 2, config.get("maxconn", 10))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures_4m = [executor.submit(_query_4month, s) for s in req.services]
        futures_earliest = [executor.submit(_query_earliest, s) for s in req.services]
        all_slots_4month = {i: f.result() for i, f in enumerate(futures_4m)}
        all_slots_earliest = {i: f.result() for i, f in enumerate(futures_earliest)}

    # Step 3b: Group slots before adaptive cap so the cap counts unique
    # (date, start, end, service) groups instead of individual specialist rows.
    raw_count = sum(len(slots) for slots in all_slots_4month.values())
    all_slots_4month = group_all_slots(all_slots_4month)
    all_slots_earliest = group_all_slots(all_slots_earliest)
    grouped_count = sum(len(slots) for slots in all_slots_4month.values())
    if grouped_count < raw_count:
        logger.info(
            "[%s] Slot grouping: %d raw -> %d grouped (%.0f%% reduction)",
            request_id, raw_count, grouped_count,
            (1 - grouped_count / raw_count) * 100 if raw_count else 0,
        )

    # Step 3c: Adaptive window — find date where cumulative slots hit cap
    total_before = sum(len(slots) for slots in all_slots_4month.values())
    all_slots_4month = apply_adaptive_cap(all_slots_4month)
    total_after = sum(len(slots) for slots in all_slots_4month.values())
    if total_after < total_before:
        logger.info(
            "[%s] Adaptive window: %d->%d slots",
            request_id, total_before, total_after,
        )

    # Step 4: Score and group
    service_ids = [svc.service_id for svc in req.services]
    result = score_multi_schedule(
        all_slots_4month=all_slots_4month,
        all_slots_earliest=all_slots_earliest,
        day_time_cells=day_time_cells,
        patient_visits=patient_visits,
        service_ids=service_ids,
        excluded_dates=req.excluded_dates,
    )

    elapsed_ms = round((_time.monotonic() - t0) * 1000)
    logger.info(
        "[%s] /schedule/multi response: %dms, %d services, %d plans",
        request_id, elapsed_ms, len(req.services), len(result["plans"]),
    )

    return MultiScheduleResponse(
        patient_profile=profile_key,
        earliest=result["earliest"],
        plans=[
            Plan(
                rank=p["rank"],
                score=p["score"],
                visits=[PlanVisit(**v) for v in p["visits"]],
            )
            for p in result["plans"]
        ],
    )


EXTENSION_BUFFER_WEEKS = 4  # buffer added to each iterative extension window


def _max_missing_visits(
    visits: list,
    total_times: int,
    service_count: int,
) -> int:
    """Return the max number of missing visits across all services.

    For single-service: total_times - visit count.
    For multi-service: count per service_id, find the biggest deficit.
    """
    if service_count == 1:
        return max(0, total_times - len(visits))

    svc_counts: dict[int | None, int] = defaultdict(int)
    for v in visits:
        seen: set[int | None] = set()
        for s in v.slots:
            if s.service_id not in seen:
                svc_counts[s.service_id] += 1
                seen.add(s.service_id)

    if not svc_counts:
        return total_times

    max_missing = max(total_times - c for c in svc_counts.values())
    # If some requested services got zero visits they won't appear in counts
    if len(svc_counts) < service_count:
        max_missing = max(max_missing, total_times)

    return max(0, max_missing)


@app.post("/schedule/regular", response_model=RegularScheduleResponse)
def schedule_regular(req: RegularScheduleRequest, _token: str = Depends(verify_token)):
    """Schedule recurring appointments over a time span.

    Divides [start_date, until_date] into calendar work-weeks (Mon-Fri)
    and picks up to times_per_week slots per week using the patient's
    profile, with >=1 day spacing preference between visits.
    """
    request_id = str(uuid.uuid4())[:8]
    t0 = _time.monotonic()

    if day_time_interactions is None:
        raise HTTPException(
            status_code=503,
            detail="Day-time interactions not loaded. Run training pipeline first.",
        )
    if not scheduling_db.is_scheduling_available():
        raise HTTPException(
            status_code=503,
            detail="Scheduling database is not available.",
        )

    logger.info(
        "[%s] /schedule/regular request: patient_id=%s services=%d times_per_week=%d total_times=%d start=%s",
        request_id, req.patient_id, len(req.services),
        req.times_per_week, req.total_times, req.start_date,
    )

    # Step 1: Resolve patient profile
    try:
        profile_key, distance_km, day_time_cells = resolve_patient_profile(
            req.patient_age, req.patient_address, req.hospital_id,
            coords_lookup, day_time_interactions,
        )
    except UnknownHospitalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(
        "[%s] Profile resolved: %s (distance=%.1fkm)",
        request_id, profile_key, distance_km or 0,
    )

    # Step 2: Fetch patient history
    patient_visits = None
    if req.patient_id is not None:
        try:
            patient_visits = fetch_patient_history(req.patient_id)
        except Exception as e:
            logger.warning(
                "[%s] Failed to fetch patient history: %s", request_id, e
            )

    # Step 3: Build attendance cache once
    from scheduling.patient_history import build_attendance_cache
    att_cache = build_attendance_cache(patient_visits or [], day_time_cells)

    # Step 4: Iterative slot fetching — extend window until all visits
    #         found or 1-year limit reached
    start = date.fromisoformat(req.start_date)
    max_until = req.max_until_date           # start + 52 weeks
    current_until = req.computed_until_date   # start + ceil(total/tpw) weeks
    query_from = start

    config = scheduling_db.get_config()
    max_workers = min(len(req.services), config.get("maxconn", 10))
    all_slots_raw: dict[int, list[dict]] = {i: [] for i in range(len(req.services))}
    visits: list[RegularPlanVisit] = []
    iteration = 0
    chosen_specialists: dict[int, int] = {}  # svc_idx → arsta_id

    while True:
        iteration += 1

        # Query [query_from, current_until] per service
        q_from, q_until = query_from, current_until

        def _query_service(idx_svc, _from=q_from, _until=q_until):
            idx, svc = idx_svc
            spec_id = svc.specialist_id or chosen_specialists.get(idx)
            return idx, query_available_slots(
                hospital_id=req.hospital_id,
                service_id=svc.service_id,
                specialist_id=spec_id,
                appointment_type=req.appointment_type,
                slot_filter=req.slot_filter,
                paid_visit=req.paid_visit,
                excluded_dates=req.excluded_dates,
                date_from=_from,
                date_to=_until,
                reception_mode=req.reception_mode,
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_query_service, (i, s))
                for i, s in enumerate(req.services)
            ]
            for f in futures:
                idx, new_slots = f.result()
                all_slots_raw[idx].extend(new_slots)

        # After first fetch, lock in specialist per service for consistency
        if iteration == 1:
            auto_selected = _select_best_specialists(
                all_slots_raw, start, current_until,
            )
            for idx, svc in enumerate(req.services):
                if svc.specialist_id is None and idx in auto_selected:
                    chosen_specialists[idx] = auto_selected[idx]
            # Filter existing slots to chosen specialists
            for idx, arsta_id in chosen_specialists.items():
                all_slots_raw[idx] = [
                    s for s in all_slots_raw[idx]
                    if s["arsta_id"] == arsta_id
                ]

        # Group all accumulated slots and score
        all_slots_grouped = group_all_slots(all_slots_raw)

        if len(req.services) == 1:
            visits = score_regular_single(
                all_slots=all_slots_grouped.get(0, []),
                day_time_cells=day_time_cells,
                times_per_week=req.times_per_week,
                total_times=req.total_times,
                until_date=current_until,
                excluded_dates=req.excluded_dates,
                att_cache=att_cache,
                start_date=start,
            )
        else:
            visits = score_regular_multi(
                all_slots=all_slots_grouped,
                day_time_cells=day_time_cells,
                times_per_week=req.times_per_week,
                total_times=req.total_times,
                until_date=current_until,
                service_count=len(req.services),
                excluded_dates=req.excluded_dates,
                att_cache=att_cache,
                start_date=start,
            )

        missing = _max_missing_visits(visits, req.total_times, len(req.services))

        logger.info(
            "[%s] Iteration %d: query=[%s, %s] until=%s visits=%d missing=%d",
            request_id, iteration, q_from, q_until, current_until,
            len(visits), missing,
        )

        if missing == 0 or current_until >= max_until:
            break

        # Extend window: next range starts day after current_until
        query_from = current_until + timedelta(days=1)
        ext_weeks = math.ceil(missing / req.times_per_week) + EXTENSION_BUFFER_WEEKS
        current_until = min(
            query_from + timedelta(weeks=ext_weeks),
            max_until,
        )

    elapsed_ms = round((_time.monotonic() - t0) * 1000)
    logger.info(
        "[%s] /schedule/regular response: %dms, %d visits",
        request_id, elapsed_ms, len(visits),
    )

    return RegularScheduleResponse(
        patient_profile=profile_key,
        start_date=req.start_date,
        times_per_week=req.times_per_week,
        total_times=req.total_times,
        total_visits=len(visits),
        visits=visits,
    )


@app.get("/health")
def health_check():
    """Service health check including scheduling DB status."""
    pool_status = scheduling_db.get_pool_status()

    if pool_status["scheduling_db"] == "connected":
        return HealthResponse(
            status="healthy",
            scheduling_db="connected",
            pool_size=pool_status.get("pool_size"),
        )
    else:
        raise HTTPException(
            status_code=503,
            detail=HealthResponse(
                status="degraded",
                scheduling_db="disconnected",
                error=pool_status.get("error"),
            ).model_dump(),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global model
    # Load model if present; otherwise start API and wait for /reload
    try:
        model = load_model('model')
    except Exception:
        model = None
    # Start Uvicorn server
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
