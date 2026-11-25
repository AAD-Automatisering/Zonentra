"""0.7.0 integration: active outdoor-stand control with mocked climate services."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.zonentra.const import DOMAIN
from tests.test_entry_layout import (
    CELSIUS,
    add_zone,
    entity,
    set_states,
    setup_central,
    zone_options,
)
from tests.test_thermostat_runtime import later

UNIT = "climate.west_ac"


async def mock_climate(hass):
    # 0.9: the zone thermostat loads the climate integration. Set it up first,
    # so its real services do not replace the mocks.
    assert await async_setup_component(hass, "climate", {})
    return (
        async_mock_service(hass, "climate", "set_hvac_mode"),
        async_mock_service(hass, "climate", "turn_off"),
    )


def calls(service_calls):
    return [(c.data["entity_id"], c.data.get("hvac_mode")) for c in service_calls]


async def fire(hass, moment):
    with patch("homeassistant.util.dt.utcnow", return_value=moment):
        async_fire_time_changed(hass, moment)
        await hass.async_block_till_done()


async def office(hass):
    set_states(hass, "west", UNIT)  # 21 °C: heating demand, unit off.
    central = await setup_central(hass)
    zone = await add_zone(hass, zone_options("west", UNIT))
    return central, zone


def switch_id(hass, central):
    return er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{central.entry_id}_group_outdoor_1_active_mode_control"
    )


async def test_preset_pulse_without_override_and_restart_memory(hass):
    set_mode, turn_off = await mock_climate(hass)
    central, zone = await office(hass)
    runtime = central.runtime_data
    assert hass.states.get(switch_id(hass, central)).state == "on"  # Active by default.
    assert calls(set_mode) == [(UNIT, "heat")]
    # 0.13.4: the reload after adding the zone keeps the switch state, so the
    # pulse is sent at once; later refreshes then report the stand in sync.
    assert runtime.mode_controls["outdoor_1"].status in (
        "group_mode_applied",
        "in_sync",
    )
    assert runtime.preset_memories["outdoor_1"].mode == "heat"
    # The integration applies the mode: Zonentra recognises its own command.
    hass.states.async_set(UNIT, "heat", {"hvac_action": "heating"})
    await hass.async_block_till_done()
    assert runtime.last_transitions["hvac_west_ac"][0] == "zonentra_initiated"
    assert not runtime.overrides
    await fire(hass, dt_util.utcnow() + timedelta(seconds=4))
    assert calls(turn_off) == [(UNIT, None)]
    hass.states.async_set(UNIT, "off", {"hvac_action": "off"})
    await hass.async_block_till_done()
    assert not runtime.overrides
    group = hass.states.get(
        entity(hass, f"{central.entry_id}_group_outdoor_1_coordination")
    )
    assert group.attributes["active_mode_control"] is True
    assert group.attributes["preset_mode"] == "heat"
    assert [c["service"] for c in group.attributes["last_commands"]] == [
        "set_hvac_mode",
        "turn_off",
    ]
    # A restart remembers the applied stand: no new pulse.
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    assert len(set_mode) == 1 and len(turn_off) == 1
    assert central.runtime_data.preset_memories["outdoor_1"].mode == "heat"
    assert zone.state.name == "LOADED"


async def test_wrong_manual_mode_is_corrected_and_automation_is_no_override(
    hass, freezer
):
    set_mode, turn_off = await mock_climate(hass)
    central, _ = await office(hass)
    runtime = central.runtime_data
    set_mode.clear()
    # A Home Assistant user switches the unit on in cool.
    hass.states.async_set(UNIT, "cool", context=Context(user_id="user"))
    await hass.async_block_till_done()
    assert runtime.last_transitions["hvac_west_ac"][0] == "manual_on"
    assert "hvac_west_ac" in runtime.overrides
    assert set_mode == []  # Not immediately: people may still be adjusting.
    await later(hass, freezer, seconds=31)
    assert calls(set_mode) == []
    await later(hass, freezer, seconds=570)
    assert turn_off
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=301)
    assert calls(set_mode) == [(UNIT, "heat")]
    hass.states.async_set(UNIT, "heat")  # The integration applies the correction.
    await hass.async_block_till_done()
    assert "hvac_west_ac" in runtime.overrides  # A correction never ends it.
    hass.states.async_set(UNIT, "off", context=Context(user_id="user"))
    await hass.async_block_till_done()
    # An automation switching a unit on is not a manual override.
    hass.states.async_set(UNIT, "heat", context=Context(parent_id="automation-run"))
    await hass.async_block_till_done()
    assert runtime.last_transitions["hvac_west_ac"][0] == "external_on"
    assert "hvac_west_ac" not in runtime.overrides


async def test_kill_switch_stops_commands_and_group_change_switches_units(
    hass, freezer
):
    set_mode, turn_off = await mock_climate(hass)
    central, _ = await office(hass)
    set_mode.clear()
    # Finish the first preset pulse: heat (ours), off after a few seconds.
    hass.states.async_set(UNIT, "heat")
    await fire(hass, dt_util.utcnow() + timedelta(seconds=4))
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id(hass, central)}, blocking=True
    )
    await hass.async_block_till_done()
    hass.states.async_set(UNIT, "heat", context=Context(user_id="user"))
    await hass.async_block_till_done()
    assert central.runtime_data.last_transitions["hvac_west_ac"][0] == "manual_on"
    freezer.move_to(dt_util.utcnow() + timedelta(hours=1))
    hass.states.async_set("sensor.west_temperature", "25", CELSIUS)  # Cooling demand.
    await hass.async_block_till_done()
    assert set_mode == []
    assert central.runtime_data.mode_controls["outdoor_1"].status == "disabled"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id(hass, central)}, blocking=True
    )
    await hass.async_block_till_done()
    # A running unit is stopped, confirmed off, and waits before restarting.
    assert set_mode == []
    assert turn_off
    hass.states.async_set(UNIT, "off")
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=299)
    assert set_mode == []
    await later(hass, freezer, seconds=2)
    assert calls(set_mode) == [(UNIT, "cool")]
    log = central.runtime_data.command_log[-1]
    assert (log["reason"], log["result"]) == ("switch_running_unit", "sent")


async def test_minimum_switch_interval_and_missing_climate_service(hass, freezer):
    set_mode, _ = await mock_climate(hass)
    central, _ = await office(hass)
    set_mode.clear()
    hass.states.async_set("sensor.west_temperature", "25", CELSIUS)
    await hass.async_block_till_done()
    control = central.runtime_data.mode_controls["outdoor_1"]
    assert control.status == "minimum_switch_interval"
    assert set_mode == []
    freezer.move_to(dt_util.utcnow() + timedelta(minutes=16))
    hass.states.async_set("sensor.west_temperature", "25.1", CELSIUS)
    await hass.async_block_till_done()
    assert calls(set_mode) == [(UNIT, "cool")]


async def test_no_climate_service_means_no_commands(hass):
    assert await async_setup_component(hass, "climate", {})
    hass.services.async_remove("climate", "set_hvac_mode")
    central, _ = await office(hass)
    control = central.runtime_data.mode_controls["outdoor_1"]
    assert control.status == "climate_service_unavailable"
    assert central.runtime_data.preset_memories == {}
