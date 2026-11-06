"""Bounded airco assistance based on sustained room warming, never floor heat."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

OBSERVATION = timedelta(minutes=45)
MAX_REPORT_GAP = timedelta(minutes=20)
SAMPLE_INTERVAL = timedelta(minutes=5)
DECAY_INTERVAL = timedelta(minutes=15)
MIN_DEFICIT = 0.5
MIN_WARMING_PER_HOUR = 0.2
RECOVERY_PER_HOUR = 0.5
CORRECTION_STEP = 0.5
MAX_EXTRA = 1.0


@dataclass(frozen=True)
class HeatingCorrection:
    target: float | None = None
    context: tuple = ()
    started_at: datetime | None = None
    samples: tuple[tuple[datetime, float], ...] = ()
    last_reported_at: datetime | None = None
    extra: float = 0.0
    status: str = "inactive"
    trend: float | None = None
    changed_at: datetime | None = None


def heating_correction(
    previous,
    now,
    *,
    eligible,
    temperature,
    reported_at,
    target,
    context=(),
    max_extra=MAX_EXTRA,
):
    """Assess fresh reports over 45 minutes; timer ticks never create evidence.

    Each increase consumes its observation window. Sparse/stale data, changed
    targets/sources and interrupted heating reset the observation and assistance.
    The existing proportional correction's total limit is enforced by its planner.
    """
    if not eligible or temperature is None or target is None:
        return HeatingCorrection()
    if reported_at is None or reported_at > now or now - reported_at >= MAX_REPORT_GAP:
        return HeatingCorrection(status="waiting_for_fresh_temperature")
    state = previous or HeatingCorrection()
    if (
        state.started_at is None
        or state.target != target
        or state.context != context
        or (
            state.last_reported_at
            and reported_at - state.last_reported_at > MAX_REPORT_GAP
        )
    ):
        state = HeatingCorrection(target=target, context=context, started_at=now)
    if reported_at < state.started_at:
        return replace(state, status="waiting_for_fresh_temperature")
    if state.last_reported_at and reported_at <= state.last_reported_at:
        return state
    state = replace(state, last_reported_at=reported_at)
    deficit = target - temperature
    extra = min(state.extra, max(0.0, max_extra))
    if deficit <= 0:
        return HeatingCorrection(
            target=target,
            context=context,
            started_at=now,
            last_reported_at=reported_at,
            status="on_target",
        )
    if (
        deficit <= 0.2
        and extra
        and (state.changed_at is None or now - state.changed_at >= DECAY_INTERVAL)
    ):
        return replace(
            state,
            extra=max(0.0, extra - CORRECTION_STEP),
            samples=(),
            changed_at=now,
            status="recovering",
            trend=None,
        )
    if deficit < MIN_DEFICIT:
        return replace(
            state, extra=extra, samples=(), status="within_margin", trend=None
        )
    if state.samples and reported_at - state.samples[-1][0] < SAMPLE_INTERVAL:
        return state
    samples = (*state.samples, (reported_at, temperature))
    elapsed = reported_at - samples[0][0]
    if elapsed < OBSERVATION:
        return replace(state, extra=extra, samples=samples, status="observing")
    trend = (temperature - samples[0][1]) / (elapsed.total_seconds() / 3600)
    status = "warming"
    if trend < MIN_WARMING_PER_HOUR:
        extra = min(max(0.0, max_extra), extra + CORRECTION_STEP)
        status = "assisting" if extra > state.extra else "at_limit"
    elif trend >= RECOVERY_PER_HOUR and extra:
        extra = max(0.0, extra - CORRECTION_STEP)
        status = "recovering"
    return replace(
        state,
        samples=((reported_at, temperature),),
        extra=extra,
        trend=round(trend, 3),
        status=status,
        changed_at=now if extra != state.extra else state.changed_at,
    )
