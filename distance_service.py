"""Distance calculation microservice.

Thin HTTP wrapper around shared.coordinates providing:
  POST /distance  – calculate distance from patient address to hospital
  GET  /health    – health check
"""

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import uvicorn

from shared.coordinates import (
    parse_coordinates_sql,
    build_lookup,
    calculate_distance,
    geocode_nominatim,
    haversine,
    FACILITY_COORDS,
    HOSPITAL_MAP,
    default_sql_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_TOKEN = os.getenv("DISTANCE_API_TOKEN", "")
MODEL_DIR = os.getenv("MODEL_DIR", "/app")
COORDS_SQL_PATH = os.path.join(MODEL_DIR, "latvian_places_with_coordinates_v2.sql")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


def verify_token(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if not API_TOKEN:
        raise HTTPException(status_code=500, detail="API token not configured")
    if creds.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return creds.credentials


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

coords_lookup: dict[str, tuple[float, float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_coordinates()
    yield


app = FastAPI(title="BKUS Distance Service", lifespan=lifespan)


class DistanceRequest(BaseModel):
    patient_address: str
    hospital_id: int


class DistanceResponse(BaseModel):
    distance_km: float
    hospital_id: int
    hospital_name: str


def _load_coordinates():
    global coords_lookup
    sql_path = COORDS_SQL_PATH if os.path.exists(COORDS_SQL_PATH) else default_sql_path()
    if sql_path and os.path.exists(sql_path):
        raw = parse_coordinates_sql(sql_path)
        coords_lookup = build_lookup(raw)
        logger.info("Loaded %d coordinate entries from %s", len(coords_lookup), sql_path)
    else:
        logger.warning("Coordinates SQL not found, distance lookup will be limited")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/distance", response_model=DistanceResponse)
def distance(req: DistanceRequest, _token: str = Depends(verify_token)):
    # Validate hospital_id
    facility_name = HOSPITAL_MAP.get(req.hospital_id)
    if facility_name is None:
        valid_ids = sorted(HOSPITAL_MAP.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown hospital_id: {req.hospital_id}. Valid IDs: {valid_ids}",
        )

    # Calculate distance using the shared module
    dist = calculate_distance(req.patient_address, facility_name, coords_lookup)

    # Nominatim fallback for unknown addresses
    if dist is None:
        nominatim_coords = geocode_nominatim(req.patient_address)
        if nominatim_coords is not None:
            fac = FACILITY_COORDS[facility_name]
            dist = round(
                haversine(nominatim_coords[0], nominatim_coords[1], fac[0], fac[1]),
                3,
            )

    if dist is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not resolve address: '{req.patient_address}'",
        )

    return DistanceResponse(
        distance_km=dist,
        hospital_id=req.hospital_id,
        hospital_name=facility_name,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
