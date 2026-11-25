"""Current wind and gust limits with hysteresis and a continuous calm hold."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .rain import DRY_CONDITIONS, WET_CONDITIONS
from .shading import FACADES

WIND_CONDITIONS = frozenset({"windy", "windy-variant"})
RELEASE_FACTOR = 0.8
SPEED_FACTORS = {"km/h": 1.0, "m/s": 3.6, "mph": 1.609344, "kn": 1.852, "ft/s": 1.09728}


def speed_kmh(value, unit):
    """Weather attributes use the published wind_speed_unit, never a guessed unit."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or not isinstance(unit, str)
        or unit not in SPEED_FACTORS
    ):
        return None
    converted = value * SPEED_FACTORS[unit]
    return converted if math.isfinite(converted) else None


def bearing_degrees(value):
    """Validate the direction the wind comes from, clockwise from north."""
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 360
    ):
        return float(value % 360)
    return None


@dataclass(frozen=True)
class WindMemory:
    active: bool = False
    calm_since: datetime | None = None


@dataclass(frozen=True)
class WindProtection:
    status: str
    memory: WindMemory
    open_required: bool = False
    block_closing: bool = False
    next_check: datetime | None = None

    def for_facade(self, bearing, facade):
        """Keep protection for windward facades, including crosswinds."""
        if (
            self.open_required
            and self.status != "unknown"
            and bearing is not None
            and facade in FACADES
        ):
            angle = abs((bearing - FACADES[facade] + 180) % 360 - 180)
            if angle > 90:
                return WindProtection("leeward", WindMemory())
        return self


def wind_protection(
    speed, gust, speed_limit, gust_limit, memory, now, calm_minutes, *, condition=None
):
    """Known excessive wind wins even when another configured reading is missing."""
    checks = [
        (value, limit)
        for value, limit in ((speed, speed_limit), (gust, gust_limit))
        if limit is not None
    ]
    if not checks:
        if condition in WIND_CONDITIONS:
            return WindProtection("wind", WindMemory(True), True, True)
        if condition not in DRY_CONDITIONS | WET_CONDITIONS:
            return WindProtection(
                "unknown", WindMemory(memory.active), memory.active, True
            )
    if any(value is not None and value >= limit for value, limit in checks):
        return WindProtection("wind", WindMemory(True), True, True)
    if any(value is None for value, _ in checks):
        return WindProtection("unknown", WindMemory(memory.active), memory.active, True)
    if not memory.active:
        return WindProtection("calm", WindMemory())
    if any(value > limit * RELEASE_FACTOR for value, limit in checks):
        return WindProtection("wind", WindMemory(True), True, True)
    since = memory.calm_since or now
    until = since + timedelta(minutes=calm_minutes)
    if now < until:
        return WindProtection("calming", WindMemory(True, since), True, True, until)
    return WindProtection("calm", WindMemory())
