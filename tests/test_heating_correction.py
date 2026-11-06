"""Sustained warming, freshness, recovery and bounded airco-only assistance."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.zonentra.heating_correction import heating_correction
from custom_components.zonentra.thermostat import plan_setpoint

NOW = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)


def observe(state=None, minute=0, temperature=20.5, **kwargs):
    now = NOW + timedelta(minutes=minute)
    inputs = dict(eligible=True, temperature=temperature, reported_at=now, target=21.5)
    return heating_correction(state, now, **(inputs | kwargs))


def plateau(end=45):
    state = None
    for minute in range(0, end + 1, 5):
        state = observe(state, minute)
    return state


def test_plateau_requires_full_fresh_window_and_each_step_has_new_evidence():
    state = plateau(40)
    assert state.extra == 0
    state = observe(state, 45)
    assert (state.extra, state.trend, state.status) == (0.5, 0, "assisting")
    for minute in range(50, 90, 5):
        state = observe(state, minute)
        assert state.extra == 0.5
    state = observe(state, 90)
    assert state.extra == 1
    for minute in range(95, 181, 5):
        state = observe(state, minute)
        assert state.extra == 1
    assert state.status == "at_limit"


def test_good_warming_does_not_increase_correction():
    state = None
    for minute in range(0, 46, 5):
        state = observe(state, minute, temperature=20 + minute / 60)
    assert state.extra == 0
    assert state.status == "warming"


def test_timer_events_and_duplicate_reports_cannot_train():
    state = plateau(40)
    report = NOW + timedelta(minutes=40)
    state = observe(state, 50, reported_at=report)
    assert state.extra == 0
    assert len(state.samples) == 9
    state = observe(state, 60, reported_at=report)
    assert state.extra == 0
    assert state.status == "waiting_for_fresh_temperature"


@pytest.mark.parametrize(
    "changes",
    [
        {"eligible": False},
        {"target": 22},
        {"context": ("new_sensor",)},
        {"temperature": None},
        {"reported_at": None},
    ],
)
def test_changed_context_or_interruption_removes_previous_assistance(changes):
    state = observe(plateau(), 50, **changes)
    assert state.extra == 0


def test_missing_reports_reset_evidence_and_extra():
    state = observe(plateau(), 70)
    assert state.extra == 0
    assert len(state.samples) == 1


def test_old_startup_report_cannot_become_a_new_anchor():
    state = observe(reported_at=NOW - timedelta(minutes=1))
    assert not state.samples
    state = observe(state, 5)
    assert len(state.samples) == 1
    assert state.samples[0][0] == NOW + timedelta(minutes=5)


def test_extra_decays_on_recovery_without_chattering_and_resets_at_target():
    state = plateau(90)
    state = observe(state, 105, temperature=21.35)
    assert state.extra == 0.5
    state = observe(state, 110, temperature=21.36)
    assert state.extra == 0.5
    state = observe(state, 120, temperature=21.4)
    assert state.extra == 0
    assert observe(plateau(), 50, temperature=21.5).extra == 0


def test_strong_warming_reduces_extra_even_before_target():
    state = plateau(90)
    for minute in range(95, 136, 5):
        state = observe(state, minute, temperature=20.5 + (minute - 90) / 90)
    assert state.extra == 0.5
    assert state.status == "recovering"


def test_small_deficit_cannot_build_assistance():
    state = None
    for minute in range(0, 121, 5):
        state = observe(state, minute, temperature=21.1)
    assert state.extra == 0


def test_extra_respects_existing_total_limit_device_step_and_command_interval():
    def plan(mode="heat", **kwargs):
        inputs = dict(max_offset=2, interval_seconds=300, step=0.5, heating_extra=1)
        return plan_setpoint(
            mode, 21.5, 24, 0.3, 20.5, 22.5, None, NOW, **(inputs | kwargs)
        )

    assert plan().setpoint == 24
    assert plan(max_offset=1).setpoint == 23
    assert plan(max_temp=23).setpoint == 23
    no_extra = plan("cool", heating_extra=0)
    assert plan("cool").setpoint == no_extra.setpoint
    timed = plan_setpoint(
        "heat",
        21.5,
        24,
        0.3,
        20.5,
        22.5,
        NOW,
        NOW + timedelta(seconds=60),
        max_offset=2,
        interval_seconds=300,
        heating_extra=1,
    )
    assert timed.status == "interval"
    assert not timed.send
