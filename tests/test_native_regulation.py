"""One-room trial: native modulation, external room limits and existing guards."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.zonentra.control import standalone_coordination, unit_intent
from custom_components.zonentra.models import build_model
from custom_components.zonentra.sensor import ZoneDemandSensor
from custom_components.zonentra.thermostat import native_hold_allowed, plan_setpoint
from tests.test_entry_layout import (
    CELSIUS,
    add_zone,
    edit_zone_subject,
    setup_central,
    zone_options,
)
from tests.test_thermostat_runtime import clear, later, mock_climate


async def trial(hass, *, mode="heat", temperature="22.5", **fields):
    mocks = await mock_climate(hass)
    for name in ("west", "east"):
        hass.states.async_set(f"sensor.{name}_temperature", temperature, CELSIUS)
        hass.states.async_set(
            f"climate.{name}_ac",
            mode,
            {
                "temperature": 22.5,
                "target_temp_step": 0.5,
                "hvac_modes": ["heat", "cool", "off"],
                "hvac_action": "idle",
            },
            timestamp=(dt_util.utcnow() - timedelta(hours=1)).timestamp(),
        )
    central = await setup_central(hass)
    west = await add_zone(
        hass,
        zone_options(
            "west", "climate.west_ac", group_id=None, native_regulation=True, **fields
        ),
    )
    await add_zone(hass, zone_options("east", "climate.east_ac", group_id=None))
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    return central, west, mocks


@pytest.mark.parametrize("mode,temperature", [("heat", "22.5"), ("cool", "23.5")])
async def test_native_room_keeps_running_while_other_room_uses_existing_control(
    hass, mode, temperature
):
    central, west, mocks = await trial(hass, mode=mode, temperature=temperature)
    runtime = central.runtime_data
    assert runtime.unit_intents["hvac_west_ac"].reason == "native_regulation"
    assert runtime.room_controls["hvac_west_ac"].status == "hold"
    runtime.set_thermostat("east", regulation=True)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in mocks["turn_off"]] == ["climate.east_ac"]
    sensor = ZoneDemandSensor(central, runtime.building.zones[0])
    assert sensor.extra_state_attributes["airco_control_strategy"] == "native_trial"
    assert sensor.extra_state_attributes["native_regulation_active"] is True


@pytest.mark.parametrize("preset", ["eco", "away"])
async def test_saving_bands_still_stop_native_room(hass, preset):
    central, _, mocks = await trial(hass)
    clear(mocks)
    central.runtime_data.set_thermostat("west", preset=preset)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in mocks["turn_off"]] == ["climate.west_ac"]


@pytest.mark.parametrize(
    "mode,initial,excess", [("heat", "22.5", "23.1"), ("cool", "23.5", "22.9")]
)
async def test_external_room_sensor_ends_hold_beyond_tolerance(
    hass, mode, initial, excess
):
    central, _, mocks = await trial(hass, mode=mode, temperature=initial)
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", excess, CELSIUS)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in mocks["turn_off"]] == ["climate.west_ac"]
    assert (
        central.runtime_data.unit_intents["hvac_west_ac"].reason != "native_regulation"
    )


async def test_explicit_off_and_room_heat_only_are_not_overridden(hass):
    central, _, mocks = await trial(hass, mode="cool", temperature="23.5")
    clear(mocks)
    central.runtime_data.set_thermostat("west", hvac_mode="heat")
    await hass.async_block_till_done()
    assert mocks["turn_off"]
    clear(mocks)
    central.runtime_data.set_thermostat("west", hvac_mode="off")
    await hass.async_block_till_done()
    assert not mocks["set_hvac_mode"]
    assert "hvac_west_ac" not in central.runtime_data._native_holds()


async def test_open_window_still_stops_native_room(hass, freezer):
    hass.states.async_set("binary_sensor.west_window", "off")
    _, _, mocks = await trial(hass, contact_entities=["binary_sensor.west_window"])
    clear(mocks)
    hass.states.async_set("binary_sensor.west_window", "on")
    await later(hass, freezer, seconds=31)
    assert [call.data["entity_id"] for call in mocks["turn_off"]] == ["climate.west_ac"]


async def test_off_unit_does_not_start_just_to_hold_temperature(hass):
    central, _, mocks = await trial(hass)
    hass.states.async_set("climate.west_ac", "off", {"temperature": 22.5})
    await hass.async_block_till_done()
    clear(mocks)
    central.runtime_data.refresh()
    await hass.async_block_till_done()
    assert not mocks["set_hvac_mode"]
    assert central.runtime_data.unit_intents["hvac_west_ac"].desired_mode == "off"


async def test_trial_can_be_enabled_for_one_room_and_reverted(hass):
    central, west, _ = await trial(hass)
    flow = await hass.config_entries.options.async_init(west.entry_id)
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_regulation",
        {"use_building_parameters": True, "native_regulation": False},
    )
    assert flow["type"] == FlowResultType.MENU
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert not next(
        zone for zone in central.runtime_data.building.zones if zone.id == "west"
    ).native_regulation
    assert not next(
        zone for zone in central.runtime_data.building.zones if zone.id == "east"
    ).native_regulation


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_trial_flag_requires_boolean(value):
    with pytest.raises(ValueError, match="invalid_parameters"):
        build_model(
            "test",
            "Test",
            {"zones": [{"id": "west", "name": "West", "native_regulation": value}]},
        )


def test_native_correction_is_smaller_bounded_and_respects_interval():
    now = dt_util.utcnow()
    args = ("heat", 22, 24, 0.3, 20, 22.5, None, now)
    old = plan_setpoint(*args, max_offset=2, interval_seconds=300)
    native = plan_setpoint(*args, max_offset=1, interval_seconds=300, gain=0.25)
    assert abs(native.offset) < abs(old.offset)
    assert native.setpoint < old.setpoint
    pending = plan_setpoint(
        "heat",
        22,
        24,
        0.3,
        20,
        22.5,
        now,
        now,
        max_offset=1,
        interval_seconds=300,
        gain=0.25,
    )
    assert pending.status == "interval"
    assert not pending.send
    assert not native_hold_allowed("heat", None, 22, 24, 0.3)


@pytest.mark.parametrize(
    "reason",
    [
        "floor_stop_unconfirmed",
        "window_open",
        "thermostat_off",
        "heating_deferred_warm_day",
    ],
)
async def test_native_hold_cannot_override_a_policy_stop(hass, reason):
    central, _, _ = await trial(hass)
    runtime = central.runtime_data
    demand = replace(runtime.zone_demands["west"], reason=reason)
    coordination = standalone_coordination("hvac_west_ac", demand)
    intent = unit_intent(
        runtime.indoor_units["hvac_west_ac"],
        dt_util.utcnow() - timedelta(hours=1),
        demand,
        coordination,
        None,
        (),
        runtime.building.settings,
        dt_util.utcnow(),
        native_hold=True,
    )
    assert intent.action == "turn_off"


@pytest.mark.parametrize(
    "changes",
    [
        {"group_mode": "cool"},
        {"conflicts": ("selected_heat_and_cool",)},
        {"phase": "blocked"},
        {
            "group_mode": "cool",
            "switch_needed": True,
            "switch_to": "cool",
            "phase": "stopping",
        },
    ],
)
async def test_native_hold_cannot_bypass_group_coordination(hass, changes):
    central, _, _ = await trial(hass)
    runtime = central.runtime_data
    demand = runtime.zone_demands["west"]
    coordination = replace(standalone_coordination("hvac_west_ac", demand), **changes)
    intent = unit_intent(
        runtime.indoor_units["hvac_west_ac"],
        dt_util.utcnow() - timedelta(hours=1),
        demand,
        coordination,
        None,
        (),
        runtime.building.settings,
        dt_util.utcnow(),
        native_hold=True,
    )
    assert intent.reason != "native_regulation"
    assert intent.action == "turn_off"


async def test_manual_temperature_change_still_pauses_control(hass):
    from homeassistant.core import Context

    central, _, mocks = await trial(hass)
    clear(mocks)
    hass.states.async_set(
        "climate.west_ac",
        "heat",
        {"temperature": 25, "hvac_action": "idle"},
        context=Context(user_id="manual-user"),
    )
    await hass.async_block_till_done()
    assert central.runtime_data.overrides["hvac_west_ac"].active(dt_util.utcnow())
    assert (
        central.runtime_data.setpoint_plans["hvac_west_ac"].status == "manual_override"
    )
    assert not mocks["set_temperature"]
    assert not mocks["turn_off"]


async def test_missing_room_temperature_cannot_keep_native_hold(hass):
    central, _, mocks = await trial(hass)
    clear(mocks)
    hass.states.async_set("sensor.west_temperature", "unavailable", CELSIUS)
    await hass.async_block_till_done()
    intent = central.runtime_data.unit_intents["hvac_west_ac"]
    assert intent.action == "blocked"
    assert intent.reason in ("zone_demand_unknown", "group_mode_unknown")
    assert not mocks["set_temperature"]


async def test_enabling_trial_in_options_changes_only_selected_room(hass):
    central, west, _ = await trial(hass)
    for enabled in (False, True):
        flow = await hass.config_entries.options.async_init(west.entry_id)
        flow = await edit_zone_subject(
            hass,
            flow,
            "zone_regulation",
            {"native_regulation": enabled, "use_building_parameters": True},
        )
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
        zones = {zone.id: zone for zone in central.runtime_data.building.zones}
        assert zones["west"].native_regulation is enabled
        assert not zones["east"].native_regulation


async def test_trial_uses_smaller_setpoint_correction_in_runtime(hass):
    central, _, _ = await trial(hass, temperature="20")
    plans = central.runtime_data.setpoint_plans
    assert plans["hvac_west_ac"].setpoint == 23
    assert plans["hvac_east_ac"].setpoint == 24.5
