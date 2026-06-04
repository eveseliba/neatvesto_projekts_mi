"""Pydantic response models for the /schedule/multi endpoint."""

from pydantic import BaseModel

from scheduling.schedule_response import SlotResult


class PlanVisit(BaseModel):
    """A single visit (one day) within a plan."""
    date: str
    slots: list[SlotResult]


class Plan(BaseModel):
    """A complete scheduling plan covering all services."""
    rank: int
    score: float
    visits: list[PlanVisit]


class MultiScheduleResponse(BaseModel):
    patient_profile: str
    earliest: list[SlotResult | None]
    plans: list[Plan]
