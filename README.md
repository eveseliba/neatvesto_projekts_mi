# Project Documentation

This document describes the components, expected inputs, and outputs of the FastAPI-based prediction service and its Docker setup.

---

## Overview

The project provides a RESTful API for predicting patient appointment outcomes, scheduling appointments, and patient profiling. It includes:

* **app.py**: FastAPI application defining prediction, scheduling, and profiling endpoints.
* **worker.py**: Background process that trains models and enriches distances.
* **distance_service.py**: Standalone distance calculation microservice (see [README.distance.md](README.distance.md)).
* **scheduling/**: Appointment scheduling module — slot querying, profile-based scoring, multi-service combination scheduling (see [README.scheduling.md](README.scheduling.md)).
* **shared/**: Shared modules for distance calculation, coordinate resolution, and patient profile resolution.
* **model.pkl**: Serialized PyCaret model pipeline.
* **requirements.txt**: Python dependencies.
* **Dockerfile**: Instructions to build a Docker image for deployment.

---

## Authentication

All main API endpoints (except `/health`) require a Bearer token. Set the `MAIN_API_TOKEN` environment variable and include the token in every request:

```
Authorization: Bearer <your-token>
```

The distance service uses a **separate** token (`DISTANCE_API_TOKEN`). See [README.distance.md](README.distance.md).

| Variable           | Service  | Description                    |
| ------------------ | -------- | ------------------------------ |
| `MAIN_API_TOKEN`   | api      | Bearer token for main API      |
| `DISTANCE_API_TOKEN` | distance | Bearer token for distance API |

---

## Files

### app.py

`app.py` initializes a FastAPI server and exposes the following endpoints:

```python
POST /predict
POST /train
GET  /jobs
POST /reload
POST /profile
GET  /profiles
POST /schedule
POST /schedule/multi
GET  /health
```

#### Description

1. **Model loading**: On startup, loads the serialized model `model.pkl` via PyCaret’s `load_model`.
2. **Input validation**: Uses Pydantic’s `PredictionFeature` schema to enforce and parse incoming JSON.
3. **Prediction**: Converts the input to a pandas DataFrame, calls PyCaret’s `predict_model`, and extracts the predicted label and score.
4. **Response**: Returns a JSON object containing the prediction and its confidence score.

## Endpoints

### 1. **POST** `/predict`

## Request

**Headers:**
`Content-Type: application/json`
`Authorization: Bearer <token>`

**Body parameters (`PredictionFeature`):**

| Field                           | Type    | Format / Values        | Description                                                                               |
| ------------------------------- | ------- | ---------------------- | ----------------------------------------------------------------------------------------- |
| `appointment_date`              | string  | `"YYYY-MM-DD"`         | Date of the scheduled appointment                                                         |
| `appointment_date_registration` | string  | `"YYYY-MM-DD"`         | Date when the appointment was registered                                                  |
| `chronic_patient`               | boolean | `true` / `false`       | Whether the patient has an entry in 'hroniskie' table                                     |
| `patient_birth_year`            | integer | ≥ 1980 or `null`       | Year of birth; values before 1980 are converted to `null`                                 |
| `distance_to_appointment_km`    | float   |                        | Distance from patient to appointment location in kilometers (see note below)              |
| `appointment_time_hour`         | integer | `0`–`23`               | Hour of the appointment                                                                   |
| `appointment_type`              | string  | e.g. `"regular"`       | Type of the appointment ('vizites_veids')                                                 |
| `doctor_type_id`                | integer |                        | Identifier for the doctor's specialty ('specialitates_id')                                |
| `service`                       | string  | e.g., `"endokrinologs"`| Service or specialty ('pakalpojums')                                                      |
| `is_repeated_patient`           | integer | `0` or `1`             | Whether the patient has had previous appointments with the same doctor (`1`) or not (`0`) |
| `past_appointments_count`       | integer |                        | Total number of past appointments                                                         |
| `past_missed_appointments`      | integer |                        | Total number of past missed appointments                                                  |

*when distance data not available, then you can use avarage distance_to_appointment_km = 41.854*


#### Example Request

```bash
curl -X POST "http://localhost:8000/predict" \
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

#### Example Response

```json
{
  "prediction": 1,
  "prediction_score": 0.85
}
```

* `prediction`: `0` (no-show) or `1` (show).
* `prediction_score`: Probability score of the prediction (how confident the model is with it's prediction), between `0.0` and `1.0`.

---

### 2. **POST** `/train`

#### Description

Initiates a new model training job. The training is performed asynchronously by the worker service. Only one training job can be active at a time.

#### Request

**Headers:**
`Content-Type: application/json`

**Body parameters (`TrainRequest`):**

| Field           | Type   | Required | Description                                      |
| --------------- | ------ | -------- | ------------------------------------------------ |
| `csv_path`      | string | optional | Path to the training CSV file                    |
| `target_column` | string | optional | Name of the target column in the training data   |

#### Example Request

```bash
curl -X POST "http://localhost:8000/train" \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "csv_path": "/shared/training.csv",
    "target_column": "target"
  }'
```

#### Example Response

```json
{
  "message": "Training started",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

#### Status Codes

* **202 Accepted**: Training job successfully queued
* **409 Conflict**: A training job is already in progress

#### Training Data Format

The training CSV file should contain historical appointment data with the following columns. The format should match the structure shown in [`input_data.sql`](../../../resources/data/input/input_data.sql) and [`training.csv`](training.csv).

**Required columns:**

| Column                           | Type    | Format / Values        | Description                                                         |
| -------------------------------- | ------- |------------------------|---------------------------------------------------------------------|
| `appointment_attended`           | integer | `0` or `1`             | **Target variable**: Whether patient attended (`1`) or missed (`0`) |
| `patient_id`                     | integer |                        | Unique patient identifier                                           |
| `chronic_patient`                | integer | `0` or `1`             | Whether patient is registered in chronic patients table             |
| `patient_birth_year`             | integer |                        | Patient's birth year;                                               |
| `patient_address`                | string  |                        | Patient's registered address (`pacienta_dzivesvieta`), used for distance enrichment |
| `facility_name`                  | string  |                        | Appointment facility name (`novietne`), used for distance enrichment |
| `distance_to_appointment_km`     | float   |                        | Distance from patient location to appointment location (km); NULLs filled during training |
| `appointment_date_registration`  | string  | `"YYYY-MM-DD"`         | Date when appointment was registered                                |
| `appointment_date`               | string  | `"YYYY-MM-DD"`         | Date of the scheduled appointment                                   |
| `appointment_time_hour`          | integer | `0`–`23`               | Hour of the appointment (derived from appointment time)             |
| `appointment_type`               | string  | e.g., `"zalais laiks"` | Type of appointment ('vizites_veids' from source data)              |
| `doctor_id`                      | integer |                        | Unique identifier for the doctor                                    |
| `doctor_type_id`                 | integer |                        | Doctor's specialty identifier ('specialitates_id')                  |
| `service`                        | string  | e.g., `"pediatrs"`     | Service or specialty name ('pakalpojums')                               |
| `service_type_id`                | string  |                        | Service type identifier ('pakalpojuma_sveids')                      |
| `appointment_status`             | integer |                        | Status code of the appointment                                      |
| `appointment_approved`           | integer | `0` or `1`             | Whether appointment was confirmed via robot call                    |


**Example CSV format:**

```csv
appointment_attended,patient_id,chronic_patient,patient_birth_year,patient_address,facility_name,distance_to_appointment_km,appointment_date_registration,appointment_date,appointment_time_hour,appointment_type,doctor_id,doctor_type_id,service,service_type_id,appointment_status,appointment_approved
1,297807,0,2012,Rīga,Torņakalns,9.113,2023-01-01,2023-01-01,18,hronisks pacients,205819,181,pediatrs,0001,2,0
0,58091,0,2005,Liepāja,Gaiļezers,,2022-12-30,2023-01-02,9,zalais laiks,206178,131,RTG rentgens,0001,0,0
1,415244,0,2015,Jelgava,Torņakalns,28.321,2023-01-01,2023-01-01,12,attalinata konsultacija,205819,32,pediatrs,0001,2,0
```

**Additional columns used for distance enrichment (optional but recommended):**

| Column               | Type   | Description                                             |
| -------------------- | ------ | ------------------------------------------------------- |
| `patient_address`    | string | Patient's registered address (e.g., `"Rīga"`)          |
| `facility_name`      | string | Appointment facility name (e.g., `"Torņakalns"`)       |

**Important notes:**

* The CSV must include the **target variable** `appointment_attended` as the first column
* The training pipeline will automatically generate derived features (time-based features, patient age, holiday indicators, etc.) from these base columns
* **Distance enrichment**: If `distance_to_appointment_km` contains NULL values, the worker will automatically attempt to fill them during training:
  1. Unknown patient addresses are geocoded via OpenStreetMap Nominatim API and cached in the coordinates SQL file (`latvian_places_with_coordinates_v2.sql`)
  2. NULL distances are then computed using haversine formula from the coordinates lookup
  3. The coordinates SQL file grows over time as new addresses are discovered, so subsequent training runs resolve addresses faster without repeated API calls
* The `patient_address` and `facility_name` columns are required for distance enrichment to work. If they are absent, enrichment is skipped and existing `distance_to_appointment_km` values are used as-is
* **Profiling data:** The training pipeline also runs patient profiling (step 7) which requires a separate file `profiling_input.csv` in the same directory as `training.csv`. This file is generated from `profiling_input.sql` (see [Patient Profiling System](#patient-profiling-system) for details). If `profiling_input.csv` is not present, profiling is skipped but the prediction model is still saved
* Data source for the original model training the following sql was used:

```sql
WITH vizites AS (
	SELECT * FROM bkus.vizites_un_dzestas_23_24
	UNION ALL
	SELECT * FROM bkus.vizites_un_dzestas_25
)
SELECT v.pacients_ir_ieradies AS appointment_attended
	, v.pacienta_id AS patient_id
	, ( CASE
			WHEN v.pacienta_id IN (
				SELECT h.pacienta_id
				FROM bkus.hroniskie h
			) THEN 1
			ELSE 0
		END ) AS chronic_patient
	, CASE
		WHEN v.dzimsanas_gads IS NOT NULL AND v.dzimsanas_gads != 'NULL' THEN EXTRACT(YEAR FROM CAST(v.dzimsanas_gads AS TIMESTAMP))
		ELSE NULL
		END AS patient_birth_year
	, v.pacienta_dzivesvieta AS patient_address
	, v.novietne AS facility_name
	, (	CASE
		  WHEN n.name IS NULL THEN NULL
		  WHEN lpwcv.name IS NULL THEN NULL
		  ELSE (
		         ST_Distance(
		           ST_SetSRID(ST_Point(n.longitude, n.latitude), 4326)::geography,
		           ST_SetSRID(ST_Point(lpwcv.longitude, lpwcv.latitude), 4326)::geography
		         ) / 1000
		       )::numeric(10,3)
		END ) AS distance_to_appointment_km
	, v.pieraksta_datums AS appointment_date_registration
	, v.vizites_datums AS appointment_date
	, (v.sakuma_laiks_int - 1) / 60 AS appointment_time_hour
	, v.vizites_veids AS appointment_type
	, v.arsta_id AS doctor_id
	, v.specialitates_id AS doctor_type_id
	, v.pakalpojums AS service
	, v.pakalpojuma_sveids AS service_type_id
	, v.vizites_statuss AS appointment_status
	, CASE WHEN v.appointment_id IN (
				SELECT rz.appointment_id
				FROM bkus.robota_zvani rz
				WHERE rz.robota_atbilde = 'Apstiprināja'
			) THEN 1
			ELSE 0
		END AS appointment_approved
FROM vizites v
    LEFT JOIN bkus.latvian_places_with_coordinates_v2 lpwcv ON lpwcv.name = v.pacienta_dzivesvieta
    LEFT JOIN bkus.novietne n ON n.name = v.novietne
WHERE spec_nosaukums NOT LIKE '%port%' --exclude every 'sport'
    AND v.pieraksta_datums <= v.vizites_datums
ORDER BY v.vizites_datums, v.pacienta_id ASC;

```

---

### 3. **GET** `/jobs`

#### Description

Returns the current status of training jobs, including job state, progress, and any errors.

#### Request

No request body required.

#### Example Request

```bash
curl -X GET "http://localhost:8000/jobs" \
  -H "Authorization: Bearer $MAIN_API_TOKEN"
```

#### Example Response

```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "csv_path": "/shared/training.csv",
  "target": "target",
  "processing": "2025-12-02T12:00:00.000Z",
  "done": "2025-12-02T12:15:30.000Z",
  "reloaded": "2025-12-02T12:15:35.000Z",
  "stage": "completed",
  "error": null,
  "claimed_by": "worker-1"
}
```

#### Response Fields

| Field         | Type   | Description                                           |
| ------------- | ------ | ----------------------------------------------------- |
| `job_id`      | string | Unique identifier for the training job                |
| `csv_path`    | string | Path to the training data file                        |
| `target`      | string | Target column name                                    |
| `processing`  | string | ISO timestamp when job started processing             |
| `done`        | string | ISO timestamp when job completed (null if not done)   |
| `reloaded`    | string | ISO timestamp when model was reloaded (null if not)   |
| `stage`       | string | Current stage (queued, training, completed, etc.)     |
| `error`       | string | Error message if job failed (null if no error)        |
| `claimed_by`  | string | Identifier of the worker processing the job           |

---

### 4. **POST** `/reload`

#### Description

Reloads the model **and coordinates lookup** from disk. This is typically called after training completes, but can be invoked manually if needed. The coordinates lookup is used internally for distance resolution.

#### Request

No request body required.

#### Example Request

```bash
curl -X POST "http://localhost:8000/reload" \
  -H "Authorization: Bearer $MAIN_API_TOKEN"
```

#### Example Response

```json
{
  "message": "Model and coordinates reloaded"
}
```

#### Status Codes

* **200 OK**: Model, day_time_interactions, and coordinates successfully reloaded
* **500 Internal Server Error**: Failed to reload (e.g., model file not found or corrupted)

---

### 5. **POST** `/profile`

#### Description

Returns ranked time-window recommendations for scheduling a patient's appointment. Distance is calculated **server-side** from the patient address and facility name using an in-memory coordinates lookup (~700 Latvian places). If the address is unknown, the API calls OpenStreetMap Nominatim in real-time and caches the result.

#### Request

**Headers:**
`Content-Type: application/json`

**Body parameters (`ProfileRequest`):**

| Field              | Type    | Required | Description                                                                                       |
| ------------------ | ------- | -------- | ------------------------------------------------------------------------------------------------- |
| `patient_age`      | integer | yes      | Patient's age in years                                                                            |
| `patient_address`  | string  | yes      | Patient's address (city/district)                                                                 |
| `hospital_id`      | integer | yes      | Hospital facility ID (1=Torņakalns, 2=Gaiļezers, 4=Rehabilitācija Gaiļezerā, 6=Daugavpils, 8=Valmiera) |

**Distance calculation:** The API calculates distance automatically using:
1. In-memory coordinates lookup (parsed from `latvian_places_with_coordinates_v2.sql` at startup)
2. Nominatim geocoding fallback for unknown addresses (~1s, result cached)
3. Same-city heuristic fallback: same city → `0-10km`, otherwise → `30-100km`

#### Example Request

```bash
curl -X POST "http://localhost:8000/profile" \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 5,
    "patient_address": "Rīga",
    "hospital_id": 1
  }'
```

#### Example Response

```json
{
  "profile_key": "toddler_0-10km",
  "distance_km": 9.113,
  "recommendations": [
    {
      "day_of_week": 3,
      "day_name": "Wed",
      "window": "8-9",
      "hours": [8, 9],
      "rate": 0.743,
      "ci_lower": 0.731,
      "ci_upper": 0.755,
      "composite_score": 5.12,
      "n": 5830,
      "attended": 4333,
      "low_sample": false,
      "lift": 0.025,
      "tier_ci": 1,
      "rank": 1,
      "tier": 1
    },
    {
      "day_of_week": 3,
      "day_name": "Wed",
      "window": "12-13",
      "hours": [12, 13],
      "rate": 0.731,
      "ci_lower": 0.719,
      "ci_upper": 0.743,
      "composite_score": 4.98,
      "n": 5796,
      "attended": 4236,
      "low_sample": false,
      "lift": 0.013,
      "tier_ci": 1,
      "rank": 2,
      "tier": 1
    }
  ]
}
```

**Response fields:**

| Field              | Type   | Description                                                                 |
| ------------------ | ------ | --------------------------------------------------------------------------- |
| `profile_key`      | string | Profile key (e.g. `"toddler_0-10km"`)                                      |
| `distance_km`      | float  | Calculated distance from patient address to hospital (km), or `null` if unknown |
| `recommendations`  | array  | Per-day × time-window cells from `day_time_interactions.json`, ranked by attendance rate (matching PROFILES.md) |

**Note:** Each recommendation is a specific day × time-window cell (e.g., Wed 8-9) with tier, rate, CI, and sample size.

#### Status Codes

* **200 OK**: Profile found and recommendations returned
* **503 Service Unavailable**: Day-time interactions not loaded (run training pipeline first)

---

### 6. **GET** `/profiles`

#### Description

Returns all day-time interaction profiles and pipeline metadata. Useful for admin/debugging purposes.

#### Example Request

```bash
curl -X GET "http://localhost:8000/profiles" \
  -H "Authorization: Bearer $MAIN_API_TOKEN"
```

#### Example Response

```json
{
  "profiles": {
    "toddler_0-10km": {
      "age_group": "0-6",
      "distance_group": "0-10km",
      "recommendations": [...]
    },
    "school_age_30-100km": {
      "age_group": "7-18",
      "distance_group": "30-100km",
      "recommendations": [...]
    }
  },
  "metadata": {
    "trained_at": "2026-02-13T10:00:00Z",
    "total_records_used": 450000,
    "distance_feature_used": "distance_group",
    "model_accuracy": {
      "decision_tree": { "cv_auc": 0.62 },
      "random_forest": { "cv_auc": 0.65 }
    }
  }
}
```

#### Status Codes

* **200 OK**: Day-time interaction profiles returned
* **503 Service Unavailable**: Day-time interactions not loaded

---

### 7. **POST** `/schedule`

#### Description

Returns the best available appointment slots for a single service, ranked by a combination of patient profile matching and historical attendance probability. Requires a connection to the scheduling database (`saule.vizisu_laiki`).

For full scheduling documentation including the scoring algorithm, multi-service scheduling, and configuration, see [README.scheduling.md](README.scheduling.md).

#### Request

**Headers:**
`Content-Type: application/json`

**Body parameters (`ScheduleRequest`):**

| Field              | Type      | Required | Default            | Description                                                              |
| ------------------ | --------- | -------- | ------------------ | ------------------------------------------------------------------------ |
| `patient_age`      | integer   | yes      |                    | Patient's age in years                                                   |
| `patient_address`  | string    | yes      |                    | Patient's address (city/district) for distance-based profile resolution  |
| `hospital_id`      | integer   | no       | `1`                | Hospital facility ID (1=Torņakalns, 2=Gaiļezers, 4=Rehab, 6=Daugavpils, 8=Valmiera) |
| `service_id`       | integer   | cond.    |                    | Service ID (at least one of `service_id` or `specialist_id` required)    |
| `specialist_id`    | integer   | cond.    |                    | Specialist ID                                                            |
| `patient_id`       | integer   | no       |                    | Patient ID for attendance history lookup                                 |
| `appointment_type` | integer   | no       |                    | Appointment type code filter                                             |
| `slot_filter`      | string    | no       | `"free"`           | `"free"` or `"free_and_reserved"`                                        |
| `reception_mode`  | string \| list[string] | no | `"0001"` | Reception mode code (`pakalpojuma_sveids`). Accepts a single code or a list. Both zero-padded (`"0002"`) and bare (`"2"`) formats are expanded automatically. Default is `"0001"` (ambulatory). Known values: `"0001"`, `"0002"`, `"0003"`, `"0004"`. |
| `paid_visit`       | boolean   | no       |                    | Filter by paid/free visits                                               |
| `excluded_dates`   | list[str] | no       | `[]`               | Dates to skip (format: `"YYYY-MM-DD"`)                                   |

#### Example Request

```bash
curl -X POST "http://localhost:8000/schedule" \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 5,
    "patient_address": "Rīga",
    "hospital_id": 1,
    "service_id": 138
  }'
```

#### Example Response

```json
{
  "patient_profile": "toddler_0-10km",
  "results": [
    {
      "service_id": 138,
      "specialist_id": null,
      "earliest": {
        "slots": [{"slot_id": 1001, "specialist_id": 205}],
        "date": "2026-03-05",
        "start_time": 540,
        "end_time": 560,
        "start_time_formatted": "09:00",
        "end_time_formatted": "09:20",
        "specialty_id": 181,
        "hospital_id": 1,
        "service_id": 138,
        "appointment_type": null,
        "score": null,
        "profile_match": false
      },
      "recommended": [
        {
          "slots": [{"slot_id": 1015, "specialist_id": 205}],
          "date": "2026-03-07",
          "start_time": 480,
          "end_time": 500,
          "start_time_formatted": "08:00",
          "end_time_formatted": "08:20",
          "specialty_id": 181,
          "hospital_id": 1,
          "service_id": 138,
          "appointment_type": null,
          "score": 0.72,
          "profile_match": true,
          "profile_tier_level": 1,
          "profile_window_rate": 0.743
        }
      ],
      "total_slots_evaluated": 85,
      "tier_used": 1,
      "profile_match": true,
      "profile_match_reason": "Tier 1 match (Fri 08:00-09:00, rate 0.743)"
    }
  ]
}
```

#### Status Codes

* **200 OK**: Slots found and scored
* **503 Service Unavailable**: Scheduling database not connected

---

### 8. **POST** `/schedule/multi`

#### Description

Finds optimal appointment plans for multiple services (1-5), combining them into as few visits as possible. Returns up to 3 ranked plans plus the earliest available slot per service.

For full scheduling documentation see [README.scheduling.md](README.scheduling.md).

#### Request

**Headers:**
`Content-Type: application/json`

**Body parameters (`MultiScheduleRequest`):**

| Field              | Type                | Required | Default  | Description                                                              |
| ------------------ | ------------------- | -------- | -------- | ------------------------------------------------------------------------ |
| `patient_age`      | integer             | yes      |          | Patient's age in years                                                   |
| `patient_address`  | string              | yes      |          | Patient's address for profile resolution                                 |
| `hospital_id`      | integer             | no       | `1`      | Hospital facility ID                                                     |
| `services`         | list[object]        | yes      |          | Array of 1-5 services: `{"service_id": int, "specialist_id": int\|null}` |
| `patient_id`       | integer             | no       |          | Patient ID for attendance history lookup                                 |
| `slot_filter`      | string              | no       | `"free"` | `"free"` or `"free_and_reserved"`                                        |
| `reception_mode`  | string \| list[string] | no    | `"0001"` | Reception mode code (`pakalpojuma_sveids`). Accepts a single code or a list. Both zero-padded (`"0002"`) and bare (`"2"`) formats are expanded automatically. Default is `"0001"` (ambulatory). Known values: `"0001"`, `"0002"`, `"0003"`, `"0004"`. |
| `appointment_type` | integer             | no       |          | Appointment type code filter                                             |
| `paid_visit`       | boolean             | no       |          | Filter by paid (`true`) or free (`false`) visits                         |
| `excluded_dates`   | list[str]           | no       | `[]`     | Dates to skip (format: `"YYYY-MM-DD"`)                                   |

#### Example Request

```bash
curl -X POST "http://localhost:8000/schedule/multi" \
  -H "Authorization: Bearer $MAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_age": 10,
    "patient_address": "Jelgava",
    "hospital_id": 1,
    "services": [
      {"service_id": 138},
      {"service_id": 200},
      {"service_id": 310}
    ]
  }'
```

#### Example Response

```json
{
  "patient_profile": "school_age_10-30km",
  "earliest": [
    {
      "slots": [{"slot_id": 1001, "specialist_id": 205}],
      "date": "2026-03-05",
      "start_time": 540,
      "end_time": 560,
      "start_time_formatted": "09:00",
      "end_time_formatted": "09:20",
      "specialty_id": 181,
      "hospital_id": 1,
      "service_id": 138,
      "appointment_type": null,
      "score": null,
      "profile_match": false
    },
    null,
    null
  ],
  "plans": [
    {
      "rank": 1,
      "score": 2.15,
      "visits": [
        {
          "date": "2026-03-10",
          "slots": [
            {
              "slots": [{"slot_id": 2001, "specialist_id": 301}],
              "date": "2026-03-10",
              "start_time": 480,
              "end_time": 500,
              "start_time_formatted": "08:00",
              "end_time_formatted": "08:20",
              "specialty_id": 181,
              "hospital_id": 1,
              "service_id": 138,
              "appointment_type": null,
              "score": 0.72,
              "profile_match": true,
              "profile_tier_level": 1,
              "profile_window_rate": 0.743
            },
            {
              "slots": [{"slot_id": 2010, "specialist_id": 402}],
              "date": "2026-03-10",
              "start_time": 540,
              "end_time": 560,
              "start_time_formatted": "09:00",
              "end_time_formatted": "09:20",
              "specialty_id": 220,
              "hospital_id": 1,
              "service_id": 200,
              "appointment_type": null,
              "score": 0.68,
              "profile_match": true,
              "profile_tier_level": 1,
              "profile_window_rate": 0.731
            }
          ]
        },
        {
          "date": "2026-03-12",
          "slots": [
            {
              "slots": [{"slot_id": 3005, "specialist_id": 510}],
              "date": "2026-03-12",
              "start_time": 600,
              "end_time": 620,
              "start_time_formatted": "10:00",
              "end_time_formatted": "10:20",
              "specialty_id": 310,
              "hospital_id": 1,
              "service_id": 310,
              "appointment_type": null,
              "score": 0.55,
              "profile_match": true,
              "profile_tier_level": 2,
              "profile_window_rate": 0.695
            }
          ]
        }
      ]
    }
  ]
}
```

Plans are ranked by **fewest visits first**, then by total score descending. Each visit groups all slots on the same date, with a minimum 15-minute gap between appointments.

#### Status Codes

* **200 OK**: Plans generated
* **503 Service Unavailable**: Scheduling database not connected

---

### 9. **GET** `/health`

#### Description

Returns the health status of the API, including scheduling database connectivity.

#### Example Request

```bash
curl -X GET "http://localhost:8000/health"
```

#### Example Response

```json
{
  "status": "healthy",
  "scheduling_db": "connected",
  "pool_size": 10,
  "pool_available": 8
}
```

When the scheduling database is unavailable:

```json
{
  "status": "degraded",
  "scheduling_db": "disconnected",
  "error": "connection refused"
}
```

---

## Patient Profiling System

### Overview

The profiling system recommends optimal appointment time windows based on patient characteristics (age, distance to hospital) and specialty constraints. It answers the question: *"When does this type of patient attend best?"*

Profiles are generated automatically as part of the training pipeline and served via the `/profile` endpoint.

### How Profiles Are Created

Profiling runs as **step 7** of the training pipeline (after model training). It requires a separate dataset (`profiling_input.csv`) generated from `profiling_input.sql`.

**Pipeline phases:**

1. **Data Preparation** — load CSV, geocode unknown patient addresses via Nominatim (results are persisted to the coordinates SQL file), enrich missing distances via haversine calculation, engineer features (age group, distance group, same city)
2. **ML Pattern Discovery** — train Decision Tree and Random Forest models on attendance data, compute SHAP feature importances, extract statistically significant optimal time windows using attendance rate analysis with Wilson-score confidence intervals, assign performance tiers via CI-overlap, analyse day × time interactions (5 days × 5 windows = 25 cells per segment)
3. **Profile Generation** — combine ML results into `day_time_interactions.json` keyed by `{age_group}_{distance_group}` with per-day × time-window tier assignments

**Profiling is fault-isolated**: if it fails, the main prediction model is still saved and usable.

### Segmentation

Patients are segmented along two dimensions:

| Dimension | Groups |
| --------- | ------ |
| Age       | `toddler` (0-6), `school_age` (7-18), `adult` (18+) |
| Distance  | `0-10km`, `10-30km`, `30-100km`, `100+km` |

The ML pipeline determines which distance feature (`distance_group` vs `same_city`) is more predictive via Random Forest feature importance and SHAP analysis.

### Time Windows

Attendance rates are computed across 5 fixed clinical time slots:

| Window           | Hours   |
| ---------------- | ------- |
| Morning          | 8-9     |
| Late morning     | 10-11   |
| Early afternoon  | 12-13   |
| Mid afternoon    | 14-15   |
| Late afternoon   | 16-17   |

Windows are ranked by attendance rate per segment, with low-sample windows (fewer than 30 samples per hour in the window) demoted to the lowest ranks regardless of their rate. Each window includes a `low_sample` boolean flag so API consumers can identify unreliable recommendations. Multiple windows can be recommended (e.g., adults may have two attendance peaks).

### Distance Calculation

Distance is handled at three levels:
- **Batch (SQL):** The SQL query computes distances via PostGIS `ST_Distance` for exact address matches
- **Pipeline (training):** Before distance enrichment, the pipeline automatically geocodes all unknown patient addresses via OpenStreetMap Nominatim and appends the results to the coordinates SQL file. This maximises address coverage for subsequent runs.
- **API runtime:** The `coordinates` module resolves addresses via in-memory lookup (~1000+ locations), with Nominatim geocoding fallback for unknown addresses. Hardcoded BKUS facility coordinates (Torņakalns, Gaiļezers, Valmiera, Daugavpils) are used for the facility side.

### Profiling Data Preparation

The profiling dataset is generated separately from the training dataset:

1. Run `profiling_input.sql` against the PostgreSQL database
2. Export the result as `profiling_input.csv`
3. Place it in the same directory as `training.csv` (locally: `resources/data/training/`, Docker: `/shared/`)

The SQL filters to weekdays (Mon-Fri), working hours (8-17), and valid bookings only.

### Generated Artefacts

| File | Description |
| ---- | ----------- |
| `day_time_interactions.json` | Main profile lookup used by `/profile` and `/schedule` API — per-day × time-window cells (25 per segment) |
| `profiling_metadata.json` | Pipeline metadata (dates, record counts, model accuracy) |
| `profiling_decision_tree.pkl` | Trained Decision Tree model |
| `profiling_random_forest.pkl` | Trained Random Forest model |
| `profiling_report.json` | Validation report comparing ML vs manual research |
| `decision_tree_rules.txt` | Human-readable decision tree rules (saved to `reports/`) |

### Profiling Module Structure

```
prediction_model/src/profiling/
├── __init__.py
├── coordinates.py              # Shared coords, haversine, Nominatim geocoding, batch geocoding
├── data_loader.py              # Load and validate profiling CSV
├── feature_engineering.py      # Age group, same city, distance group features
├── pattern_discovery.py        # Decision Tree, Random Forest, SHAP, attendance rates, tiers, day×time interactions
├── profile_generator.py        # Combine analyses → day_time_interactions.json
├── report_generator.py         # Validation report (ML vs manual research), tier comparison
├── specialty_analysis.py       # Per-specialty working hours and patient counts
└── pipeline.py                 # Orchestrates all steps
```

---

### docker-compose

The `docker-compose.yml` runs three services:

| Service    | Port | Description                              |
| ---------- | ---- | ---------------------------------------- |
| `api`      | 8000 | FastAPI server (predict, schedule, profile) |
| `worker`   | —    | Background model training worker         |
| `distance` | 8001 | Standalone distance calculation microservice |

#### Authentication Environment Variables

| Variable              | Default     | Description                    |
| --------------------- | ----------- | ------------------------------ |
| `MAIN_API_TOKEN`      | *(none)*    | **Required.** Bearer token for main API authentication |
| `DISTANCE_API_TOKEN`  | *(none)*    | **Required.** Bearer token for distance service authentication |

#### Scheduling Database Environment Variables

The scheduling endpoints (`/schedule`, `/schedule/multi`) require a PostgreSQL database. Configure via environment variables:

| Variable                | Default     | Description                    |
| ----------------------- | ----------- | ------------------------------ |
| `SCHEDULING_DB_HOST`    | `localhost` | Database host                  |
| `SCHEDULING_DB_PORT`    | `5432`      | Database port                  |
| `SCHEDULING_DB_NAME`    | `bkus`      | Database name                  |
| `SCHEDULING_DB_USER`    | `postgres`  | Database user                  |
| `SCHEDULING_DB_PASSWORD`| `postgres`  | Database password              |

These are passed through in `docker-compose.yml`. Alternatively, mount a `db_config.json` file (see `db_config.json.template`).

## Dependencies

* **Python** 3.11
* **FastAPI**: Web framework for building APIs.
* **Uvicorn**: ASGI server for running FastAPI apps.
* **PyCaret**: Automated machine learning library.
* **Pandas**: Data manipulation.
* **LightGBM**: Gradient boosting dependency.
* **Requests**: HTTP client for Nominatim geocoding (used by worker during training).
* **Psycopg2**: PostgreSQL adapter (used by scheduling endpoints).

Dependencies are listed in `requirements.txt` and installed in the Docker image.
