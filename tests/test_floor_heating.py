"""Floor thermostat control, physical confirmation and independent HVAC demand."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.zonentra.const import DOMAIN
from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics
from custom_components.zonentra.floor_heating import heating_progress
from custom_components.zonentra.models import build_model
from custom_components.zonentra.thermostat import Thermostat
from tests.test_entry_layout import (
    CELSIUS,
    add_zone,
    edit_zone_subject,
    set_states,
    setup_central,
    zone_options,
)
from tests.test_thermostat_runtime import later, mock_climate

FLOOR = "climate.floor"
ATTRS = {
    "temperature": 19,
    "hvac_modes": ["heat", "off"],
    "min_temp": 5,
    "max_temp": 30,
    "target_temp_step": 0.5,
    "hvac_action": "idle",
}


KNX_HEAT_ONLY = {
    "hvac_modes": ["heat"],
    "min_temp": 7,
    "max_temp": 35,
    "target_temp_step": 0.1,
    "preset_modes": ["comfort", "standby", "auto", "building_protection", "economy"],
    "current_temperature": 22.6,
    "temperature": 16,
    "preset_mode": "comfort",
    "supported_features": 17,
}


async def setup_floor(hass, *, temperature="21.5", units=False, floor=FLOOR, **fields):
    mocks = await mock_climate(hass)
    set_states(
        hass, "west", *(["climate.west_ac"] if units else []), temperature=temperature
    )
    hass.states.async_set(floor, "off", ATTRS)
    central = await setup_central(hass)
    room = await add_zone(
        hass,
        zone_options(
            "west",
            *(["climate.west_ac"] if units else []),
            floor_heating_entities=[floor],
            **fields,
        ),
    )
    return central.runtime_data, room, mocks


async def report(hass, mode="heat", **attrs):
    hass.states.async_set(FLOOR, mode, {**ATTRS, "temperature": 16, **attrs})
    await hass.async_block_till_done()


def floor_calls(mocks):
    return [
        call
        for values in mocks.values()
        for call in values
        if call.data.get("entity_id") == FLOOR
    ]


def test_floor_settings_inherit_and_preserve_explicit_values():
    building = build_model(
        "test",
        "Test",
        {
            "building": {"floor_heating_limit": 17},
            "zones": [
                {"id": "a", "name": "A"},
                {"id": "b", "name": "B", "floor_heating_limit": 15},
            ],
        },
    )
    assert [
        (z.floor_heating_limit, z.use_building_floor_heating) for z in building.zones
    ] == [(17, True), (15, False)]
    assert Thermostat.from_dict(
        Thermostat(floor_regulation=True).as_dict()
    ).floor_regulation


@pytest.mark.parametrize("value", [0, 26, True, float("nan")])
def test_invalid_floor_settings(value):
    with pytest.raises(ValueError):
        build_model("test", "Test", {"building": {"floor_heating_limit": value}})


def test_warming_signal_requires_reported_progress_after_one_hour():
    now = dt_util.utcnow()
    args = dict(temperature=19, target=22, reported_at=now, watching=True)
    progress = heating_progress(None, now, **args)
    assert progress.status == "monitoring"
    assert progress.next_check == now + timedelta(hours=1)
    later = now + timedelta(hours=1)
    assert heating_progress(progress.memory, later, **args).status == "unknown"
    assert (
        heating_progress(
            progress.memory, later, **{**args, "reported_at": later}
        ).status
        == "insufficient"
    )
    assert (
        heating_progress(
            progress.memory,
            later,
            **{**args, "temperature": 19.4, "reported_at": later},
        ).status
        == "monitoring"
    )
    assert (
        heating_progress(
            progress.memory,
            later,
            **{**args, "temperature": 21.5, "reported_at": later},
        ).status
        == "adequate"
    )
    assert (
        heating_progress(progress.memory, later, **{**args, "watching": False}).memory
        is None
    )


async def test_floor_default_off_sensor_and_switch_visible(hass):
    runtime, room, mocks = await setup_floor(hass)
    assert not runtime.floor_regulated("west")
    assert not floor_calls(mocks)
    registry = er.async_get(hass)
    sensor = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{room.entry_id}_floor_floor_floor"
    )
    switch = registry.async_get_entity_id(
        "switch", DOMAIN, f"{room.entry_id}_floor_regulation"
    )
    assert hass.states.get(sensor).state == "disabled"
    assert hass.states.get(switch).state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert runtime.floor_regulated("west")
    assert floor_calls(mocks)[0].service == "set_temperature"


async def test_setpoint_confirmed_before_heat_and_no_repeated_commands(hass):
    runtime, _, mocks = await setup_floor(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert [(c.service, c.data.get("temperature")) for c in floor_calls(mocks)] == [
        ("set_temperature", 16)
    ]
    assert runtime.zone_demands["west"].demand == "heating_demand"
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    await report(hass, "off")
    assert [c.service for c in floor_calls(mocks)] == [
        "set_hvac_mode",
        "set_temperature",
    ]
    await report(hass)
    runtime.refresh()
    await hass.async_block_till_done()
    assert len(floor_calls(mocks)) == 2
    assert runtime.floor_plans["floor_floor"]["status"] == "in_sync"
    assert not runtime.floor_overrides
    assert all(
        c["confirmation"] == "confirmed"
        for c in runtime.command_log
        if c["equipment_id"] == "floor_floor"
    )


async def test_small_demand_reaches_airco_immediately_without_resetting_hysteresis(
    hass, freezer
):
    runtime, _, _ = await setup_floor(hass)
    await report(hass, "heat")  # Before floor control: no manual override.
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    await later(hass, freezer, minutes=19)
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    # Inside the hysteresis band, the original heating cycle must survive.
    hass.states.async_set(
        "sensor.west_temperature", "22.1", {"unit_of_measurement": "°C"}
    )
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].demand == "heating_demand"
    await later(hass, freezer, minutes=1)
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    await later(hass, freezer, minutes=1)
    assert runtime.hvac_demands["west"].demand == "heating_demand"


async def test_big_deficit_and_disabled_floor_do_not_delay_airco(hass):
    runtime, _, _ = await setup_floor(hass, temperature="20")
    runtime.set_thermostat("west", floor_regulation=True)
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    runtime.set_thermostat("west", floor_regulation=False)
    assert runtime.hvac_demands["west"].demand == "heating_demand"


@pytest.mark.parametrize(
    "attributes,status",
    [
        ({"min_temp": 25}, "target_out_of_range"),
        ({"temperature": None}, "unsupported_setpoint"),
        ({"hvac_modes": ["auto", "off"]}, "unsupported_modes"),
        ({"assumed_state": True}, "unavailable"),
    ],
)
async def test_unusable_floor_falls_back_without_unsafe_commands(
    hass, attributes, status
):
    runtime, _, mocks = await setup_floor(hass)
    hass.states.async_set(FLOOR, "off", {**ATTRS, **attributes})
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert runtime.floor_plans["floor_floor"]["status"] == status
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    assert not floor_calls(mocks)


@pytest.mark.parametrize("setpoint", [16, 19])
async def test_knx_heat_only_floor_controls_target_without_changing_preset(
    hass, setpoint
):
    runtime, _, mocks = await setup_floor(hass)
    hass.states.async_set(FLOOR, "heat", {**KNX_HEAT_ONLY, "temperature": setpoint})
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert runtime.floor_plans["floor_floor"]["status"] == (
        "in_sync" if setpoint == 16 else "setting_target"
    )
    assert [
        (call.service, call.data.get("temperature")) for call in floor_calls(mocks)
    ] == ([] if setpoint == 16 else [("set_temperature", 16)])
    hass.states.async_set(FLOOR, "heat", KNX_HEAT_ONLY)
    await hass.async_block_till_done()
    assert runtime.floor_plans["floor_floor"]["status"] == "in_sync"
    assert hass.states.get(FLOOR).attributes["preset_mode"] == "comfort"


@pytest.mark.parametrize("mode", ["off", "cool"])
async def test_heat_only_floor_cannot_claim_off_or_allow_cooling(hass, mode):
    runtime, _, mocks = await setup_floor(hass, units=True)
    hass.states.async_set(FLOOR, "heat", KNX_HEAT_ONLY)
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=True, hvac_mode=mode)
    if mode == "cool":
        hass.states.async_set("sensor.west_temperature", "25", CELSIUS)
    await hass.async_block_till_done()
    plan = runtime.floor_plans["floor_floor"]
    assert plan["status"] == "cannot_stop"
    assert plan["command"] is None
    assert not floor_calls(mocks)
    if mode == "cool":
        assert plan["cooling_blocked"] is True
        assert runtime.hvac_demands["west"].reason == "floor_stop_unconfirmed"
        assert runtime.hvac_demands["west"].demand == "neutral"


async def test_number_floor_never_receives_commands(hass):
    runtime, _, mocks = await setup_floor(hass, floor="number.floor")
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert runtime.floor_plans["floor_floor"]["status"] == "unsupported_entity"
    assert not floor_calls(mocks)
    assert runtime.hvac_demands["west"].demand == "heating_demand"


async def test_timeout_never_delays_airco_heat_and_keeps_retry_backoff(hass, freezer):
    runtime, _, mocks = await setup_floor(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=61)
    assert runtime.floor_plans["floor_floor"]["status"] == "command_failed"
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    assert len(floor_calls(mocks)) == 1
    await later(hass, freezer, seconds=121)
    assert len(floor_calls(mocks)) == 2


async def test_manual_change_held_but_off_still_stops_floor(hass, freezer):
    runtime, _, mocks = await setup_floor(hass)
    await report(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await report(hass, temperature=20)
    assert runtime.floor_plans["floor_floor"]["status"] == "manual_override"
    assert not floor_calls(mocks)
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    runtime.set_thermostat("west", hvac_mode="off")
    await hass.async_block_till_done()
    assert floor_calls(mocks)[0].service == "turn_off"


async def test_recovery_not_manual_and_override_expires(hass, freezer):
    runtime, _, _ = await setup_floor(hass)
    await report(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await report(hass, "unavailable")
    await report(hass)
    assert not runtime.floor_overrides
    await report(hass, temperature=20)
    assert runtime.floor_overrides
    await later(hass, freezer, minutes=31)
    assert not runtime.floor_overrides
    assert runtime.floor_plans["floor_floor"]["status"] == "setting_target"


async def test_cooling_waits_for_floor_off_confirmation_and_final_guard(hass):
    runtime, _, mocks = await setup_floor(hass, temperature="25", units=True)
    await report(hass)
    runtime.set_thermostat("west", floor_regulation=True, regulation=True)
    await hass.async_block_till_done()
    assert runtime.hvac_demands["west"].reason == "floor_stop_unconfirmed"
    assert floor_calls(mocks)[0].service == "turn_off"
    item = next(i for i in runtime.building.equipment if i.kind == "hvac")
    assert not await runtime._call(
        "set_hvac_mode",
        {"entity_id": item.entity_id, "hvac_mode": "cool"},
        {"equipment_id": item.id, "reason": "room_demand"},
    )
    await report(hass, "off")
    assert runtime.hvac_demands["west"].demand == "cooling_demand"
    assert not runtime._floor_cooling_block("west")


async def test_window_stops_floor_independent_of_group_switch(hass, freezer):
    runtime, _, mocks = await setup_floor(
        hass, contact_entities=["binary_sensor.window"]
    )
    await report(hass)
    runtime.mode_active["outdoor_1"] = False
    runtime.set_thermostat("west", floor_regulation=True)
    hass.states.async_set("binary_sensor.window", "on")
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=31)
    assert runtime.windows["west"] == "open"
    assert floor_calls(mocks)[0].service == "turn_off"


@pytest.mark.parametrize("preset", ["comfort", "eco", "away"])
async def test_floor_keeps_fixed_baseline_for_every_preset(hass, preset):
    runtime, _, mocks = await setup_floor(hass)
    runtime.set_thermostat("west", floor_regulation=True, preset=preset)
    await hass.async_block_till_done()
    assert floor_calls(mocks)[0].data["temperature"] == 16
    assert not runtime.regulated("west")


async def test_floor_status_activity_and_diagnostics(hass):
    runtime, room, _ = await setup_floor(hass)
    await report(hass, hvac_action="heating")
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    climate = registry.async_get_entity_id(
        "climate", DOMAIN, f"{room.entry_id}_thermostat"
    )
    assert hass.states.get(climate).attributes["hvac_action"] == "heating"
    central = hass.config_entries.async_get_entry(runtime.building.id)
    diagnostic = await async_get_config_entry_diagnostics(hass, central)
    assert FLOOR not in str(diagnostic)
    assert diagnostic["floor_heating"][0]["equipment_id"] == "**REDACTED**"


async def test_floor_target_converted_to_fahrenheit(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    runtime, _, mocks = await setup_floor(hass)
    hass.states.async_set(
        FLOOR,
        "off",
        {
            **ATTRS,
            "temperature": 65,
            "min_temp": 41,
            "max_temp": 95,
            "target_temp_step": 1,
        },
    )
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert floor_calls(mocks)[0].data["temperature"] == 61


async def test_final_guard_rechecks_disable_and_target_before_sending(hass):
    runtime, _, _ = await setup_floor(hass)
    item = next(i for i in runtime.building.equipment if i.kind == "floor_heating")
    entry = {"equipment_id": item.id, "reason": "floor_heating"}
    assert not await runtime._call(
        "set_temperature", {"entity_id": FLOOR, "temperature": 22}, entry
    )
    assert entry["result"] == "blocked: floor_disabled"
    runtime.thermostats["west"] = replace(
        runtime.thermostat("west"), floor_regulation=True
    )
    assert not await runtime._call(
        "set_hvac_mode", {"entity_id": FLOOR, "hvac_mode": "heat"}, entry
    )
    assert entry["result"] == "blocked: floor_target_unconfirmed"


@pytest.mark.parametrize(
    "native_unit,step,expected", [("°C", 0.5, 16), ("°F", 1, 61), ("°F", 5, 60)]
)
@pytest.mark.parametrize("system_fahrenheit", [False, True])
async def test_real_floor_service_quantization_and_confirmation(
    hass, native_unit, step, expected, system_fahrenheit
):
    from homeassistant.helpers.entity_component import DATA_INSTANCES
    from homeassistant.setup import async_setup_component

    from tests.test_temperature_runtime import ReportingClimate

    if system_fahrenheit:
        hass.config.units = US_CUSTOMARY_SYSTEM
    assert await async_setup_component(hass, "climate", {})
    source = ReportingClimate(
        native_unit,
        step,
        5 if native_unit == "°C" else 41,
        35 if native_unit == "°C" else 95,
    )
    await hass.data[DATA_INSTANCES]["climate"].async_add_entities([source])
    await hass.async_block_till_done()
    set_states(hass, "west", temperature="21.5")
    central = await setup_central(hass)
    await add_zone(
        hass, zone_options("west", floor_heating_entities=[source.entity_id])
    )
    runtime = central.runtime_data
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert source.received == [pytest.approx(expected)]
    assert runtime.floor_plans["floor_source_ac"]["status"] == "in_sync"
    assert not runtime.floor_overrides
    runtime.refresh()
    await hass.async_block_till_done()
    assert source.received == [pytest.approx(expected)]
    assert not runtime.command_receipts


async def test_local_floor_options_round_trip(hass):
    from homeassistant.data_entry_flow import FlowResultType

    runtime, room, _ = await setup_floor(hass)
    flow = await hass.config_entries.options.async_init(room.entry_id)
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_floor_heating",
        {
            "floor_heating_entities": [FLOOR],
            "use_building_floor_heating": False,
            "floor_heating_limit": 15,
        },
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    central = hass.config_entries.async_get_entry(runtime.building.id)
    zone = central.runtime_data.building.zones[0]
    assert zone.floor_heating_limit == 15


async def test_unconfirmed_cooling_stop_remains_blocked_after_timeout(hass, freezer):
    runtime, _, _ = await setup_floor(hass, temperature="25")
    await report(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=61)
    assert runtime.hvac_demands["west"].reason == "floor_stop_unconfirmed"
    assert runtime.command_retries


async def test_floor_without_reported_action_does_not_claim_active_heating(hass):
    runtime, room, _ = await setup_floor(hass)
    await report(hass, hvac_action=None)
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    climate = er.async_get(hass).async_get_entity_id(
        "climate", DOMAIN, f"{room.entry_id}_thermostat"
    )
    assert hass.states.get(climate).attributes["hvac_action"] == "idle"


async def test_floor_override_survives_reload_and_removed_floor_is_pruned(
    hass, freezer
):
    runtime, room, mocks = await setup_floor(hass)
    await report(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await report(hass, temperature=18)
    until = runtime.floor_overrides["floor_floor"]
    central = hass.config_entries.async_get_entry(runtime.building.id)
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert runtime.floor_overrides["floor_floor"] == until
    hass.config_entries.async_update_entry(
        room, options={**dict(room.options), "floor_heating_entities": []}
    )
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert not runtime.floor_plans
    assert not runtime.floor_overrides
    assert not runtime.heating_watches
    assert not floor_calls(mocks)


async def test_service_failure_releases_airco_heating(hass):
    from homeassistant.exceptions import HomeAssistantError
    from pytest_homeassistant_custom_component.common import async_mock_service

    runtime, _, _ = await setup_floor(hass)
    failed = async_mock_service(
        hass,
        "climate",
        "set_temperature",
        raise_exception=HomeAssistantError("offline"),
    )
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert len(failed) == 1
    assert runtime.floor_plans["floor_floor"]["status"] == "command_failed"
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    assert not runtime.command_receipts


async def test_floor_calendar_preparation_keeps_baseline_while_airco_asks_for_comfort(
    hass,
):
    from tests.test_bookings_runtime import office

    mocks = await mock_climate(hass)
    hass.states.async_set(FLOOR, "off", ATTRS)
    hass.states.async_set("input_boolean.building_present", "off")
    central, _, _ = await office(
        hass,
        start=10,
        presence="input_boolean.building_present",
        floor_heating_entities=[FLOOR],
    )
    runtime = central.runtime_data
    assert runtime.booking_plans["west"].active
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert floor_calls(mocks)[0].data["temperature"] == 16


async def test_floor_switch_off_leaves_device_and_cancels_queued_authorization(hass):
    runtime, _, mocks = await setup_floor(hass)
    await report(hass)
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=False)
    await hass.async_block_till_done()
    assert not floor_calls(mocks)
    assert hass.states.get(FLOOR).state == "heat"
    item = next(i for i in runtime.building.equipment if i.kind == "floor_heating")
    entry = {"equipment_id": item.id, "reason": "floor_heating"}
    assert not await runtime._call("turn_off", {"entity_id": FLOOR}, entry)
    assert entry["result"] == "blocked: floor_disabled"


@pytest.mark.parametrize("mode", ["cool", "dry"])
async def test_floor_stays_off_when_airco_cooling_without_thermal_demand(hass, mode):
    runtime, _, mocks = await setup_floor(hass, temperature="23", units=True)
    await report(hass)
    hass.states.async_set(
        "climate.west_ac", mode, {"temperature": 23, "hvac_action": "idle"}
    )
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].demand == "neutral"
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert [c.service for c in floor_calls(mocks)] == ["turn_off"]
    await report(hass, "off")
    runtime.refresh()
    await hass.async_block_till_done()
    assert len(floor_calls(mocks)) == 1
    assert runtime.floor_plans["floor_floor"]["status"] == "off"
    item = next(i for i in runtime.building.equipment if i.kind == "floor_heating")
    entry = {"equipment_id": item.id, "reason": "floor_heating"}
    assert not await runtime._call(
        "set_hvac_mode", {"entity_id": FLOOR, "hvac_mode": "heat"}, entry
    )
    assert entry["result"] == "blocked: floor_heat_blocked"


async def test_cooling_does_not_release_on_contradictory_floor_activity(hass):
    runtime, _, _ = await setup_floor(hass, temperature="25")
    await report(hass, "off", hvac_action="heating")
    runtime.set_thermostat("west", floor_regulation=True)
    assert runtime.hvac_demands["west"].reason == "floor_stop_unconfirmed"
    assert runtime.floor_plans["floor_floor"]["status"] == "stopping"
    await report(hass, "off", hvac_action="idle")
    assert runtime.hvac_demands["west"].demand == "cooling_demand"


async def test_reported_insufficient_warming_signals_without_raising_floor(
    hass, freezer
):
    runtime, room, mocks = await setup_floor(hass, temperature="19", units=True)
    await report(hass)
    hass.states.async_set(
        "climate.west_ac", "heat", {"temperature": 24, "hvac_action": "heating"}
    )
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=True, regulation=True)
    await hass.async_block_till_done()
    assert runtime.heating_progress["west"].status == "monitoring"
    await later(hass, freezer, minutes=60)
    assert runtime.heating_progress["west"].status == "unknown"
    hass.states.async_set(
        "sensor.west_temperature", "19.1", {"unit_of_measurement": "°C"}
    )
    await hass.async_block_till_done()
    assert runtime.heating_progress["west"].status == "insufficient"
    sensor = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{room.entry_id}_heating_progress"
    )
    assert hass.states.get(sensor).state == "insufficient"
    assert hass.states.get(sensor).attributes["automatic_gas_boost"] is False
    assert runtime.floor_plans["floor_floor"]["target_temperature"] == 16
    assert not floor_calls(mocks)
    assert runtime.hvac_demands["west"].demand == "heating_demand"
    hass.states.async_set(
        "climate.west_ac", "heat", {"temperature": 24, "hvac_action": "idle"}
    )
    await hass.async_block_till_done()
    assert runtime.heating_progress["west"].status == "idle"
    assert not runtime.heating_watches


async def test_selected_heat_without_reported_activity_cannot_trigger_warning(
    hass, freezer
):
    runtime, _, _ = await setup_floor(hass, temperature="19", units=True)
    await report(hass)
    hass.states.async_set("climate.west_ac", "heat", {"temperature": 24})
    await hass.async_block_till_done()
    runtime.set_thermostat("west", floor_regulation=True, regulation=True)
    await later(hass, freezer, minutes=61)
    hass.states.async_set(
        "sensor.west_temperature", "19.1", {"unit_of_measurement": "°C"}
    )
    await hass.async_block_till_done()
    assert runtime.heating_progress["west"].status == "idle"
    assert not runtime.heating_watches


def test_new_heating_target_starts_fresh_monitoring_period():
    now = dt_util.utcnow()
    progress = heating_progress(
        None, now, temperature=19, target=21, reported_at=now, watching=True
    )
    later = now + timedelta(hours=1)
    changed = heating_progress(
        progress.memory,
        later,
        temperature=19.1,
        target=22,
        reported_at=later,
        watching=True,
    )
    assert changed.status == "monitoring"
    assert changed.memory.since == later


@pytest.mark.parametrize("mode", ["cool", "dry"])
async def test_manual_floor_keeps_cooling_interlock_without_floor_commands(hass, mode):
    runtime, _, mocks = await setup_floor(hass, temperature="25", units=True)
    await report(hass, "heat", hvac_action="heating")
    assert not runtime.floor_regulated("west")
    runtime.set_thermostat("west", regulation=True, hvac_mode=mode)
    await hass.async_block_till_done()
    assert runtime._floor_cooling_block("west")
    assert runtime.floor_plans["floor_floor"]["cooling_blocked"] is True
    assert not floor_calls(mocks)
    item = next(i for i in runtime.building.equipment if i.kind == "hvac")
    entry = {"equipment_id": item.id, "reason": "room_demand"}
    assert not await runtime._call(
        "set_hvac_mode", {"entity_id": item.entity_id, "hvac_mode": mode}, entry
    )
    assert entry["result"] == "blocked: floor_stop_unconfirmed"
    await report(hass, "off", hvac_action="idle")
    assert not runtime._floor_cooling_block("west")
    assert not floor_calls(mocks)
    if mode == "cool":
        assert runtime.hvac_demands["west"].demand == "cooling_demand"


async def test_disabling_floor_regulation_cannot_release_unconfirmed_stop(hass):
    runtime, _, mocks = await setup_floor(hass, temperature="25", units=True)
    await report(hass, "heat", hvac_action="heating")
    runtime.set_thermostat("west", floor_regulation=True)
    await hass.async_block_till_done()
    assert runtime._floor_cooling_block("west")
    for values in mocks.values():
        values.clear()
    runtime.set_thermostat("west", floor_regulation=False)
    await hass.async_block_till_done()
    assert runtime._floor_cooling_block("west")
    assert runtime.hvac_demands["west"].reason == "floor_stop_unconfirmed"
    assert not floor_calls(mocks)
    await report(hass, "unavailable")
    assert runtime._floor_cooling_block("west")
    await report(hass, "off", hvac_action="heating")
    assert runtime._floor_cooling_block("west")
    await report(hass, "off", hvac_action="idle")
    assert not runtime._floor_cooling_block("west")
