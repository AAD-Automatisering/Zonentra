"""Regressions for timing, feedback, retries, overrides and source recovery."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.zonentra.bookings import ThermalRates
from custom_components.zonentra.executor import ModeCommand
from custom_components.zonentra.models import build_model
from custom_components.zonentra.override import ManualOverride
from custom_components.zonentra.runtime import BuildingRuntime
from custom_components.zonentra.shading import SunMemory, sunny_state
from tests.test_entry_layout import CELSIUS
from tests.test_mode_control_runtime import office as group_office
from tests.test_thermostat_runtime import (
    UNIT,
    clear,
    later,
    mock_climate,
)


def standalone(hass, *, mode="heat", temperature="20", fan="25%", **zone):
    hass.states.async_set("sensor.review_temperature", temperature, CELSIUS)
    hass.states.async_set(
        UNIT,
        mode,
        {
            "temperature": 22,
            "hvac_action": "heating" if mode == "heat" else "off",
            "hvac_modes": ["off", "heat", "cool", "fan_only"],
            "fan_modes": ["25%", "50%", "75%"],
            "fan_mode": fan,
        },
    )
    building = build_model(
        "review",
        "Review",
        {
            "building": {"heating_limit": 22, "cooling_limit": 24},
            "zones": [
                {
                    "id": "west",
                    "name": "West",
                    "temperature_entity": "sensor.review_temperature",
                    **zone,
                }
            ],
            "equipment": [
                {
                    "id": "ac",
                    "name": "AC",
                    "kind": "hvac",
                    "zone_id": "west",
                    "entity_id": UNIT,
                }
            ],
        },
    )
    runtime = BuildingRuntime(hass, building, active=True)
    runtime.refresh()
    return runtime


async def test_fan_failure_is_retried_with_backoff(hass, freezer):
    await mock_climate(hass)
    failed = async_mock_service(
        hass, "climate", "set_fan_mode", raise_exception=HomeAssistantError("offline")
    )
    runtime = standalone(hass)
    runtime.set_thermostat("west", regulation=True, fan_mode="high")
    await hass.async_block_till_done()
    assert len(failed) == 1
    assert "ac" not in runtime.fan_applied
    assert hass.states.get(UNIT).attributes["fan_mode"] == "25%"
    await later(hass, freezer, seconds=119)
    assert len(failed) == 1
    await later(hass, freezer, seconds=2)
    assert len(failed) == 2
    runtime.refresh()
    await hass.async_block_till_done()
    assert len(failed) == 2
    await later(hass, freezer, seconds=120)
    assert len(failed) == 2  # Second failure backs off for four minutes.
    await later(hass, freezer, seconds=121)
    assert len(failed) == 3
    runtime.close()


async def test_vane_failure_is_not_marked_applied_and_retries(hass, freezer):
    await mock_climate(hass)
    failed = async_mock_service(
        hass, "select", "select_option", raise_exception=HomeAssistantError("offline")
    )
    hass.states.async_set("select.review_vane", "1")
    runtime = standalone(
        hass, vane_entity="select.review_vane", vane_heat_value="5", vane_cool_value="1"
    )
    await hass.async_block_till_done()
    assert len(failed) == 1
    assert "west" not in runtime.vane_applied
    await later(hass, freezer, seconds=119)
    assert len(failed) == 1
    recovered = async_mock_service(hass, "select", "select_option")
    await later(hass, freezer, seconds=2)
    assert len(recovered) == 1
    assert runtime.vane_applied["west"] == "heat"
    runtime.close()


async def test_manual_override_pauses_automatic_fan_changes(hass):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, temperature="22.1")
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    assert not mocks["set_fan_mode"]
    now = dt_util.utcnow()
    runtime.overrides["ac"] = ManualOverride(
        "ac", now, now + timedelta(minutes=30), "heat"
    )
    hass.states.async_set("sensor.review_temperature", "20", CELSIUS)
    runtime.refresh()
    await hass.async_block_till_done()
    assert runtime.overrides["ac"].active(now)
    assert runtime.setpoint_plans["ac"].status == "manual_override"
    assert not mocks["set_fan_mode"]
    runtime.close()


async def test_room_disable_blocks_queued_room_command(hass):
    mocks = await mock_climate(hass)
    runtime = standalone(hass)
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    command = ModeCommand(
        "ac", UNIT, "set_temperature", None, 0, "self_regulation", temperature=25
    )
    entry = runtime._log(command, dt_util.utcnow(), "sending")
    runtime.set_thermostat("west", regulation=False)
    clear(mocks)
    sent = await runtime._call(
        "set_temperature", {"entity_id": UNIT, "temperature": 25}, entry
    )
    assert not runtime.regulated("west")
    assert not sent
    assert not mocks["set_temperature"]
    assert entry["result"] == "blocked: room_disabled"
    runtime.close()


async def test_group_preset_respects_a_recent_stop(hass, freezer):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    runtime = central.runtime_data
    hass.states.async_set(UNIT, "heat", {"hvac_action": "heating"})
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=4)
    hass.states.async_set(UNIT, "off", context=Context(parent_id="test"))
    await hass.async_block_till_done()
    runtime.command_tracker.cancel(UNIT)
    runtime.pending_offs.clear()
    group = replace(runtime.building.groups[0], min_switch_seconds=0)
    runtime.building = replace(
        runtime.building,
        groups=(group,),
        settings=replace(runtime.building.settings, changeover_wait_seconds=0),
    )
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", "27", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.building.settings.min_off_seconds == 180
    assert (dt_util.utcnow() - hass.states.get(UNIT).last_changed).total_seconds() < 1
    assert not mocks["set_hvac_mode"]
    assert runtime.mode_controls["outdoor_1"].status == "minimum_off_time"
    await later(hass, freezer, seconds=179)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["cool"]


async def test_group_switch_stops_confirms_and_waits_before_opposite_mode(
    hass, freezer
):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    runtime = central.runtime_data
    hass.states.async_set(UNIT, "heat", {"hvac_action": "heating"})
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=16)
    runtime.pending_offs.clear()
    runtime.command_tracker.cancel(UNIT)
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", "27", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.building.settings.changeover_wait_seconds == 300
    assert runtime.coordinations["outdoor_1"].phase == "stopping"
    assert not mocks["set_hvac_mode"]
    assert mocks["turn_off"]
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    assert runtime.mode_controls["outdoor_1"].status == "changeover_wait"
    await later(hass, freezer, seconds=299)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["cool"]


async def test_missing_command_confirmation_retries_without_external_events(
    hass, freezer
):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, mode="off")
    await later(hass, freezer, minutes=4)
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    assert len(mocks["set_hvac_mode"]) == 1
    assert runtime.command_tracker.is_pending(UNIT, dt_util.utcnow())
    assert runtime._cancel_deadline is not None
    await later(hass, freezer, seconds=61)
    assert len(mocks["set_hvac_mode"]) == 1
    assert runtime.command_log[-1]["confirmation"] == "timeout"
    await later(hass, freezer, seconds=121)
    assert len(mocks["set_hvac_mode"]) == 2
    runtime.close()


def test_unknown_solar_power_restarts_continuous_sun_delay():
    now = dt_util.utcnow()
    _, memory, _ = sunny_state(2000, 1000, SunMemory(), now)
    _, memory, _ = sunny_state(None, 1000, memory, now + timedelta(minutes=1))
    sunny, _, check = sunny_state(2000, 1000, memory, now + timedelta(minutes=10))
    assert sunny is False
    assert check == now + timedelta(minutes=15)


async def test_learning_does_not_promote_old_readings_on_timer_events(hass, freezer):
    from tests.test_bookings_runtime import later as booking_later
    from tests.test_bookings_runtime import office

    await mock_climate(hass)
    central, _, _ = await office(
        hass, temperature="18", outside="5", start=20, units=True
    )
    runtime = central.runtime_data
    runtime.building = replace(
        runtime.building,
        settings=replace(runtime.building.settings, outdoor_max_age_seconds=0),
    )
    runtime.set_thermostat("west", regulation=True)
    hass.states.async_set(
        UNIT, "heat", {"hvac_action": "heating"}, context=Context(parent_id="test")
    )
    await hass.async_block_till_done()
    original = runtime.thermal_samples["west"]
    await booking_later(hass, freezer, minutes=31)
    assert runtime.thermal_samples["west"] == original
    await booking_later(hass, freezer, minutes=5)
    hass.states.async_set("sensor.west_temperature", "19", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.thermal_rates["west"].get("cold", ThermalRates()).heating is None


async def test_cover_missing_confirmation_retries_without_external_events(
    hass, freezer
):
    from tests.test_shading_runtime import SCREEN
    from tests.test_shading_runtime import office as screen_office

    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await screen_office(hass)
    runtime = central.runtime_data
    await later(hass, freezer, minutes=6)
    runtime.set_thermostat("west", screen_regulation=True)
    await hass.async_block_till_done()
    assert not moves
    await later(hass, freezer, minutes=5)
    assert len(moves) == 1
    assert hass.states.get(SCREEN).attributes["current_position"] == 100
    assert runtime._cancel_deadline is not None
    await later(hass, freezer, minutes=5)
    assert len(moves) == 1
    await later(hass, freezer, minutes=25)
    assert len(moves) == 2


async def test_first_observation_off_waits_before_initial_preset(hass, freezer):
    from tests.test_entry_layout import add_zone, setup_central, zone_options

    mocks = await mock_climate(hass)
    hass.states.async_set(UNIT, "off")
    hass.states.async_set("sensor.west_temperature", "20", CELSIUS)
    central = await setup_central(hass)
    await add_zone(hass, zone_options("west", UNIT))
    assert not mocks["set_hvac_mode"]
    assert central.runtime_data.mode_controls["outdoor_1"].status == "minimum_off_time"
    await later(hass, freezer, seconds=179)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["heat"]


async def test_confirmed_fan_command_does_not_retry(hass, freezer):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, temperature="22.1")
    state = hass.states.get(UNIT)
    hass.states.async_set(UNIT, "heat", {**state.attributes, "temperature": 22.5})
    runtime.set_thermostat("west", regulation=True, fan_mode="high")
    await hass.async_block_till_done()
    state = hass.states.get(UNIT)
    hass.states.async_set(UNIT, "heat", {**state.attributes, "fan_mode": "75%"})
    runtime.refresh()
    await hass.async_block_till_done()
    assert not runtime.command_receipts
    assert runtime.command_log[-1]["confirmation"] == "confirmed"
    await later(hass, freezer, minutes=10)
    assert len(mocks["set_fan_mode"]) == 1
    runtime.close()


async def test_accepted_fan_without_feedback_retries(hass, freezer):
    mocks = await mock_climate(hass)
    runtime = standalone(hass)
    runtime.set_thermostat("west", regulation=True, fan_mode="high")
    await hass.async_block_till_done()
    assert len(mocks["set_fan_mode"]) == 1
    await later(hass, freezer, seconds=61)
    assert "ac" not in runtime.fan_applied
    await later(hass, freezer, seconds=121)
    assert len(mocks["set_fan_mode"]) == 2
    runtime.close()


async def test_fan_disable_is_rechecked_before_dispatch(hass):
    mocks = await mock_climate(hass)
    runtime = standalone(hass)
    runtime.set_thermostat("west", regulation=True, fan_control=False)
    await hass.async_block_till_done()
    clear(mocks)
    command = ModeCommand(
        "ac", UNIT, "set_fan_mode", None, 0, "thermostat_fan_mode", fan_mode="75%"
    )
    entry = runtime._log(command, dt_util.utcnow(), "sending")
    assert not await runtime._call(
        "set_fan_mode", {"entity_id": UNIT, "fan_mode": "75%"}, entry
    )
    assert entry["result"] == "blocked: fan_disabled"
    assert not mocks["set_fan_mode"]
    runtime.close()


async def test_changed_demand_supersedes_a_retry(hass, freezer):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, mode="off")
    await later(hass, freezer, minutes=4)
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=61)
    hass.states.async_set("sensor.review_temperature", "27", CELSIUS)
    runtime.refresh()
    await hass.async_block_till_done()
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["heat", "cool"]
    await later(hass, freezer, seconds=121)
    assert all(c.data["hvac_mode"] == "cool" for c in mocks["set_hvac_mode"][1:])
    runtime.close()


async def test_standalone_changeover_stops_and_waits(hass, freezer):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, temperature="27")
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=11)
    assert mocks["turn_off"]
    assert not mocks["set_hvac_mode"]
    hass.states.async_set(UNIT, "off")
    runtime.refresh()
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=299)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["cool"]
    runtime.close()


async def test_group_wait_survives_reload(hass, freezer):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    runtime = central.runtime_data
    hass.states.async_set(UNIT, "heat")
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=4)
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    hass.states.async_set(UNIT, "heat", context=Context(parent_id="automation"))
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=16)
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", "27", CELSIUS)
    await hass.async_block_till_done()
    assert mocks["turn_off"]
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    stopped_at = runtime.group_switches["outdoor_1"].stopped_at
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    assert central.runtime_data.group_switches["outdoor_1"].stopped_at == stopped_at
    await later(hass, freezer, seconds=299)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["cool"]


async def test_group_never_applies_opposite_mode_before_stop_confirmation(
    hass, freezer
):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    hass.states.async_set(UNIT, "heat")
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=16)
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", "27", CELSIUS)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=10)
    assert mocks["turn_off"]
    assert not mocks["set_hvac_mode"]
    assert (
        central.runtime_data.mode_controls["outdoor_1"].status
        == "stopping_for_changeover"
    )


async def test_accepted_pulse_off_without_feedback_retries_off_only(hass, freezer):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    hass.states.async_set(UNIT, "heat")
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=4)
    assert len(mocks["turn_off"]) == 1
    await later(hass, freezer, seconds=61)
    assert central.runtime_data.pending_offs
    await later(hass, freezer, seconds=121)
    assert len(mocks["turn_off"]) == 2
    assert len(mocks["set_hvac_mode"]) == 1


async def test_setpoint_without_feedback_retries_after_minimum_interval(hass, freezer):
    mocks = await mock_climate(hass)
    runtime = standalone(hass)
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    assert len(mocks["set_temperature"]) == 1
    await later(hass, freezer, seconds=61)
    await later(hass, freezer, seconds=121)
    assert len(mocks["set_temperature"]) == 1
    await later(hass, freezer, seconds=119)
    assert len(mocks["set_temperature"]) == 2
    runtime.close()


async def test_numeric_vane_confirmation_accepts_equivalent_number_strings(
    hass, freezer
):
    await mock_climate(hass)
    moves = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("number.review_vane", "1")
    runtime = standalone(
        hass, vane_entity="number.review_vane", vane_heat_value="5", vane_cool_value="1"
    )
    await hass.async_block_till_done()
    hass.states.async_set("number.review_vane", "5")
    runtime.refresh()
    assert runtime.command_log[-1]["confirmation"] == "confirmed"
    assert not runtime.command_receipts
    await later(hass, freezer, minutes=10)
    assert len(moves) == 1
    runtime.close()


async def test_manual_mode_correction_keeps_thirty_second_grace(hass, freezer):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    runtime = central.runtime_data
    hass.states.async_set(UNIT, "heat")
    await hass.async_block_till_done()
    runtime.building = replace(
        runtime.building, settings=replace(runtime.building.settings, min_on_seconds=0)
    )
    clear(mocks)
    hass.states.async_set(UNIT, "cool")
    await hass.async_block_till_done()
    assert not mocks["turn_off"]
    await later(hass, freezer, seconds=29)
    assert not mocks["turn_off"]
    await later(hass, freezer, seconds=2)
    assert mocks["turn_off"]


async def test_group_switch_preserves_resume_intent_through_unavailable(hass, freezer):
    mocks = await mock_climate(hass)
    central, _ = await group_office(hass)
    runtime = central.runtime_data
    hass.states.async_set(UNIT, "heat")
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=16)
    runtime.command_tracker.cancel(UNIT)
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", "27", CELSIUS)
    await hass.async_block_till_done()
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    assert runtime.group_switches["outdoor_1"].resume_units
    hass.states.async_set(UNIT, "unavailable")
    await hass.async_block_till_done()
    assert runtime.group_switches["outdoor_1"].resume_units
    await later(hass, freezer, seconds=30)
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=299)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["cool"]
    assert not runtime.pending_offs  # Originally running: resume, no preset-off.


async def test_standalone_wait_restarts_after_unavailable(hass, freezer):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, temperature="27")
    runtime.set_thermostat("west", regulation=True)
    await later(hass, freezer, minutes=11)
    hass.states.async_set(UNIT, "off")
    runtime.refresh()
    hass.states.async_set(UNIT, "unavailable")
    runtime.refresh()
    assert "ac" in runtime.room_switches
    await later(hass, freezer, seconds=30)
    hass.states.async_set(UNIT, "off")
    runtime.refresh()
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=299)
    assert not mocks["set_hvac_mode"]
    await later(hass, freezer, seconds=2)
    assert [c.data["hvac_mode"] for c in mocks["set_hvac_mode"]] == ["cool"]
    runtime.close()


@pytest.mark.parametrize("domain", ["switch", "input_boolean"])
async def test_vane_switch_off_without_feedback_retries_after_backoff(
    hass, freezer, domain
):
    await mock_climate(hass)
    calls = async_mock_service(hass, domain, "turn_off")
    entity_id = f"{domain}.review_vane"
    hass.states.async_set(entity_id, "on")
    runtime = standalone(
        hass, vane_entity=entity_id, vane_heat_value="off", vane_cool_value="on"
    )
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert runtime.vane_applied["west"] == "heat"
    await later(hass, freezer, seconds=61)
    assert "west" not in runtime.vane_applied
    assert len(calls) == 1
    await later(hass, freezer, seconds=119)
    assert len(calls) == 1
    await later(hass, freezer, seconds=2)
    assert len(calls) == 2
    hass.states.async_set(entity_id, "off")
    runtime.refresh()
    await hass.async_block_till_done()
    assert not runtime.command_receipts
    await later(hass, freezer, minutes=10)
    assert len(calls) == 2
    runtime.close()
