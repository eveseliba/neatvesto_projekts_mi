"""Pydantic request models for the /schedule/multi endpoint."""

from pydantic import BaseModel, field_validator

from scheduling.validators import _validate_excluded_dates, _coerce_appointment_type


class MultiServiceItem(BaseModel):
    service_id: int
    specialist_id: int | None = None


class MultiScheduleRequest(BaseModel):
    patient_id: int | None = None
    patient_age: int
    patient_address: str
    hospital_id: int = 1
    slot_filter: str = "free"
    reception_mode: str | list[str] | None = None
    appointment_type: list[int] | None = None

    @field_validator("appointment_type", mode="before")
    @classmethod
    def coerce_appointment_type(cls, v):
        return _coerce_appointment_type(v)
    paid_visit: bool | None = None
    services: list[MultiServiceItem]
    excluded_dates: list[str] = []

    @field_validator("services")
    @classmethod
    def services_length(cls, v):
        if len(v) < 1 or len(v) > 5:
            raise ValueError("services must contain 1-5 entries")
        return v

    @field_validator("slot_filter")
    @classmethod
    def valid_slot_filter(cls, v):
        if v not in ("free", "free_and_reserved"):
            raise ValueError("slot_filter must be 'free' or 'free_and_reserved'")
        return v

    @field_validator("excluded_dates")
    @classmethod
    def valid_excluded_dates(cls, v):
        return _validate_excluded_dates(v)
