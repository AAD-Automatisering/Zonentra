"""0.10.0 pure tests: window, safety, temperature trend and automatic fan."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.zonentra.observations import Reading
from custom_components.zonentra.roomstate import (
    Trend,
    auto_fan,
    fan_names,
    fan_speeds,
    safety_state,
    unit_fan,
    update_trend,
    vane_command,
    vane_mode,
    window_state,
)
from custom_components.zonentra.thermostat import Thermostat, update_thermostat

NOW = datetime(2026, 10, 14, 17, tzinfo=UTC)


def contact(value, ago=timedelta(minutes=5), quality="valid"):
    return Reading(value, quality, NOW, changed_at=NOW - ago)


# --- Window -------------------------------------------------------------------


def test_no_contacts():
    assert window_state((), 30, False, NOW) == ("not_configured", None)


def test_closed_and_unknown():
    assert window_state((contact("closed"),), 30, False, NOW) == ("closed", None)
    unknown = contact(None, quality="unavailable")
    assert window_state((unknown,), 30, False, NOW) == ("unknown", None)
    # One unknown contact next to a closed one: closed.
    assert window_state((unknown, contact("closed")), 30, False, NOW)[0] == "closed"


def test_open_after_the_delay():
    state, check = window_state(
        (contact("open", timedelta(seconds=10)),), 30, False, NOW
    )
    assert (state, check) == ("opening", NOW + timedelta(seconds=20))
    assert window_state((contact("open"),), 30, False, NOW) == ("open", None)
    assert window_state((contact("open", timedelta(0)),), 0, False, NOW)[0] == "open"


def test_any_open_contact_counts_from_the_first_opening():
    contacts = (
        contact("open", timedelta(seconds=40)),
        contact("open", timedelta(seconds=5)),
        contact("closed"),
    )
    assert window_state(contacts, 30, False, NOW)[0] == "open"


def test_bypass():
    assert window_state((contact("open"),), 30, True, NOW) == ("bypass", None)
    assert window_state((contact("closed"),), 30, True, NOW)[0] == "closed"


def test_bypass_is_stored():
    thermostat = update_thermostat(Thermostat(), 0.3, window_bypass=True)
    assert Thermostat.from_dict(thermostat.as_dict()).window_bypass is True
    assert Thermostat.from_dict({"window_bypass": "yes"}).window_bypass is False
    with pytest.raises(ValueError, match="invalid_parameters"):
        update_thermostat(Thermostat(), 0.3, window_bypass="on")


# --- Safety -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ("valid", "safe"),
        ("stale", "sensor_fault"),
        ("unavailable", "sensor_fault"),
        ("missing", "sensor_fault"),
    ],
)
def test_safety(quality, expected):
    assert safety_state(Reading(None, quality)) == (expected, quality)


# --- Trend --------------------------------------------------------------------


def test_first_sample_has_no_slope():
    trend = update_trend(None, 21.0, NOW, NOW)
    assert trend == Trend(21.0, None, NOW)


def test_rising_temperature_gives_a_positive_slope():
    trend = Trend(20.0, None, NOW - timedelta(minutes=10))
    trend = update_trend(trend, 21.0, NOW, NOW)
    assert 20.0 < trend.ema < 21.0
    assert trend.slope > 0
    falling = update_trend(Trend(22.0, 0.0, NOW - timedelta(minutes=10)), 21, NOW, NOW)
    assert falling.slope < 0


def test_same_report_is_not_counted_twice():
    trend = Trend(21.0, 0.5, NOW)
    assert update_trend(trend, 21.0, NOW, NOW + timedelta(minutes=5)) is None


def test_unchanged_value_lets_the_slope_decay():
    trend = Trend(21.0, 1.0, NOW - timedelta(minutes=15))
    later = update_trend(trend, 21.0, NOW - timedelta(minutes=15), NOW)
    assert later.at == NOW
    assert 0 <= later.slope < 1.0


# --- Automatic fan ------------------------------------------------------------

SPEEDS = ("25%", "50%", "75%", "100%")


def test_fan_speeds_exclude_off_and_auto():
    assert fan_speeds(("off", "25%", "50%", "auto")) == ("25%", "50%")


@pytest.mark.parametrize(
    ("mode", "room", "expected"),
    [
        ("heat", 21.5, "25%"),  # At or above target (21.3): lowest.
        ("heat", 21.0, "25%"),  # 0.3 below.
        ("heat", 20.7, "50%"),  # 0.6 below.
        ("heat", 20.2, "75%"),  # 1.1 below.
        ("heat", 18.0, "100%"),  # Far below: highest.
        ("cool", 24.5, "50%"),  # 0.8 above 23.7.
        ("cool", 27.0, "100%"),
        ("dry", 27.0, "25%"),  # No heating or cooling: lowest.
        ("heat", None, "25%"),  # No room temperature: lowest.
    ],
)
def test_auto_fan(mode, room, expected):
    assert auto_fan(SPEEDS, mode, room, 21.0, 24.0, 0.3) == expected


@pytest.mark.parametrize(
    ("step", "room", "expected"),
    [
        (1.0, 20.7, "25%"),  # 0.6 below: within the first degree.
        (1.0, 19.9, "50%"),  # 1.4 below.
        (0.25, 20.7, "75%"),  # 0.6 below: two steps of 0.25.
        (0.25, 20.2, "100%"),
    ],
)
def test_auto_fan_step_size(step, room, expected):
    """0.13.1: the °C per fan speed step is a central setting."""
    assert auto_fan(SPEEDS, "heat", room, 21.0, 24.0, 0.3, step) == expected


def test_auto_fan_step_setting():
    from custom_components.zonentra.models import build_model

    assert build_model("b", "B", {}).settings.auto_fan_step == 0.5
    settings = {"building": {"auto_fan_step": 1.0}}
    assert build_model("b", "B", settings).settings.auto_fan_step == 1.0
    for wrong in (0.1, 3):
        with pytest.raises(ValueError):
            build_model("b", "B", {"building": {"auto_fan_step": wrong}})


@pytest.mark.parametrize(
    ("unit_fans", "expected"),
    [
        (("off", "25%", "50%", "75%", "100%"), ["low", "middle", "medium", "high"]),
        (("auto", "quiet", "normal", "max"), ["low", "middle", "high"]),
        (("slow", "fast"), ["low", "high"]),
        (("1", "2", "3", "4", "5"), ["1", "2", "3", "4", "5"]),  # Kept as is.
        (("off", "auto"), []),
    ],
)
def test_standard_fan_names(unit_fans, expected):
    """0.13.2: HomeKit only understands Home Assistant's standard names."""
    assert list(fan_names(unit_fans)) == expected


def test_fan_names_both_ways():
    fans = ("off", "25%", "50%", "75%", "100%")
    assert unit_fan("medium", fans) == "75%"
    assert unit_fan("75%", fans) == "75%"  # A stored unit speed still works.
    assert unit_fan("turbo", fans) is None
    assert unit_fan(None, fans) is None


def test_auto_fan_without_speeds():
    assert auto_fan((), "heat", 18, 21, 24, 0.3) is None


# --- Vane ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        ({"cool"}, "cool"),
        ({"dry"}, "cool"),
        ({"heat", "off"}, "heat"),
        ({"fan_only"}, None),
        ({"off"}, None),
        (set(), None),
    ],
)
def test_vane_mode(modes, expected):
    assert vane_mode(modes) == expected


def test_vane_command_for_select_and_number():
    assert vane_command("select.vane", "cool", "Up", "Down") == (
        "select",
        "select_option",
        {"entity_id": "select.vane", "option": "Up"},
    )
    assert vane_command("input_number.vane", "heat", "1", "4,5") == (
        "input_number",
        "set_value",
        {"entity_id": "input_number.vane", "value": 4.5},
    )
    assert vane_command("select.vane", "heat", "Up", None) is None  # Not set.
    assert vane_command("number.vane", "cool", "up", "down") is None  # Not a number.


def test_vane_switch_for_auto_or_swing():
    assert vane_command("switch.vane_auto", "cool", "on", "on") == (
        "switch",
        "turn_on",
        {"entity_id": "switch.vane_auto"},
    )
    assert vane_command("input_boolean.vane", "heat", "on", "OFF")[1] == "turn_off"
    assert vane_command("switch.vane_auto", "cool", "up", "on") is None


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_auto_fan_hysteresis_retains_speed_near_boundary(mode):
    def choose(distance, previous):
        room = 21.3 - distance if mode == "heat" else 23.7 + distance
        return auto_fan(SPEEDS, mode, room, 21, 24, 0.3, previous=previous)

    assert choose(0.51, "25%") == "25%"
    assert choose(0.49, "50%") == "50%"
    assert choose(0.65, "25%") == "50%"
    assert choose(0.35, "50%") == "25%"
    assert choose(2.0, "25%") == "100%"
    assert choose(0, "100%") == "25%"


def test_auto_fan_hysteresis_handles_custom_steps_and_unknown_previous():
    assert auto_fan(SPEEDS, "heat", 21.09, 21, 24, 0.3, 0.2, previous="25%") == "25%"
    assert auto_fan(SPEEDS, "heat", 21.01, 21, 24, 0.3, 0.2, previous="25%") == "50%"
    assert auto_fan(SPEEDS, "heat", 20.7, 21, 24, 0.3, previous="auto") == "50%"
    assert auto_fan(SPEEDS, "dry", 27, 21, 24, 0.3, previous="100%") == "25%"
    assert auto_fan(SPEEDS, "heat", None, 21, 24, 0.3, previous="100%") == "25%"
    assert auto_fan((), "heat", 20, 21, 24, 0.3, previous="100%") is None
