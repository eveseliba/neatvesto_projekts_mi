"""Pydantic request models for the /schedule endpoint."""

from pydantic import BaseModel, field_validator, model_validator

from scheduling.validators import _validate_excluded_dates, _validate_at_least_one_identifier, _coerce_appointment_type


class ServiceRequest(BaseModel):
    service_id: int | None = None
    specialist_id: int | None = None
    appointment_type: list[int] | None = None

    @field_validator("appointment_type", mode="before")
    @classmethod
    def coerce_appointment_type(cls, v):
        return _coerce_appointment_type(v)

    @model_validator(mode="after")
    def at_least_one_identifier(self):
        return _validate_at_least_one_identifier(self)


class ScheduleRequest(BaseModel):
    patient_id: int | None = None
    patient_age: int
    patient_address: str
    hospital_id: int = 1
    service_id: int | None = None
    specialist_id: int | None = None
    appointment_type: list[int] | None = None

    @field_validator("appointment_type", mode="before")
    @classmethod
    def coerce_appointment_type(cls, v):
        return _coerce_appointment_type(v)

    slot_filter: str = "free"
    reception_mode: str | list[str] | None = None
    paid_visit: bool | None = None
    excluded_dates: list[str] = []

    @model_validator(mode="after")
    def at_least_one_identifier(self):
        return _validate_at_least_one_identifier(self)

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
