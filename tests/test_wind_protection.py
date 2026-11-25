"""Current weather wind/gust protection, calm hold and rain coexistence."""

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics
from custom_components.zonentra.models import build_model
from custom_components.zonentra.wind import (
    WindMemory,
    bearing_degrees,
    speed_kmh,
    wind_protection,
)
from tests.test_entry_layout import add_zone, zone_options
from tests.test_followup_improvements import restart_central
from tests.test_rain_protection import WEATHER, lower_screen
from tests.test_shading_runtime import EQUIPMENT, SCREEN, office
from tests.test_thermostat_runtime import later


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (10, "m/s", 36),
        (10, "km/h", 10),
        (10, "mph", 16.09344),
        (10, "kn", 18.52),
        (10, "ft/s", 10.9728),
    ],
)
def test_wind_unit_conversion(value, unit, expected):
    assert speed_kmh(value, unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value,unit",
    [
        (True, "km/h"),
        (-999, "m/s"),
        (float("nan"), "km/h"),
        (10, None),
        (10, "°C"),
        (None, "km/h"),
        ("10", "km/h"),
    ],
)
def test_unusable_wind_values_are_unknown(value, unit):
    assert speed_kmh(value, unit) is None


def test_wind_limit_hysteresis_and_calm_hold():
    now = dt_util.utcnow()
    wind = wind_protection(40, 30, 40, 50, WindMemory(), now, 30)
    assert wind.open_required and wind.status == "wind"
    # Below the trigger alone is insufficient: release requires <=80%.
    assert wind_protection(35, 30, 40, 50, wind.memory, now, 30).status == "wind"
    calm = wind_protection(32, 40, 40, 50, wind.memory, now, 30)
    assert calm.status == "calming"
    assert calm.next_check == now + timedelta(minutes=30)
    assert (
        wind_protection(
            32, 40, 40, 50, calm.memory, now + timedelta(minutes=30), 30
        ).status
        == "calm"
    )
    unknown = wind_protection(
        30, None, 40, 50, calm.memory, now + timedelta(minutes=29), 30
    )
    assert unknown.open_required and unknown.memory.calm_since is None
    assert wind_protection(30, 50, 40, 50, calm.memory, now, 30).status == "wind"


def test_known_dangerous_wind_wins_over_missing_other_reading():
    now = dt_util.utcnow()
    assert wind_protection(None, 60, 40, 50, WindMemory(), now, 30).open_required
    assert wind_protection(45, None, 40, 50, WindMemory(), now, 30).open_required
    unknown = wind_protection(20, None, 40, 50, WindMemory(), now, 30)
    assert (
        unknown.status == "unknown"
        and unknown.block_closing
        and not unknown.open_required
    )
    assert wind_protection(20, None, 40, None, WindMemory(), now, 30).status == "calm"
    assert (
        wind_protection(100, 100, None, None, WindMemory(), now, 30).status == "unknown"
    )


async def report_wind(
    hass, speed=0, gust=0, unit="km/h", condition="sunny", *, bearing=None
):
    hass.states.async_set(
        WEATHER,
        condition,
        {
            "wind_speed": speed,
            "wind_gust_speed": gust,
            "wind_speed_unit": unit,
            "wind_bearing": bearing,
        },
    )
    await hass.async_block_till_done()


async def windy_office(hass, **settings):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    await report_wind(hass)
    central, room = await office(
        hass,
        weather_entity=WEATHER,
        screen_wind_speed_limit=40,
        screen_wind_gust_limit=50,
        **settings,
    )
    central.runtime_data.set_thermostat("west", screen_regulation=True)
    await hass.async_block_till_done()
    return central, room, moves


async def test_gust_opens_immediately_and_final_guard_rejects_live_wind_close(hass):
    central, _, moves = await windy_office(hass)
    runtime = central.runtime_data
    await lower_screen(hass, runtime)
    await report_wind(hass, 10, 51)
    assert [call.data["position"] for call in moves] == [100]
    assert runtime.screen_decisions[EQUIPMENT].reason == "wind"
    item = next(item for item in runtime.building.equipment if item.kind == "shading")
    entry = {"equipment_id": item.id, "reason": "screen_cooling"}
    assert not await runtime._call(
        "set_cover_position", {"entity_id": SCREEN, "position": 0}, entry, "cover"
    )
    assert entry["result"] == "blocked: wind_blocked"


async def test_wind_readings_use_published_units(hass):
    central, _, moves = await windy_office(hass)
    runtime = central.runtime_data
    await lower_screen(hass, runtime)
    await report_wind(hass, 12, 10, "m/s")
    assert runtime.wind_speed == pytest.approx(43.2)
    assert runtime.wind.status == "wind"
    assert [call.data["position"] for call in moves] == [100]


@pytest.mark.parametrize(
    "bearing,facade,opens",
    [
        (69, "east", True),
        (69, "west", False),
        (350, "north", True),
        (360, "east", True),  # Crosswind at exactly 90°.
        (270, None, True),
        (None, "west", True),
        ("E", "west", True),
        (True, "west", True),
        (float("nan"), "west", True),
        (361, "west", True),
    ],
)
def test_wind_alarm_applies_to_facade(bearing, facade, opens):
    alarm = wind_protection(45, 55, 40, 50, WindMemory(), dt_util.utcnow(), 30)
    wind = alarm.for_facade(bearing_degrees(bearing), facade)
    assert wind.open_required == wind.block_closing == opens


async def test_wind_opens_only_exposed_screens_and_rechecks_direction(hass):
    north_screen = "cover.north_screen"
    hass.states.async_set(north_screen, "closed", {"current_position": 0})
    central, _, moves = await windy_office(hass)
    north_room = await add_zone(
        hass, zone_options("north", shading_entities=[north_screen], facade="north")
    )
    runtime = central.runtime_data
    runtime.set_thermostat("north", screen_regulation=True)
    await lower_screen(hass, runtime)  # The screen from office() faces south.
    south = next(
        item for item in runtime.building.equipment if item.entity_id == SCREEN
    )
    north = next(
        item for item in runtime.building.equipment if item.entity_id == north_screen
    )

    await report_wind(hass, 45, 55, bearing=180)
    assert [call.data["entity_id"] for call in moves] == [SCREEN]
    data = await async_get_config_entry_diagnostics(hass, north_room)
    assert data["screen_wind"]["bearing"] == 180
    assert data["screens"][0]["wind_status"] == "leeward"
    assert runtime._screen_guard(south, position=0) == "wind_blocked"
    assert runtime._screen_guard(north, position=0) is None
    hass.states.async_set(SCREEN, "open", {"current_position": 100})
    hass.states.async_set(
        WEATHER,
        "sunny",
        {
            "wind_speed": 45,
            "wind_gust_speed": 55,
            "wind_speed_unit": "km/h",
            "wind_bearing": 0,
        },
    )
    assert runtime._screen_guard(south, position=0) is None
    assert runtime._screen_guard(north, position=0) == "wind_blocked"
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in moves] == [SCREEN, north_screen]


@pytest.mark.parametrize("condition", ["windy", "windy-variant"])
async def test_wind_condition_does_not_reopen_manually_lowered_screen(hass, condition):
    central, _, moves = await windy_office(hass)
    runtime = central.runtime_data
    await report_wind(hass, 25.88, 38.83, condition=condition)
    await lower_screen(hass, runtime)
    assert runtime.wind.status == "calm"
    assert moves == []

    await report_wind(hass, 25.88, 50, condition=condition)
    assert runtime.wind.status == "wind"
    assert [call.data["position"] for call in moves] == [100]

    await report_wind(hass, 25.88, 38.83, condition=condition)
    assert runtime.wind.status == "calming"


async def test_wind_and_rain_both_must_release_before_closing(hass, freezer):
    central, _, moves = await windy_office(hass)
    runtime = central.runtime_data
    await lower_screen(hass, runtime)
    # Clear the manual pause for the normal control that resumes afterwards.
    runtime.screen_overrides.clear()
    await report_wind(hass, 45, 55, condition="rainy")
    hass.states.async_set(SCREEN, "open", {"current_position": 100})
    await hass.async_block_till_done()
    await report_wind(hass, 10, 20, condition="rainy")
    await later(hass, freezer, minutes=10)
    await report_wind(hass, 10, 20, condition="sunny")
    await later(hass, freezer, minutes=20)
    assert runtime.wind.status == "calm"
    assert runtime.rain.status == "drying"
    assert [call.data["position"] for call in moves] == [100]
    await later(hass, freezer, minutes=10)
    assert runtime.rain.status == "dry"
    await later(hass, freezer, minutes=5)
    assert [call.data["position"] for call in moves] == [100, 0]


async def test_stale_wind_never_clears_active_protection(hass, freezer):
    central, _, moves = await windy_office(hass, screen_wind_max_age_minutes=10)
    runtime = central.runtime_data
    await lower_screen(hass, runtime)
    await report_wind(hass, 45, 55)
    hass.states.async_set(SCREEN, "open", {"current_position": 100})
    await hass.async_block_till_done()
    await report_wind(hass, 10, 20)
    await later(hass, freezer, minutes=11)
    assert runtime.wind.status == "unknown"
    assert runtime.wind.open_required
    await report_wind(hass, 10, 20)
    assert runtime.wind.status == "calming"
    assert runtime.wind.next_check == dt_util.utcnow() + timedelta(minutes=30)
    assert [call.data["position"] for call in moves] == [100]


async def test_without_known_thresholds_uses_current_windy_condition(hass):
    await report_wind(hass, 100, 120)
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await office(hass, weather_entity=WEATHER)
    runtime = central.runtime_data
    runtime.set_thermostat("west", screen_regulation=True)
    assert runtime.wind.status == "calm"
    assert runtime.wind_mode == "weather_condition"
    await lower_screen(hass, runtime)
    assert moves == []
    await report_wind(hass, 10, 20, condition="windy")
    assert [call.data["position"] for call in moves] == [100]
    assert runtime.wind.status == "wind"


async def test_unknown_gust_blocks_lowering_without_inventing_wind_alarm(hass, freezer):
    central, _, moves = await windy_office(hass)
    await report_wind(hass, 10, None, condition="windy")
    await later(hass, freezer, minutes=10)
    assert central.runtime_data.wind.status == "unknown"
    assert not central.runtime_data.wind.open_required
    assert central.runtime_data.screen_decisions[EQUIPMENT].reason == "wind_unknown"
    assert moves == []


async def test_wind_latch_survives_full_restart(hass, freezer):
    central, _, _ = await windy_office(hass)
    await report_wind(hass, 45, 55)
    await report_wind(hass, 10, 20)
    await later(hass, freezer, minutes=20)
    await later(hass, freezer, seconds=2)
    runtime = await restart_central(hass, central)
    assert runtime.wind.status == "calming"
    assert runtime.wind.next_check == dt_util.utcnow() + timedelta(minutes=30)


async def test_wind_calm_timer_survives_configuration_reload(hass, freezer):
    central, _, _ = await windy_office(hass)
    await report_wind(hass, 45, 55)
    await report_wind(hass, 10, 20)
    deadline = central.runtime_data.wind.next_check
    await later(hass, freezer, minutes=10)
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    assert central.runtime_data.wind.next_check == deadline


async def test_hardware_interlock_remains_authoritative_during_weather_wind(hass):
    hass.states.async_set("binary_sensor.wind_lock", "on")
    central, _, moves = await windy_office(
        hass, screen_block_entity="binary_sensor.wind_lock"
    )
    runtime = central.runtime_data
    await lower_screen(hass, runtime)
    await report_wind(hass, 45, 55)
    assert runtime.screen_moves[EQUIPMENT].status == "blocked"
    assert moves == []
    hass.states.async_set("binary_sensor.wind_lock", "off")
    await hass.async_block_till_done()
    assert [call.data["position"] for call in moves] == [100]


async def test_diagnostics_explain_wind_without_exposing_source(hass):
    central, room, _ = await windy_office(hass)
    await report_wind(hass, 45, 55)
    for entry in [central, room]:
        data = await async_get_config_entry_diagnostics(hass, entry)
        assert WEATHER not in str(data)
        assert data["screen_wind"]["status"] == "wind"
        assert data["screen_wind"]["mode"] == "limits"
        assert data["screen_wind"]["entity_id"] == "**REDACTED**"
        assert data["screen_wind"]["gust_kmh"] == 55


@pytest.mark.parametrize(
    "key,value",
    [
        ("screen_wind_speed_limit", 0),
        ("screen_wind_gust_limit", True),
        ("screen_wind_protection", 1),
        ("screen_wind_calm_minutes", float("nan")),
        ("screen_wind_max_age_minutes", 181),
    ],
)
def test_invalid_wind_configuration(key, value):
    with pytest.raises(ValueError):
        build_model("test", "Test", {"building": {key: value}})


@pytest.mark.parametrize("condition", ["windy", "windy-variant"])
def test_current_wind_condition_is_protective_without_guessed_limits(condition):
    now = dt_util.utcnow()
    wind = wind_protection(
        None, None, None, None, WindMemory(), now, 30, condition=condition
    )
    assert wind.open_required
    calm = wind_protection(
        None, None, None, None, wind.memory, now, 30, condition="sunny"
    )
    assert calm.status == "calming"
    unknown = wind_protection(
        None, None, None, None, calm.memory, now + timedelta(minutes=29), 30
    )
    assert unknown.open_required and unknown.memory.calm_since is None
    assert (
        wind_protection(
            None,
            None,
            None,
            None,
            calm.memory,
            now + timedelta(minutes=30),
            30,
            condition="cloudy",
        ).status
        == "calm"
    )


@pytest.mark.parametrize("value,unit", [(1e308, "m/s"), (10, {}), (10, [])])
def test_malformed_wind_attributes_do_not_crash_runtime(value, unit):
    assert speed_kmh(value, unit) is None
