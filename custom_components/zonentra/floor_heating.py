"""Observe inadequate room warming without increasing gas heating."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

WATCH_MINUTES = 60
MINIMUM_RISE = 0.3
MINIMUM_DEFICIT = 1.0


@dataclass(frozen=True)
class HeatingWatch:
    since: datetime
    temperature: float
    reported_at: datetime | None
    target: float


@dataclass(frozen=True)
class HeatingProgress:
    status: str
    memory: HeatingWatch | None = None
    next_check: datetime | None = None
    rise: float | None = None


def heating_progress(previous, now, *, temperature, target, reported_at, watching):
    """Signal only with continuous reported heating and a newer room report.

    This is a room comfort observation, not an efficiency/fault measurement.
    The room fan can use this signal independently of the floor thermostat.
    """
    if not watching:
        return HeatingProgress("idle")
    if temperature is None or target is None:
        return HeatingProgress("unknown")
    if target - temperature < MINIMUM_DEFICIT:
        return HeatingProgress("adequate")
    memory = (
        previous
        if previous and previous.target == target
        else HeatingWatch(now, temperature, reported_at, target)
    )
    deadline = memory.since + timedelta(minutes=WATCH_MINUTES)
    rise = round(temperature - memory.temperature, 3)
    if now < deadline:
        return HeatingProgress("monitoring", memory, deadline, rise)
    if (
        reported_at is None
        or reported_at <= memory.since
        or (memory.reported_at is not None and reported_at <= memory.reported_at)
    ):
        return HeatingProgress("unknown", memory, rise=rise)
    if rise < MINIMUM_RISE:
        return HeatingProgress("insufficient", memory, rise=rise)
    return HeatingProgress(
        "monitoring",
        HeatingWatch(now, temperature, reported_at, target),
        now + timedelta(minutes=WATCH_MINUTES),
        rise,
    )


def floor_demand_percent(state):
    """Read an optional 0–100% heating-demand signal without assuming bad data."""
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    if state.attributes.get("restored") or state.attributes.get("assumed_state"):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit not in (None, "%"):
        return None
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None
