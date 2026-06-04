"""Pydantic response models for the /schedule/regular endpoint."""

from pydantic import BaseModel

from scheduling.schedule_response import SlotResult


class RegularSlotResult(SlotResult):
    """Extends SlotResult with regular-scheduling metadata."""
    label: str
    days_since_previous: int | None


class RegularPlanVisit(BaseModel):
    """A single visit (one day) within the regular schedule."""
    date: str
    slots: list[RegularSlotResult]


class RegularScheduleResponse(BaseModel):
    patient_profile: str
    start_date: str
    times_per_week: int
    total_times: int
    total_visits: int
    visits: list[RegularPlanVisit]
