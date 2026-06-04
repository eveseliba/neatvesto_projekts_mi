"""Shared Pydantic validators for scheduling models."""

from datetime import datetime as _dt


def _validate_excluded_dates(v: list[str]) -> list[str]:
    """Validate that each excluded_dates entry is a valid YYYY-MM-DD string."""
    for d in v:
        try:
            _dt.strptime(d, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Invalid date format: '{d}'. Expected YYYY-MM-DD."
            )
    return v


def _validate_at_least_one_identifier(self):
    """Ensure at least one of service_id or specialist_id is provided."""
    if self.service_id is None and self.specialist_id is None:
        raise ValueError(
            "At least one of service_id or specialist_id must be provided"
        )
    return self


def _coerce_appointment_type(v):
    """Accept int or list[int] for appointment_type, normalise to list[int] | None."""
    if v is None:
        return None
    if isinstance(v, int):
        return [v]
    if isinstance(v, list) and len(v) == 0:
        return None
    if isinstance(v, list) and len(v) > 50:
        raise ValueError("appointment_type must contain at most 50 entries")
    return v
