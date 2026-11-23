"""Learn the temperature offset between each indoor unit and its room sensor."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from statistics import fmean, pstdev

SETTLE_TIME = timedelta(minutes=5)
SAMPLE_INTERVAL = timedelta(minutes=5)
MAX_PAIR_SKEW = timedelta(minutes=5)
MAX_REPORT_AGE = timedelta(minutes=20)
MAX_PROFILE_AGE = timedelta(days=7)
MIN_SAMPLES = 6
MAX_SAMPLES = 8
MAX_SPREAD = 0.35
MAX_SENSOR_DIFFERENCE = 4.0
MAX_CORRECTION = 1.0
ACTIVE_ACTION = {"heat": "heating", "cool": "cooling"}


@dataclass(frozen=True)
class OffsetProfile:
    samples: tuple[float, ...] = ()
    room_at: datetime | None = None
    unit_at: datetime | None = None

    @property
    def estimate(self):
        return fmean(self.samples) if self.samples else None

    @property
    def spread(self):
        return pstdev(self.samples) if len(self.samples) > 1 else None


@dataclass(frozen=True)
class SensorOffset:
    context: tuple[str, str | None] = ("", None)
    profiles: dict[str, OffsetProfile] = field(default_factory=dict)
    mode: str | None = None
    active_since: datetime | None = None
    status: str = "inactive"
    correction: float = 0.0
    next_check: datetime | None = None

    def summary(self):
        """Export derived values without raw observations or entity IDs."""
        return {
            "status": self.status,
            "mode": self.mode,
            "correction_c": self.correction,
            "profiles": {
                mode: {
                    "samples": len(profile.samples),
                    "estimate_c": round(estimate, 2)
                    if (estimate := profile.estimate) is not None
                    else None,
                    "spread_c": round(spread, 2)
                    if (spread := profile.spread) is not None
                    else None,
                }
                for mode, profile in self.profiles.items()
            },
        }


def observe_sensor_offset(
    previous: SensorOffset | None,
    now: datetime,
    *,
    context: tuple[str, str | None],
    mode: str | None,
    action: str | None,
    difference: float | None,
    room_at: datetime | None,
    unit_at: datetime | None,
) -> SensorOffset:
    """Learn from paired reports in stable runs and reuse the offset during idle."""
    if previous is None or previous.context != context:
        previous = SensorOffset(context=context)
    profiles = {
        key: profile
        for key, profile in previous.profiles.items()
        if now - profile.room_at < MAX_PROFILE_AGE
    }
    state = SensorOffset(context=context, profiles=profiles, mode=mode)
    active = mode in ACTIVE_ACTION and action == ACTIVE_ACTION[mode]
    if mode not in ACTIVE_ACTION or (not active and action != "idle"):
        return state
    if active:
        active_since = previous.active_since if previous.mode == mode else None
        state = replace(state, active_since=active_since or now)
    if (
        difference is None
        or room_at is None
        or unit_at is None
        or not now - MAX_REPORT_AGE < min(room_at, unit_at)
        or max(room_at, unit_at) > now
        or abs(room_at - unit_at) > MAX_PAIR_SKEW
    ):
        return replace(state, status="waiting_for_fresh_pair")

    profile = profiles.get(mode, OffsetProfile())
    next_check = min(room_at, unit_at) + MAX_REPORT_AGE
    state = replace(state, next_check=next_check)
    continuing = previous.mode == mode and previous.status in (
        "correcting",
        "no_offset",
    )
    if active:
        settled_at = state.active_since + SETTLE_TIME
        if min(room_at, unit_at) < settled_at:
            if not continuing:
                return replace(state, status="settling")
        elif (
            profile.room_at is None
            or room_at - profile.room_at >= SAMPLE_INTERVAL
            and unit_at - profile.unit_at >= SAMPLE_INTERVAL
        ):
            samples = (
                (*profile.samples, difference)[-MAX_SAMPLES:]
                if abs(difference) <= MAX_SENSOR_DIFFERENCE
                else ()
            )
            profile = OffsetProfile(samples, room_at, unit_at)
            state = replace(state, profiles={**profiles, mode: profile})
        if not continuing and (
            profile.room_at is None
            or min(profile.room_at, profile.unit_at) < settled_at
        ):
            return replace(state, status="waiting_for_fresh_pair")
    elif not continuing:
        return replace(state, status="idle")

    if abs(difference) > MAX_SENSOR_DIFFERENCE:
        return replace(state, status="implausible_difference")
    if len(profile.samples) < MIN_SAMPLES:
        return replace(state, status="learning")
    if profile.spread > MAX_SPREAD:
        return replace(state, status="inconsistent")
    estimate = profile.estimate
    # Half-degree steps keep small changes in the learned estimate from moving
    # the target on every report. The planner still uses the actual device grid.
    correction = max(-MAX_CORRECTION, min(MAX_CORRECTION, round(2 * estimate) / 2))
    return replace(
        state,
        status="correcting" if correction else "no_offset",
        correction=correction,
        next_check=min(next_check, profile.room_at + MAX_PROFILE_AGE),
    )
