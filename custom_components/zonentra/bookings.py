"""Pure booking windows and empirical room heating/cooling estimates."""

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo

MIN_SAMPLE_MINUTES = 5
MAX_SAMPLE_MINUTES = 30
MIN_RATE = 0.005  # °C/min; slower/no progress does not yield a usable sample.
MAX_RATE = 0.5
RATE_ALPHA = 0.25
MIN_LEARNED_SAMPLES = 4
MAX_PROFILE_AGE_DAYS = 30


def outdoor_band(temperature):
    if temperature is None:
        return "unknown"
    return (
        "freezing"
        if temperature < 0
        else "cold"
        if temperature < 10
        else "mild"
        if temperature < 20
        else "warm"
        if temperature < 30
        else "hot"
    )


def rate_quality(mode, rates, now):
    """Expose the evidence behind an estimate; legacy rates have unknown age."""
    value = getattr(rates, mode)
    count = getattr(rates, f"{mode}_samples")
    raw = getattr(rates, f"{mode}_updated")
    updated = datetime.fromisoformat(raw) if raw else None
    age = (now - updated).total_seconds() / 86400 if updated else None
    spread = math.sqrt(getattr(rates, f"{mode}_variance")) / value if value else None
    status = (
        "untrained"
        if value is None
        else "age_unknown"
        if age is None
        else "expired"
        if age < 0 or age > MAX_PROFILE_AGE_DAYS
        else "insufficient_samples"
        if count < MIN_LEARNED_SAMPLES
        else "variable"
        if spread > 0.5
        else "reliable"
    )
    confidence = min(1.0, count / 8) * (1 - spread) if status == "reliable" else 0.0
    return {
        "status": status,
        "samples": count,
        "age_days": age,
        "relative_spread": spread,
        "confidence": confidence,
    }


def estimated_rate(mode, rates, default, outdoor, now):
    learned = getattr(rates, mode)
    # A conservative starting estimate until this outdoor band has samples.
    # This is a bounded heuristic, not a heat-loss or heat-pump capacity model.
    penalty = (
        max(0, 10 - outdoor)
        if mode == "heating" and outdoor is not None
        else max(0, outdoor - 25)
        if outdoor is not None
        else 10
    )
    baseline = default / (1 + 0.03 * penalty)
    quality = rate_quality(mode, rates, now)
    if quality["status"] == "reliable":
        conservative = learned * (1 - quality["relative_spread"])
        # Slow evidence is conservative immediately; faster rates earn trust.
        return (
            min(baseline, conservative)
            + quality["confidence"] * max(0, conservative - baseline),
            True,
        )
    return baseline, False


@dataclass(frozen=True, order=True)
class Booking:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ThermalRates:
    heating: float | None = None
    cooling: float | None = None
    heating_samples: int = 0
    cooling_samples: int = 0
    heating_variance: float = 0.0
    cooling_variance: float = 0.0
    heating_updated: str | None = None
    cooling_updated: str | None = None
    heating_buffer: float = 0.0
    cooling_buffer: float = 0.0

    @classmethod
    def restore(cls, data):
        if not isinstance(data, dict):
            return cls()
        values = {}
        for mode in ("heating", "cooling"):
            buffer = data.get(f"{mode}_buffer", 0.0)
            if (
                isinstance(buffer, (int, float))
                and not isinstance(buffer, bool)
                and math.isfinite(buffer)
                and 0 <= buffer <= 30
            ):
                values[f"{mode}_buffer"] = buffer
            rate, count = data.get(mode), data.get(f"{mode}_samples")
            if (
                isinstance(rate, (int, float))
                and not isinstance(rate, bool)
                and math.isfinite(rate)
                and MIN_RATE <= rate <= MAX_RATE
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            ):
                values[mode], values[f"{mode}_samples"] = rate, count
                variance = data.get(f"{mode}_variance", 0.0)
                variance_valid = (
                    isinstance(variance, (int, float))
                    and not isinstance(variance, bool)
                    and math.isfinite(variance)
                    and 0 <= variance <= MAX_RATE**2
                )
                if variance_valid:
                    values[f"{mode}_variance"] = variance
                raw = data.get(f"{mode}_updated")
                try:
                    updated = (
                        datetime.fromisoformat(raw) if isinstance(raw, str) else None
                    )
                except ValueError:
                    updated = None
                if (
                    variance_valid
                    and updated is not None
                    and updated.tzinfo is not None
                ):
                    values[f"{mode}_updated"] = updated.isoformat()
        return cls(**values)


@dataclass(frozen=True)
class ThermalSample:
    at: datetime
    temperature: float
    mode: str
    outdoor_band: str


def learn_rate(
    rates, sample, now, temperature, mode, outdoor=None, *, diagnostics=None
):
    """Learn only from sustained heating/cooling, not passive temperature drift."""

    def result(rates, sample, reason):
        if diagnostics is not None:
            diagnostics["reason"] = reason
        return rates, sample

    if (
        temperature is None
        or not math.isfinite(temperature)
        or mode not in ("heating", "cooling")
    ):
        return result(rates, None, "invalid_measurement_or_mode")
    current = ThermalSample(now, temperature, mode, outdoor_band(outdoor))
    if (
        sample is None
        or sample.mode != mode
        or sample.outdoor_band != current.outdoor_band
    ):
        return result(rates, current, "sample_started")
    if now <= sample.at:
        return result(rates, sample, "non_increasing_timestamp")
    minutes = (now - sample.at).total_seconds() / 60
    if minutes < MIN_SAMPLE_MINUTES:
        return result(rates, sample, "sample_too_short")
    if minutes > MAX_SAMPLE_MINUTES:
        return result(rates, current, "sample_gap_too_long")
    change = temperature - sample.temperature
    if change == 0:
        return result(rates, sample, "no_temperature_change")
    rate = (change if mode == "heating" else -change) / minutes
    if not MIN_RATE <= rate <= MAX_RATE:
        return result(rates, current, "rate_out_of_range")
    previous = getattr(rates, mode)
    updated = getattr(rates, f"{mode}_updated")
    old = datetime.fromisoformat(updated) if updated else None
    reset = old is None or now < old or now - old > timedelta(days=MAX_PROFILE_AGE_DAYS)
    count = 0 if reset else getattr(rates, f"{mode}_samples")
    if reset:
        previous = None
    variance = (
        0.0
        if previous is None
        else (1 - RATE_ALPHA)
        * (getattr(rates, f"{mode}_variance") + RATE_ALPHA * (rate - previous) ** 2)
    )
    learned = (
        rate if previous is None else previous * (1 - RATE_ALPHA) + rate * RATE_ALPHA
    )
    return result(
        replace(
            rates,
            **{
                mode: learned,
                f"{mode}_samples": count + 1,
                f"{mode}_variance": variance,
                f"{mode}_updated": now.isoformat(),
            },
        ),
        current,
        "sample_recorded",
    )


def parse_bookings(events, local_timezone: tzinfo):
    """Keep times only. All-day entries are not timed room reservations."""
    if not isinstance(events, list):
        return None
    result = []
    for event in events:
        if not isinstance(event, dict):
            return None
        start, end = event.get("start"), event.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        # HA uses ISO dates for all-day entries and ISO datetimes for meetings.
        if len(start) == 10 and len(end) == 10:
            continue
        try:
            start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError:
            return None
        start = start.replace(tzinfo=local_timezone) if start.tzinfo is None else start
        end = end.replace(tzinfo=local_timezone) if end.tzinfo is None else end
        if end <= start:
            return None
        result.append(
            Booking(start.astimezone(timezone.utc), end.astimezone(timezone.utc))
        )
    # Merge overlapping/back-to-back meetings, so comfort is continuous.
    merged = []
    for event in sorted(set(result)):
        if merged and event.start <= merged[-1].end:
            merged[-1] = Booking(merged[-1].start, max(merged[-1].end, event.end))
        else:
            merged.append(event)
    return tuple(merged)


@dataclass(frozen=True)
class BookingPlan:
    status: str  # not_configured / unknown / idle / scheduled / preparing / meeting
    start: datetime | None = None
    end: datetime | None = None
    prepare_at: datetime | None = None
    lead_minutes: float = 0
    direction: str | None = None
    rate: float | None = None
    learned: bool = False
    next_check: datetime | None = None

    @property
    def active(self):
        return self.status in ("preparing", "meeting")


def assess_booking(temperature, heat, cool, eligible):
    """Outcome at meeting start; unavailable/blocked input cannot train margins."""
    if not eligible:
        return "not_evaluated", None
    if temperature is None or heat is None or cool is None or heat >= cool:
        return "unknown", None
    if temperature < heat:
        return "too_cold", round(heat - temperature, 3)
    if temperature > cool:
        return "too_warm", round(temperature - cool, 3)
    return "ready", 0.0


def learn_booking_margin(rates, status, direction):
    """Small, bounded correction; never modify a measured thermal rate."""
    mode = {"too_cold": "heating", "too_warm": "cooling"}.get(status)
    if mode:
        return replace(
            rates, **{f"{mode}_buffer": min(30, getattr(rates, f"{mode}_buffer") + 5)}
        )
    if status == "ready" and direction in ("heating", "cooling"):
        return replace(
            rates,
            **{
                f"{direction}_buffer": max(0, getattr(rates, f"{direction}_buffer") - 1)
            },
        )
    return rates


def booking_priority(plan, now):
    """A bounded 1..2 multiplier; group timing and authorization stay separate."""
    if plan.status == "meeting":
        return 2.0
    if plan.status != "preparing" or plan.start is None:
        return 1.0
    remaining = max(0, (plan.start - now).total_seconds() / 60)
    return 1 + max(0, min(1, 1 - remaining / max(1, plan.lead_minutes)))


def plan_booking(
    events,
    now,
    temperature,
    heat,
    cool,
    hysteresis,
    rates,
    *,
    heating_rate,
    cooling_rate,
    buffer_minutes,
    max_minutes,
    previous=None,
    outdoor=None,
):
    """Select the next booking and its bounded adaptive preparation window."""
    if events is None:
        return BookingPlan("unknown")
    event = next((event for event in events if event.end > now), None)
    if event is None:
        return BookingPlan("idle")
    lead, direction, rate, learned = 0, None, None, False
    if temperature is None or heat is None or cool is None:
        # With missing room input, reserve the full horizon; existing sensor
        # validation still prevents unsafe HVAC commands.
        lead = max_minutes
    else:
        if temperature < heat:
            direction = "heating"
            rate, learned = estimated_rate("heating", rates, heating_rate, outdoor, now)
            distance = heat + hysteresis - temperature
        elif temperature > cool:
            direction = "cooling"
            rate, learned = estimated_rate("cooling", rates, cooling_rate, outdoor, now)
            distance = temperature - (cool - hysteresis)
        else:
            distance = 0
        adjustment = getattr(rates, f"{direction}_buffer") if direction else 0
        lead = min(
            max_minutes, buffer_minutes + adjustment + (distance / rate if rate else 0)
        )
    prepare_at = event.start - timedelta(minutes=lead)
    # Once preparation starts, reaching the target must not end comfort early.
    if previous and previous.active and previous.start == event.start:
        prepare_at = max(
            event.start - timedelta(minutes=max_minutes),
            min(previous.prepare_at, prepare_at),
        )
    lead = (event.start - prepare_at).total_seconds() / 60
    status = (
        "meeting"
        if now >= event.start
        else "preparing"
        if now >= prepare_at
        else "scheduled"
    )
    return BookingPlan(
        status,
        event.start,
        event.end,
        prepare_at,
        lead,
        direction,
        rate,
        learned,
        event.end
        if status == "meeting"
        else event.start
        if status == "preparing"
        else prepare_at,
    )
