"""Pure immutable readings and unit/age validation; no Home Assistant imports.

Two freshness strategies exist. ``state_based`` (max_age 0) accepts any valid,
available HA state: many sources (KNX, some Modbus/MQTT setups) only send on a
change, so an unchanged value is not evidence of a fault. ``update_based``
(max_age > 0) additionally requires a report (HA ``last_reported``) within
max_age; this is only meaningful for sources that demonstrably report
periodically. Neither strategy proves that the physical sensor measured again.
"""

import math
from dataclasses import dataclass
from datetime import datetime

STATE_BASED = "state_based"
UPDATE_BASED = "update_based"
VALIDATION_STRATEGIES = (STATE_BASED, UPDATE_BASED)


@dataclass(frozen=True)
class ClimateAttributes:
    """Read-only climate attributes copied from HA; none of these are commands."""

    hvac_action: str | None = None
    current_temperature: float | None = None
    target_temperature: float | None = None
    fan_mode: str | None = None
    temperature_unit: str | None = None
    observed_at: datetime | None = None  # HA last_updated: state or attribute change


@dataclass(frozen=True)
class SourceState:
    state: str | None
    unit: str | None = None
    reported_at: datetime | None = None  # HA last_reported: last write, even unchanged
    trustworthy: bool = True
    changed_at: datetime | None = None  # HA last_changed: diagnostic only
    climate: ClimateAttributes | None = None  # Only for climate entities


@dataclass(frozen=True)
class Reading:
    value: float | str | None
    quality: str
    reported_at: datetime | None = None
    reason: str | None = None
    changed_at: datetime | None = None
    max_age: float = 0

    @property
    def strategy(self) -> str:
        return UPDATE_BASED if self.max_age else STATE_BASED

    @property
    def freshness_verified(self) -> bool:
        """True only when a periodic report within max_age was required and seen."""
        return self.quality == "valid" and bool(self.max_age)


def max_age_for(strategy: str, max_age: float) -> float:
    """State-based validation never applies an age limit."""
    return max_age if strategy == UPDATE_BASED else 0


def read(
    source: SourceState | None, kind: str, now: datetime, max_age: float = 0
) -> Reading:
    """Validate explicit units and, if max_age is set, report freshness.

    The value's age since last_changed is never a rejection reason.
    """
    if source is None:
        return Reading(None, "missing", reason="source_missing", max_age=max_age)
    timestamp = source.reported_at

    def result(value, quality, reason=None):
        return Reading(value, quality, timestamp, reason, source.changed_at, max_age)

    if source.state in (None, "unknown", "unavailable"):
        state = source.state or "unknown"
        return result(None, state, f"source_{state}")
    if not source.trustworthy:
        return result(None, "unreliable", "untrusted_source")
    if (
        timestamp is None
        or timestamp.tzinfo is None
        or (timestamp - now).total_seconds() > 5
    ):
        return result(None, "unreliable", "invalid_timestamp")
    if max_age and (now - timestamp).total_seconds() >= max_age:
        return result(None, "stale", "no_report_within_max_age")
    if kind in ("occupancy", "contact"):
        value = {
            "on": "occupied" if kind == "occupancy" else "open",
            "off": "unoccupied" if kind == "occupancy" else "closed",
        }.get(source.state)
        return (
            result(value, "valid")
            if value
            else result(None, "unreliable", "unsupported_state")
        )
    if kind == "mode":
        value = source.state if source.state in ("heat", "cool", "off") else None
        return (
            result(value, "valid")
            if value
            else result(None, "unreliable", "unsupported_state")
        )
    try:
        value = float(source.state)
    except TypeError, ValueError:
        return result(None, "unreliable", "not_numeric")
    if not math.isfinite(value):
        return result(None, "unreliable", "not_finite")
    unit = source.unit
    if kind in ("temperature", "limit"):
        if unit == "°F":
            value = (value - 32) * 5 / 9
        elif unit != "°C":
            return result(None, "unreliable", "unsupported_unit")
        low, high = (-50, 65) if kind == "temperature" else (5, 35)
    else:
        raise ValueError(f"Unknown reading kind: {kind}")
    if not low <= value <= high:
        return result(None, "unreliable", "out_of_range")
    return result(value, "valid")


def _age(now: datetime, timestamp: datetime | None) -> float | None:
    return round((now - timestamp).total_seconds()) if timestamp else None


def describe(reading: Reading, now: datetime) -> dict:
    """Explain a reading for diagnostics without exposing entity IDs or raw states."""
    return {
        "health": reading.quality,
        "strategy": reading.strategy,
        "value": reading.value,
        "reason": reading.reason,
        "seconds_since_change": _age(now, reading.changed_at),
        "seconds_since_report": _age(now, reading.reported_at),
        "max_update_age_seconds": reading.max_age or None,
        "freshness_verified": reading.freshness_verified,
        "evaluated_at": now.isoformat(),
    }


def configured_limit(value: float | None) -> Reading:
    """Static UI limits are in Celsius and do not expire."""
    if value is None:
        return Reading(None, "missing", reason="not_configured")
    return Reading(value, "valid")


def cover_position(state) -> float | None:
    """A usable HA cover position: 100 open, 0 closed, None if unknown."""
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    if state.attributes.get("restored") or state.attributes.get("assumed_state"):
        return None
    position = state.attributes.get("current_position")
    if (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and math.isfinite(position)
        and 0 <= position <= 100
    ):
        return float(position)
    return {"open": 100.0, "closed": 0.0}.get(state.state)
