"""0.12.0 pure tests: the weather forecast makes a day warm, neutral or cold."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.zonentra.demand import ZoneSnapshot, calculate_demand
from custom_components.zonentra.forecast import (
    Forecast,
    day_type,
    demand_margins,
    summarize_forecast,
)
from custom_components.zonentra.models import Zone, build_model
from custom_components.zonentra.observations import Reading
from custom_components.zonentra.shading import decide_screen

NOW = datetime(2026, 7, 1, 8, 20, tzinfo=UTC)


def hourly(*temperatures, start=NOW.replace(minute=0)):
    return [
        {"datetime": (start + timedelta(hours=hour)).isoformat(), "temperature": t}
        for hour, t in enumerate(temperatures)
    ]


# --- Summary of the coming hours ----------------------------------------------


def test_summary_of_the_coming_twelve_hours():
    # The running hour (08:00) counts; hours beyond 12 from now do not.
    entries = hourly(14, 16, 19, 23, 25, 24, 22, 20, 18, 17, 16, 15, 14, 30)
    forecast = summarize_forecast(entries, NOW)
    assert (forecast.maximum, forecast.minimum, forecast.hours) == (25, 14, 13)
    assert forecast.fetched_at == NOW


def test_summary_skips_past_and_broken_entries():
    entries = [
        *hourly(30, start=NOW - timedelta(hours=3)),  # Past.
        {"datetime": "not a date", "temperature": 40},
        {"datetime": NOW.isoformat(), "temperature": None},
        {"datetime": NOW.isoformat(), "temperature": True},
        {"temperature": 40},
        "nonsense",
        *hourly(18, start=NOW + timedelta(minutes=40)),
    ]
    forecast = summarize_forecast(entries, NOW)
    assert (forecast.maximum, forecast.minimum, forecast.hours) == (18, 18, 1)


def test_empty_forecast_has_no_temperatures():
    assert summarize_forecast([], NOW) == Forecast(fetched_at=NOW)
    assert summarize_forecast(None, NOW).maximum is None


# --- Day type -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("maximum", "expected"),
    [(22, "warm"), (26, "warm"), (21.9, "neutral"), (12.1, "neutral"), (12, "cold")],
)
def test_day_type(maximum, expected):
    forecast = Forecast(maximum, maximum - 5, 12, NOW)
    assert day_type(forecast, NOW, 22, 12) == expected


def test_day_type_unknown_without_a_fresh_forecast():
    assert day_type(None, NOW, 22, 12) == "unknown"
    assert day_type(Forecast(fetched_at=NOW), NOW, 22, 12) == "unknown"
    old = Forecast(25, 15, 12, NOW - timedelta(hours=3, minutes=1))
    assert day_type(old, NOW, 22, 12) == "unknown"


def test_margins():
    assert demand_margins("warm", 1.0) == (1.0, 0.0)
    assert demand_margins("cold", 1.0) == (0.0, 1.0)
    assert demand_margins("neutral", 1.0) == (0.0, 0.0)
    assert demand_margins("unknown", 1.0) == (0.0, 0.0)


# --- Later start of heating / cooling -----------------------------------------

ZONE = Zone("r", "R", hysteresis=0.3)


def snapshot(temperature):
    def valid(value):
        return Reading(value, "valid")

    missing = Reading(None, "missing")
    return ZoneSnapshot(
        "r",
        valid(temperature),
        valid(21.0),
        valid(24.0),
        missing,
        missing,
        (),
        (),
        (),
        (),
    )


@pytest.mark.parametrize(
    ("temperature", "margins", "demand", "reason"),
    [
        (20.5, (0, 0), "heating_demand", "below_heating_limit"),
        (20.5, (1, 0), "neutral", "heating_deferred_warm_day"),
        (19.6, (1, 0), "heating_demand", "below_heating_limit"),
        (24.5, (0, 0), "cooling_demand", "above_cooling_limit"),
        (24.5, (0, 1), "neutral", "cooling_deferred_cold_day"),
        (25.4, (0, 1), "cooling_demand", "above_cooling_limit"),
        (22.5, (1, 1), "neutral", "within_comfort_band"),
    ],
)
def test_margin_delays_the_start(temperature, margins, demand, reason):
    result = calculate_demand(ZONE, snapshot(temperature), "neutral", *margins)
    assert (result.demand, result.reason) == (demand, reason)


def test_running_demand_ends_as_usual():
    # Already heating: the margin does not stop it before heat + hysteresis.
    result = calculate_demand(ZONE, snapshot(20.9), "heating_demand", 1.0, 0.0)
    assert result.demand == "heating_demand"


# --- Screens ------------------------------------------------------------------


def screen(**kwargs):
    defaults = {
        "elevation": 40,
        "sunny": True,
        "on_facade": True,
        "demand": "neutral",
        "room_temperature": 22.0,  # Cool half of 21-24.
        "heating_limit": 21.0,
        "cooling_limit": 24.0,
        "shade_position": 0,
        "previous": None,
    }
    decision = decide_screen(**defaults | kwargs)
    return decision.target, decision.reason


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ("open", "room_cool")),
        ({"day": "unknown"}, ("open", "room_cool")),
        ({"day": "neutral"}, ("open", "room_cool")),
        ({"day": "warm"}, ("shade", "warm_day_forecast")),
        ({"day": "cold", "room_temperature": 23.5}, ("open", "cold_day_forecast")),
        ({"day": "warm", "occupied": False}, ("shade", "empty_room_keep_cool")),
        ({"day": "neutral", "occupied": False}, ("shade", "empty_room_keep_cool")),
        ({"day": "cold", "occupied": False}, ("open", "empty_room_solar_gain")),
        # Without a forecast an empty room still follows its temperature.
        ({"occupied": False}, ("open", "room_cool")),
        # The room's own demand and the night come first.
        ({"day": "warm", "demand": "heating_demand"}, ("open", "solar_gain_heating")),
        ({"day": "cold", "demand": "cooling_demand"}, ("shade", "cooling")),
        ({"day": "warm", "elevation": -2}, ("open", "night")),
        ({"day": "warm", "sunny": False}, ("open", "no_sun")),
    ],
)
def test_screen_with_forecast(kwargs, expected):
    assert screen(**kwargs) == expected


# --- Configuration ------------------------------------------------------------


def building(**settings):
    return build_model("b", "B", {"building": settings})


def test_forecast_settings():
    settings = building(weather_entity="weather.home").settings
    assert settings.weather_entity == "weather.home"
    assert (
        settings.warm_day_temperature,
        settings.cold_day_temperature,
        settings.forecast_margin,
    ) == (22.0, 12.0, 1.0)
    # Fetched by service, not a state input.
    assert "weather.home" not in building(weather_entity="weather.home").entity_ids


@pytest.mark.parametrize(
    "settings",
    [
        {"weather_entity": "sensor.weather"},
        {"forecast_margin": 4},
        {"warm_day_temperature": 12, "cold_day_temperature": 12},
        {"cold_day_temperature": 50},
    ],
)
def test_invalid_forecast_settings(settings):
    with pytest.raises(ValueError):
        building(**settings)


@pytest.mark.parametrize(
    ("unit", "values"),
    [
        ("°C", (5, 10)),
        ("°F", (41, 50)),
        ("K", (278.15, 283.15)),
    ],
)
def test_forecast_normalizes_temperature_units(unit, values):
    forecast = summarize_forecast(hourly(*values), NOW, temperature_unit=unit)
    assert forecast.minimum == pytest.approx(5)
    assert forecast.maximum == pytest.approx(10)
    assert day_type(forecast, NOW, 22, 12) == "cold"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_forecast_rejects_nonfinite_or_boolean_temperature(value):
    forecast = summarize_forecast(hourly(value, 50), NOW, temperature_unit="°F")
    assert (forecast.minimum, forecast.maximum, forecast.hours) == (10, 10, 1)


def test_unknown_forecast_unit_produces_unknown_day():
    forecast = summarize_forecast(hourly(50), NOW, temperature_unit="unsupported")
    assert day_type(forecast, NOW, 22, 12) == "unknown"
