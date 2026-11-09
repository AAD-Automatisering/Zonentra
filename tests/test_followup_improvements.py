"""Practical follow-up: physical feedback, fan ordering and persistent screen policy."""

from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.zonentra.climate import ZonentraThermostat
from custom_components.zonentra.const import DOMAIN
from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics
from custom_components.zonentra.models import Equipment
from custom_components.zonentra.roomstate import fan_names, fan_speeds
from tests.test_shading_runtime import EQUIPMENT, SCREEN, office
from tests.test_thermostat_runtime import later


@pytest.mark.parametrize(
    "modes,expected",
    [
        (("off", "100%", "75%", "25%", "50%", "auto"), ("25%", "50%", "75%", "100%")),
        (("high", "low", "medium"), ("low", "medium", "high")),
        (("3", "1", "2"), ("1", "2", "3")),
        (("Strong", "Breeze", "Normal"), ("Strong", "Breeze", "Normal")),
        (("Auto", "OFF", "High", "Low"), ("Low", "High")),
    ],
)
def test_normalized_fan_order(modes, expected):
    assert fan_speeds(modes) == expected
    if len(expected) == 4:
        assert fan_names(modes)["low"] == "25%"


@pytest.mark.parametrize(
    "selected,reported,expected",
    [
        ("heat", "idle", "idle"),
        ("cool", "idle", "idle"),
        ("heat", "fan", "fan"),
        ("heat", "defrosting", "heating"),
        ("heat_cool", "cooling", "cooling"),
        ("heat", None, "heating"),
    ],
)
def test_thermostat_prefers_reported_action(selected, reported, expected):
    # Construct the same observation the runtime supplies to the entity.
    item = Equipment("unit", "Unit", "hvac", "climate.unit", "room")
    observation = SimpleNamespace(
        equipment_id="unit",
        available=True,
        selected_mode=selected,
        reported_action=reported,
        activity=None if reported is None else "reported",
    )
    room = SimpleNamespace(
        _zone_id="room",
        _floor_demand=lambda: (False, None),
        thermostat=SimpleNamespace(hvac_mode="heat_cool"),
        runtime=SimpleNamespace(
            building=SimpleNamespace(equipment=(item,)),
            indoor_units={"unit": observation},
            floor_observations={},
        ),
    )
    assert ZonentraThermostat.hvac_action.fget(room) == expected


async def restart_central(hass, central):
    assert await hass.config_entries.async_unload(central.entry_id)
    hass.data[DOMAIN]["runtimes"].clear()
    hass.data[DOMAIN]["screen_stores"].clear()
    assert await hass.config_entries.async_setup(central.entry_id)
    await hass.async_block_till_done()
    return central.runtime_data


async def test_screen_manual_pause_and_last_move_survive_fresh_runtime(hass, freezer):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, zone = await office(hass, screen_shade_delay_minutes=0)
    runtime = central.runtime_data
    runtime.set_thermostat("west", screen_regulation=True)
    await later(hass, freezer, minutes=5)
    assert len(moves) == 1
    last_move = runtime.screen_last_move[EQUIPMENT]
    hass.states.async_set(SCREEN, "closed", {"current_position": 0})
    await hass.async_block_till_done()
    hass.states.async_set(
        SCREEN, "open", {"current_position": 100}, context=Context(user_id="person")
    )
    await hass.async_block_till_done()
    until = runtime.screen_overrides[EQUIPMENT]
    await later(hass, freezer, seconds=2)
    runtime = await restart_central(hass, central)
    assert runtime.screen_last_move[EQUIPMENT] == last_move
    assert runtime.screen_overrides[EQUIPMENT] == until
    await later(hass, freezer, minutes=5)
    assert len(moves) == 1
    assert runtime.screen_moves[EQUIPMENT].status == "manual_override"
    data = await async_get_config_entry_diagnostics(hass, zone)
    assert data["screens"][0]["manual_until"] == until.isoformat()


async def test_restart_preserves_interval_without_manual_override(hass, freezer):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await office(
        hass, screen_open_delay_minutes=0, screen_shade_delay_minutes=0
    )
    central.runtime_data.set_thermostat("west", screen_regulation=True)
    await later(hass, freezer, minutes=5)
    hass.states.async_set(SCREEN, "closed", {"current_position": 0})
    await hass.async_block_till_done()
    await later(hass, freezer, seconds=2)
    runtime = await restart_central(hass, central)
    hass.states.async_set("sun.sun", "below_horizon", {"azimuth": 300, "elevation": -5})
    await hass.async_block_till_done()
    assert len(moves) == 1
    assert runtime.screen_moves[EQUIPMENT].status == "interval"
    await later(hass, freezer, minutes=29, seconds=58)
    assert [call.data["position"] for call in moves] == [0, 100]


async def test_screen_diagnostics_include_reason_timer_and_commands(hass, freezer):
    async_mock_service(hass, "cover", "set_cover_position")
    central, zone = await office(hass)
    runtime = central.runtime_data
    runtime.set_thermostat("west", screen_regulation=True)
    await later(hass, freezer, minutes=5)
    for entry in (central, zone):
        data = await async_get_config_entry_diagnostics(hass, entry)
        screen = data["screens"][0]
        assert screen["decision"]["target"] == "shade"
        assert screen["movement"]["status"] == "stabilizing"
        assert screen["movement"]["next_check"] is not None
        assert screen["target_since"] is not None
        assert screen["equipment_id"] == "**REDACTED**"
        assert data["screen_timing"]["minimum_move_minutes"] == 30


def test_profile_quality_rejects_age_spread_and_missing_evidence():
    from custom_components.zonentra.bookings import (
        ThermalRates,
        estimated_rate,
        rate_quality,
    )

    now = dt_util.utcnow()
    for fields, status in [
        ({}, "age_unknown"),
        ({"heating_updated": (now - timedelta(days=31)).isoformat()}, "expired"),
        ({"heating_updated": now.isoformat(), "heating_variance": 0.01}, "variable"),
        (
            {"heating_updated": now.isoformat(), "heating_samples": 2},
            "insufficient_samples",
        ),
    ]:
        rates = ThermalRates(heating=0.1, **{"heating_samples": 8, **fields})
        assert rate_quality("heating", rates, now)["status"] == status
        assert estimated_rate("heating", rates, 0.05, 15, now) == (0.05, False)


def test_new_learning_resets_legacy_profile_and_records_time_and_spread():
    from custom_components.zonentra.bookings import ThermalRates, learn_rate

    now = dt_util.utcnow()
    legacy = ThermalRates(heating=0.3, heating_samples=100)
    rates, sample = learn_rate(legacy, None, now, 18, "heating")
    rates, sample = learn_rate(
        rates, sample, now + timedelta(minutes=10), 19, "heating"
    )
    assert rates.heating == 0.1
    assert rates.heating_samples == 1
    assert rates.heating_updated == (now + timedelta(minutes=10)).isoformat()
    rates, _ = learn_rate(rates, sample, now + timedelta(minutes=20), 19.5, "heating")
    assert rates.heating_variance > 0
    assert ThermalRates.restore(asdict(rates)) == rates


def test_meeting_result_adjusts_margin_with_bounds_and_decays_on_success():
    from custom_components.zonentra.bookings import (
        ThermalRates,
        assess_booking,
        learn_booking_margin,
    )

    assert assess_booking(20, 22, 24, True) == ("too_cold", 2)
    assert assess_booking(23, 22, 24, True) == ("ready", 0)
    assert assess_booking(None, 22, 24, True) == ("unknown", None)
    assert assess_booking(20, 22, 24, False) == ("not_evaluated", None)
    rates = ThermalRates()
    for _ in range(10):
        rates = learn_booking_margin(rates, "too_cold", "heating")
    assert rates.heating_buffer == 30
    assert rates.cooling_buffer == 0
    assert learn_booking_margin(rates, "ready", "heating").heating_buffer == 29
    assert learn_booking_margin(rates, "not_evaluated", "heating") == rates


def test_booking_urgency_is_bounded_and_group_guards_still_win():
    from custom_components.zonentra.bookings import BookingPlan, booking_priority
    from custom_components.zonentra.demand import ZoneDemand
    from custom_components.zonentra.engine import GroupMemory, evaluate
    from custom_components.zonentra.models import build_model
    from custom_components.zonentra.observations import SourceState

    now = dt_util.utcnow()
    plan = BookingPlan("preparing", start=now + timedelta(minutes=60), lead_minutes=60)
    assert booking_priority(plan, now) == 1
    assert booking_priority(plan, now + timedelta(minutes=30)) == 1.5
    assert booking_priority(replace(plan, status="meeting"), now) == 2
    assert booking_priority(BookingPlan("unknown"), now) == 1
    building = build_model(
        "test",
        "Test",
        {
            "zones": [{"id": key, "name": key} for key in ("west", "east")],
            "groups": [{"id": "g", "name": "Group", "mode_entity": "sensor.mode"}],
            "equipment": [
                {
                    "id": key,
                    "name": key,
                    "kind": "hvac",
                    "zone_id": key,
                    "entity_id": f"climate.{key}",
                    "group_id": "g",
                }
                for key in ("west", "east")
            ],
        },
    )
    sources = {
        key: SourceState("off", None, now)
        for key in ("sensor.mode", "climate.west", "climate.east")
    }
    demands = {
        "west": ZoneDemand("west", "heating_demand", 1.1, 0, "cold", "valid"),
        "east": ZoneDemand("east", "cooling_demand", 0, 1.6, "warm", "valid"),
    }
    memories = {"g": GroupMemory("off", now - timedelta(hours=1))}
    assert evaluate(building, demands, sources, memories, now)[0].desired_mode == "cool"
    demands["west"] = replace(demands["west"], priority=2)
    assert evaluate(building, demands, sources, memories, now)[0].desired_mode == "heat"
    memories["g"] = GroupMemory("off", now)
    assert (
        evaluate(building, demands, sources, memories, now)[0].blocked_reason
        == "minimum_switch_interval"
    )
    sources["climate.east"] = SourceState("unavailable", None, now)
    assert (
        evaluate(building, demands, sources, memories, now)[0].proposed_mode
        == "unknown"
    )


@pytest.mark.parametrize(
    "temperature,status,buffer,interrupted",
    [
        ("20.5", "too_cold", 5, False),
        ("22.5", "ready", 4, False),
        ("20.5", "not_evaluated", 0, True),
    ],
)
async def test_meeting_start_result_is_measured_once_and_saved(
    hass, freezer, temperature, status, buffer, interrupted
):
    from custom_components.zonentra.bookings import ThermalRates
    from tests.test_bookings_runtime import CALENDAR, booking, calendar_service, later
    from tests.test_bookings_runtime import office as booking_office
    from tests.test_entry_layout import CELSIUS
    from tests.test_thermostat_runtime import mock_climate

    await mock_climate(hass)
    central, zone, _ = await booking_office(hass, start=120, units=True)
    unit = "climate.west_ac"

    async def feedback(call):
        state = hass.states.get(unit)
        mode = call.data.get("hvac_mode", state.state)
        if call.service == "turn_off":
            mode = "off"
        attributes = {
            **state.attributes,
            "hvac_action": {"heat": "heating", "cool": "cooling"}.get(mode, "off"),
        }
        if "temperature" in call.data:
            attributes["temperature"] = call.data["temperature"]
        hass.states.async_set(unit, mode, attributes, context=call.context)

    for service in ("set_hvac_mode", "turn_off", "set_temperature"):
        hass.services.async_register("climate", service, feedback)
    runtime = central.runtime_data
    if status == "ready":
        runtime.thermal_rates["west"] = {"mild": ThermalRates(heating_buffer=5)}
    runtime.set_thermostat("west", regulation=True)
    calendar_service(hass, {CALENDAR: {"events": [booking(20)]}})
    await runtime.async_update_bookings()
    assert runtime.booking_plans["west"].status == "preparing"
    await later(hass, freezer, minutes=4)
    if interrupted:
        runtime.set_thermostat("west", regulation=False)
        await hass.async_block_till_done()
    await later(hass, freezer, minutes=4)
    runtime.set_thermostat("west", regulation=True)
    for _ in range(2):
        await later(hass, freezer, minutes=4)
    hass.states.async_set("sensor.west_temperature", temperature, CELSIUS)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=4)
    assert runtime.booking_results["west"]["status"] == status
    assert runtime.thermal_rates["west"]["mild"].heating_buffer == buffer
    result = dict(runtime.booking_results["west"])
    runtime.refresh()
    await hass.async_block_till_done()
    assert runtime.booking_results["west"] == result
    assert runtime.thermal_rates["west"]["mild"].heating_buffer == buffer
    data = await async_get_config_entry_diagnostics(hass, zone)
    assert data["bookings"][0]["last_result"]["status"] == status
    await later(hass, freezer, seconds=6)
    assert await runtime.thermal_store.async_load() is not None
    assert await hass.config_entries.async_unload(central.entry_id)
    hass.data[DOMAIN]["runtimes"].clear()
    assert await hass.config_entries.async_setup(central.entry_id)
    await hass.async_block_till_done()
    assert central.runtime_data.booking_results["west"] == result
    assert central.runtime_data.thermal_rates["west"]["mild"].heating_buffer == buffer


async def test_booking_priority_rises_on_timer_but_saving_presets_do_not_get_it(
    hass, freezer
):
    from tests.test_bookings_runtime import office as booking_office

    central, _, _ = await booking_office(hass, start=20, units=True)
    runtime = central.runtime_data
    assert runtime.zone_demands["west"].priority == 1
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    before = runtime.zone_demands["west"].priority
    assert 1 < before <= 2
    await later(hass, freezer, minutes=1)
    assert runtime.zone_demands["west"].priority > before
    runtime.set_thermostat("west", preset="eco")
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].priority == 1
    runtime.set_thermostat("west", preset="comfort", hvac_mode="off")
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].priority == 1


@pytest.mark.parametrize(
    "entity,last,override",
    [
        ("cover.other", "valid", "valid"),
        (SCREEN, "invalid", "invalid"),
        (SCREEN, "future", "expired"),
        (SCREEN, "naive", "naive"),
    ],
)
async def test_screen_restore_rejects_wrong_identity_and_invalid_times(
    hass, entity, last, override
):
    central, _ = await office(hass)
    runtime = central.runtime_data
    now = dt_util.utcnow()
    values = {
        "valid": now.isoformat(),
        "invalid": "nonsense",
        "future": (now + timedelta(days=1)).isoformat(),
        "expired": (now - timedelta(minutes=1)).isoformat(),
        "naive": now.replace(tzinfo=None).isoformat(),
    }
    runtime.load_screen_state(
        {
            EQUIPMENT: {
                "entity_id": entity,
                "last_move": values[last],
                "override_until": values[override],
            }
        }
    )
    assert EQUIPMENT not in runtime.screen_last_move
    assert EQUIPMENT not in runtime.screen_overrides


def test_profile_with_invalid_metadata_does_not_gain_trust():
    from custom_components.zonentra.bookings import ThermalRates, rate_quality

    now = dt_util.utcnow()
    rates = ThermalRates.restore(
        {
            "heating": 0.1,
            "heating_samples": 100,
            "heating_variance": float("nan"),
            "heating_updated": now.isoformat(),
        }
    )
    assert rate_quality("heating", rates, now)["status"] == "age_unknown"
