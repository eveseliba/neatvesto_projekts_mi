"""Pydantic response models for the /schedule endpoint."""

from pydantic import BaseModel, model_serializer


class SlotSpecialistPair(BaseModel):
    slot_id: int
    specialist_id: int


class SlotResult(BaseModel):
    slots: list[SlotSpecialistPair]
    date: str
    start_time: int
    end_time: int
    start_time_formatted: str
    end_time_formatted: str
    specialty_id: int
    hospital_id: int
    service_id: int | None
    appointment_type: int | None
    score: float | None
    profile_match: bool
    profile_tier_level: int | None = None
    profile_window_rate: float | None = None


class ServiceResult(BaseModel):
    service_id: int | None
    specialist_id: int | None
    earliest: SlotResult | None
    recommended: list[SlotResult]
    total_slots_evaluated: int
    tier_used: int | None
    profile_match: bool
    profile_match_reason: str | None = None

    @model_serializer(mode="wrap")
    def _omit_null_reason(self, handler):
        data = handler(self)
        if data.get("profile_match_reason") is None:
            data.pop("profile_match_reason", None)
        return data


class ScheduleResponse(BaseModel):
    patient_profile: str
    results: list[ServiceResult]
