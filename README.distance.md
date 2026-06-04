# Distance Calculation Service

Microservice that calculates the distance (km) between a patient address and a BKUS hospital. Uses the shared [`coordinates.py`](../shared/coordinates.py) module for address resolution and haversine calculation.

---

## Setup

### Docker (recommended)

1. Set the API token in your environment or `.env` file:

```bash
export DISTANCE_API_TOKEN=your-secret-token
```

2. Build and start the service:

```bash
docker-compose build distance
docker-compose up distance
```

The service starts on port **8001**.

### Local development

```bash
cd prediction_model/src/api
pip install -r requirements.distance.txt
DISTANCE_API_TOKEN=your-secret-token python distance_service.py
```

---

## API

### `GET /health`

Health check (no authentication required).

```bash
curl http://localhost:8001/health
```

```json
{"status": "ok"}
```

### `POST /distance`

Calculate distance from a patient address to a hospital. Requires Bearer token.

**Request:**

```bash
curl -X POST http://localhost:8001/distance \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_address": "Rīga",
    "hospital_id": 1
  }'
```

**Parameters:**

| Field             | Type    | Description                   |
| ----------------- | ------- | ----------------------------- |
| `patient_address` | string  | Patient's address in Latvia   |
| `hospital_id`     | integer | Hospital identifier (see below) |

**Response (200):**

```json
{
  "distance_km": 3.456,
  "hospital_id": 1,
  "hospital_name": "Torņakalns"
}
```

**Error responses:**

| Status | Cause                              |
| ------ | ---------------------------------- |
| 401    | Missing or invalid Bearer token    |
| 400    | Unknown `hospital_id`              |
| 404    | Address could not be resolved      |
| 422    | Missing or invalid request fields  |
| 500    | `DISTANCE_API_TOKEN` env var not set |

---

## Supported Hospitals

| ID | Name                       |
| -- | -------------------------- |
| 1  | Torņakalns                 |
| 2  | Gaiļezers                  |
| 4  | Rehabilitācija Gaiļezerā   |
| 6  | Daugavpils                 |
| 8  | Valmiera                   |

---

## How It Works

1. Maps `hospital_id` to facility name and coordinates
2. Resolves patient address to coordinates via multi-level lookup (direct match, city extraction, normalization, diacritics-stripped)
3. If not found in the local cache, falls back to OpenStreetMap Nominatim geocoding (adds 1-4s latency)
4. Calculates great-circle distance using the haversine formula

The address lookup is loaded at startup from the shared `latvian_places_with_coordinates_v2.sql` file (same file used by the training pipeline).

---

## Environment Variables

| Variable              | Default  | Description                                    |
| --------------------- | -------- | ---------------------------------------------- |
| `DISTANCE_API_TOKEN`  | *(none)* | **Required.** Bearer token for authentication  |
| `MODEL_DIR`           | `/app`   | Directory containing the coordinates SQL file  |

---

## Testing

```bash
cd prediction_model/src/api
python -m pytest tests/test_distance_health.py tests/test_distance_auth.py tests/test_distance_hospital_id.py tests/test_distance_calc.py tests/test_distance_validation.py -v
```
