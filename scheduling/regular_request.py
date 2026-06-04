"""Pydantic request models for the /schedule/regular endpoint."""

import math
from datetime import date, datetime as _dt, timedelta

from pydantic import BaseModel, field_validator, model_validator

from scheduling.validators import _validate_excluded_dates, _coerce_appointment_type
from scheduling.multi_request import MultiServiceItem

MAX_WEEKS = 52       # 1-year horizon
MAX_HORIZON_WEEKS = 52  # cap for computed_until_date


class RegularScheduleRequest(BaseModel):
    patient_id: int | None = None
    patient_age: int
    patient_address: str
    hospital_id: int = 1
    slot_filter: str = "free"
    reception_mode: str | list[str] | None = None
    appointment_type: list[int] | None = None
    paid_visit: bool | None = None
    services: list[MultiServiceItem]
    excluded_dates: list[str] = []
    start_date: str
    times_per_week: int
    total_times: int

    @field_validator("appointment_type", mode="before")
    @classmethod
    def coerce_appointment_type(cls, v):
        return _coerce_appointment_type(v)

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

    @field_validator("start_date")
    @classmethod
    def valid_start_date(cls, v):
        try:
            _dt.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Invalid date format: '{v}'. Expected YYYY-MM-DD."
            )
        parsed = date.fromisoformat(v)
        if parsed < date.today():
            raise ValueError("start_date must be today or in the future")
        # Shift weekend to next Monday
        if parsed.weekday() >= 5:
            parsed += timedelta(days=(7 - parsed.weekday()))
            v = parsed.isoformat()
        return v

    @field_validator("times_per_week")
    @classmethod
    def valid_times_per_week(cls, v):
        if v < 1 or v > 5:
            raise ValueError("times_per_week must be between 1 and 5")
        return v

    @field_validator("total_times")
    @classmethod
    def valid_total_times(cls, v):
        if v < 1:
            raise ValueError("total_times must be at least 1")
        return v

    @model_validator(mode="after")
    def validate_total_times_range(self):
        max_total = self.times_per_week * MAX_WEEKS
        if self.total_times > max_total:
            raise ValueError(
                f"total_times ({self.total_times}) exceeds maximum "
                f"{max_total} for {self.times_per_week}x/week over {MAX_WEEKS} weeks"
            )
        return self

    @property
    def computed_until_date(self) -> date:
        """Initial until-date (no buffer), capped at start + 1 year."""
        base_weeks = math.ceil(self.total_times / self.times_per_week)
        capped_weeks = min(base_weeks, MAX_HORIZON_WEEKS)
        return date.fromisoformat(self.start_date) + timedelta(weeks=capped_weeks)

    @property
    def max_until_date(self) -> date:
        """Absolute 1-year limit from start_date."""
        return date.fromisoformat(self.start_date) + timedelta(weeks=MAX_HORIZON_WEEKS)
