# ML Training & Prediction API Architecture

A distributed machine learning system with a REST API frontend ([`app.py`](app.py)) and background training worker ([`worker.py`](worker.py)) that coordinate via a shared state file ([`status.json`](status.json)).

---

## Components

- **[`app.py`](app.py)** - FastAPI server providing REST endpoints for training, predictions, and status monitoring
- **[`worker.py`](worker.py)** - Background process that polls for training jobs and executes them
- **[`distance_service.py`](distance_service.py)** - Standalone distance calculation microservice with token auth (`DISTANCE_API_TOKEN`, see [README.distance.md](README.distance.md))

**Authentication:** Both the main API and distance service require Bearer token authentication. The main API uses `MAIN_API_TOKEN`, the distance service uses `DISTANCE_API_TOKEN` (separate tokens). The `/health` endpoints are unauthenticated.
- **[`shared/`](shared/)** - Shared modules used by both app and worker:
  - **[`coordinates.py`](shared/coordinates.py)** - Coordinate resolution, haversine distance, Nominatim geocoding, SQL parsing
  - **[`distance_enrichment.py`](shared/distance_enrichment.py)** - DataFrame-level distance enrichment and batch geocoding
- **[`status.json`](status.json)** - Shared state file for job coordination between app and worker
- **[`fsstate.py`](fsstate.py)** - Atomic file operations and locking utilities
- **[`model.pkl`](model.pkl)** - Trained PyCaret model (created by worker, used by app)
- **`day_time_interactions.json`** - Per-day × time-window profile data (created by worker, used by app)
- **`latvian_places_with_coordinates_v2.sql`** - Coordinates cache (SQL INSERT file, grows as new addresses are geocoded)

---

## How It Works

1. Client calls `POST /train` → [`app.py`](app.py) writes job to [`status.json`](status.json)
2. [`worker.py`](worker.py) polls every 60s, detects job, acquires lock, trains model
3. [`worker.py`](worker.py) runs **distance enrichment** (geocodes unknown addresses via Nominatim, fills NULL distances via haversine), then trains model and saves [`model.pkl`](model.pkl)
4. [`worker.py`](worker.py) runs the profiling pipeline (generates `day_time_interactions.json` if `profiling_input.csv` is available)
5. [`worker.py`](worker.py) updates [`status.json`](status.json) with completion status
6. Client calls `POST /reload` → [`app.py`](app.py) loads new model, day_time_interactions, and coordinates into memory
7. Client calls `POST /predict` → [`app.py`](app.py) uses loaded model for predictions
8. Client calls `POST /profile` → [`app.py`](app.py) calculates distance server-side and returns time-window recommendations

---

## API Endpoints

### Training & Management

#### `POST /train`
Start a new training job (asynchronous, returns immediately).

**Request:**
```bash
curl -X POST http://localhost:8000/train \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"csv_path": "training.csv", "target_column": "appointment_attended"}'
```

**Response (202 Accepted):**
```json
{
  "message": "Training started",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

**Notes:**
- Only one training job can run at a time (returns 409 if job already in progress)
- Training happens in background via [`worker.py`](worker.py)
- The worker also runs the **profiling pipeline** after model training, which requires a separate `profiling_input.csv` file in the same directory (generated from `profiling_input.sql`). If this file is missing, profiling is skipped but the prediction model is still saved

---

#### `GET /jobs`
Get current training job status.

**Request:**
```bash
curl http://localhost:8000/jobs \
  -H "Authorization: Bearer $MAIN_API_TOKEN"
```

**Response:**
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "csv_path": "training.csv",
  "target": "appointment_attended",
  "processing": "2025-12-02T06:59:15Z",
  "done": "2025-12-02T07:59:15Z",
  "reloaded": "2025-12-02T08:10:15Z",
  "stage": "done",
  "error": null,
  "claimed_by": "worker-hostname",
  "rev": 4
}
```

**Status Fields:**
- `processing`: Job start timestamp (set by [`app.py`](app.py))
- `done`: Job completion timestamp (set by [`worker.py`](worker.py))
- `reloaded`: Model reload timestamp (set by [`app.py`](app.py))
- `stage`: Current stage (`queued` → `loading_csv` → `training` → `geocoding` → `profiling` → `done`/`failed`)
- `error`: Traceback if training failed
- `claimed_by`: Worker hostname that claimed the job

---

#### `POST /reload`
Reload the trained model, day_time_interactions, and coordinates into memory.

**Request:**
```bash
curl -X POST http://localhost:8000/reload \
  -H "Authorization: Bearer $MAIN_API_TOKEN"
```

**Response:**
```json
{
  "message": "Model, day_time_interactions, and coordinates reloaded"
}
```

**Notes:**
- Must be called after training completes to use new model and profiles
- Reloads [`model.pkl`](model.pkl), `day_time_interactions.json`, and coordinates lookup
- Updates `reloaded` timestamp in [`status.json`](status.json)
- Returns 500 if [`model.pkl`](model.pkl) doesn't exist

---

### Profiling

#### `POST /profile`
Get ranked time-window recommendations for a patient. Distance is calculated server-side from address and facility name.

**Request:**
```bash
curl -X POST http://localhost:8000/profile \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 5,
    "patient_address": "Rīga",
    "hospital_id": 1
  }'
```

**Response:**
```json
{
  "profile_key": "toddler_0-10km",
  "distance_km": 9.113,
  "recommendations": [
    {"day_of_week": 3, "day_name": "Wed", "window": "8-9", "hours": [8, 9], "rate": 0.743, "tier": 1, "tier_ci": 1, "rank": 1, "n": 5830, "attended": 4333, "ci_lower": 0.731, "ci_upper": 0.755, "low_sample": false, "lift": 0.025, "composite_score": 5.12},
    {"day_of_week": 3, "day_name": "Wed", "window": "12-13", "hours": [12, 13], "rate": 0.731, "tier": 1, "tier_ci": 1, "rank": 2, "n": 5796, "attended": 4236, "ci_lower": 0.719, "ci_upper": 0.743, "low_sample": false, "lift": 0.013, "composite_score": 4.98}
  ]
}
```

**Notes:**
- Returns per-day × time-window cells from `day_time_interactions.json` (matching PROFILES.md)
- Distance is calculated server-side from `patient_address` + `hospital_id` (mapped to facility name internally)
- Uses in-memory coordinates lookup → Nominatim fallback → same-city heuristic
- Returns 503 if day_time_interactions not loaded
- Returns empty recommendations list if no matching profile is found

---

#### `GET /profiles`
Return all discovered profiles and metadata (admin/debug).

**Request:**
```bash
curl http://localhost:8000/profiles \
  -H "Authorization: Bearer $MAIN_API_TOKEN"
```

**Notes:**
- Returns all profiles and metadata from `day_time_interactions.json`
- Returns 503 if day_time_interactions not loaded

---

### Predictions

#### `POST /predict`
Make a prediction using the loaded model.

**Request:**
```bash
curl -X POST http://localhost:8000/predict \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "appointment_date": "2025-02-28",
    "appointment_date_registration": "2025-04-01",
    "chronic_patient": false,
    "patient_birth_year": 1990,
    "distance_to_appointment_km": 12.5,
    "appointment_time_hour": 14,
    "appointment_type": "regular",
    "doctor_type_id": 3,
    "service": "endokrinologs",
    "is_repeated_patient": 0,
    "past_appointments_count": 0,
    "past_missed_appointments": 0
  }'
```

**Response:**
```json
{
  "prediction": 1,
  "prediction_score": 0.85
}
```

- `prediction`: `0` (no-show) or `1` (show)
- `prediction_score`: Confidence score (0.0 to 1.0)

**Notes:**
- Returns 503 if model not loaded (call `POST /reload` first)

---

## Typical Workflow

```bash
# 1. Start training (ensure both training.csv and profiling_input.csv are in the shared volume)
curl -X POST http://localhost:8000/train \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"csv_path": "training.csv", "target_column": "appointment_attended"}'

# 2. Monitor progress (poll until "done" is set)
curl http://localhost:8000/jobs \
  -H "Authorization: Bearer $MAIN_API_TOKEN"

# 3. Reload model and profiles into memory
curl -X POST http://localhost:8000/reload \
  -H "Authorization: Bearer $MAIN_API_TOKEN"

# 4. Make predictions
curl -X POST http://localhost:8000/predict \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ ... }'

# 5. Get profiling recommendations for a patient (distance calculated server-side)
curl -X POST http://localhost:8000/profile \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 5,
    "patient_address": "Rīga",
    "hospital_id": 1
  }'
```

---

## Worker Process

[`worker.py`](worker.py) runs continuously in the background:

```python
while True:
    status = read_status()
    if job_pending and not locked:
        acquire_lock()
        load_csv()
        preprocess()
        geocode_unknown_addresses()   # Nominatim API → coordinates SQL cache
        enrich_distances()            # haversine from coordinates lookup
        feature_engineering()
        train_model()
        save_model()
        geocode_unknown_addresses()  # geocode new patient addresses via Nominatim
        run_profiling_pipeline()     # fault-isolated, non-fatal on failure
        update_status(done=True)
        release_lock()
    sleep(60)  # Poll every 60 seconds
```

**Environment Variables (worker):**
- `MODEL_DIR`: Directory for model files (default: `/app`)
- `MODEL_BASENAME`: Model filename without extension (default: `model`)
- `POLL_INTERVAL`: Polling interval in seconds (default: `60.0`)
- `STATUS_PATH`: Path to status.json (default: `/app/status.json`)

**Environment Variables (authentication):**
- `MAIN_API_TOKEN`: Bearer token for main API (required)
- `DISTANCE_API_TOKEN`: Bearer token for distance service (required)

### Distance Enrichment Flow

During training, the worker fills NULL `distance_to_appointment_km` values in two steps:

1. **Geocode** ([`shared/distance_enrichment.py`](shared/distance_enrichment.py) → [`geocode_unknown_addresses()`](shared/distance_enrichment.py)):
   - Finds patient addresses with NULL distance that aren't in the coordinates SQL file
   - Geocodes them via OpenStreetMap Nominatim (1 req/sec rate limit, 4-strategy fallback for Latvian addresses)
   - Appends successful results to `latvian_places_with_coordinates_v2.sql` (persistent cache)

2. **Enrich** ([`shared/distance_enrichment.py`](shared/distance_enrichment.py) → [`enrich_distances()`](shared/distance_enrichment.py)):
   - Re-reads the coordinates SQL file (now including newly geocoded entries)
   - Resolves patient addresses via 4-level lookup (direct, city extraction, aggressive normalization, diacritics-stripped)
   - Computes haversine distance to the appointment facility
   - Fills NULL distances in the in-memory DataFrame (the CSV on disk is never modified)

The coordinates SQL file grows over time, so subsequent training runs resolve more addresses from cache without Nominatim calls.

---

## Deployment

The system is designed for containerized deployment:

- **Container 1**: Runs [`app.py`](app.py) (FastAPI server)
- **Container 2**: Runs [`worker.py`](worker.py) (background worker)
- **Container 3**: Runs [`distance_service.py`](distance_service.py) (distance API on port 8001)
- **Shared Volume**: Both mount `/app` containing:
  - [`status.json`](status.json) - job coordination
  - [`model.pkl`](model.pkl) - trained model
  - `day_time_interactions.json`
  - `latvian_places_with_coordinates_v2.sql` - coordinates cache (read by app, written by worker)
  - `training.csv` - training data

See [`Dockerfile`](Dockerfile), [`Dockerfile.worker`](Dockerfile.worker), [`Dockerfile.distance`](Dockerfile.distance), and [`docker-compose.yml`](docker-compose.yml) for container configurations.

## Testing

Run the test suite from the `prediction_model/src/api/` directory:

```bash
python -m pytest tests/ -v
```

Tests cover:
- `shared/coordinates.py` - diacritics stripping, city extraction, SQL parsing, address resolution, haversine, distance calculation
- `shared/distance_enrichment.py` - NULL distance filling, geocoding flow, cache persistence
- `app.py` - `/reload` endpoint coordinates loading and refresh
- Worker integration - full geocode → enrich → train flow

### Profiling Tests

Run the full profiling test plan (unit tests + pipeline on real data):

```bash
python prediction_model/scripts/run_profiling_tests.py
```

Tests cover 7 modules:
- `data_loader` - CSV loading, type coercion, validation
- `feature_engineering` - age groups, distance groups, city extraction
- `pattern_discovery` - attendance rates, DT/RF models, Wilson CI, tier assignment (CI-overlap), day × time interactions
- `profile_generator` - profile structure, tier/CI field passthrough
- `report_generator` - ML vs manual comparisons
- `specialty_analysis` - per-specialty working hours and patient counts
- `pipeline_integration` - end-to-end pipeline, artifact generation including `day_time_interactions.json`
