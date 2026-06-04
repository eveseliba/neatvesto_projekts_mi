"""Pydantic model for the /health endpoint."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    scheduling_db: str
    pool_size: int | None = None
    pool_available: int | None = None
    error: str | None = None
