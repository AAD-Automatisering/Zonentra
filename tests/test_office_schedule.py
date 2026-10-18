"""Central schedule selection and adaptive preparation without false presence."""

import json
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics
from custom_components.zonentra.models import build_model
from tests.test_bookings_runtime import CALENDAR, calendar_service, later
from tests.test_central_options import open_section
from tests.test_entry_layout import CELSIUS, add_zone, setup_central, zone_options
from tests.test_thermostat_runtime import clear, mock_climate

SCHEDULE = "schedule.office_hours"
PRESENCE = "input_boolean.building_present"


async def office(hass, *, start=45, temperature="21", **zone_fields):
    opening = dt_util.utcnow() + timedelta(minutes=start)
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening})
    hass.states.async_set(PRESENCE, "off")
    hass.states.async_set("sensor.west_temperature", temperature, CELSIUS)
    hass.states.async_set("sensor.outside", "15", CELSIUS)
    hass.states.async_set("climate.west_ac", "off", {"temperature": 22})
    central = await setup_central(hass)
    flow = await open_section(hass, central, "general")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"outdoor_temperature_entity": "sensor.outside"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "presence"}
    )
    assert flow["data_schema"].schema["office_schedule_entity"].config["filter"][0][
        "domain"
    ] == ["schedule"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"office_schedule_entity": SCHEDULE, "presence_entity": PRESENCE},
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    zone = await add_zone(
        hass, zone_options("west", "climate.west_ac", group_id=None, **zone_fields)
    )
    return central, zone, opening


@pytest.mark.parametrize(
    "entity", ["sensor.office", "calendar.office", "schedule", True]
)
def test_only_schedule_entities_are_accepted(entity):
    with pytest.raises((ValueError, TypeError)):
        build_model("test", "Test", {"building": {"office_schedule_entity": entity}})


async def test_prepares_without_new_measurements_and_stops_after_no_arrival(
    hass, freezer
):
    central, zone, opening = await office(hass)
    runtime = central.runtime_data
    assert runtime.office_plans["west"].status == "scheduled"
    assert runtime.zone_bands["west"][0] == "away"
    assert SCHEDULE in runtime.building.entity_ids
    assert "office_schedule_entity" not in zone.options
    await later(hass, freezer, minutes=10)
    assert runtime.office_plans["west"].status == "preparing"
    assert runtime.zone_bands["west"] == ("comfort", "office_preparing")
    assert runtime.presence == "absent"
    await later(hass, freezer, minutes=35)
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening + timedelta(hours=8)})
    await hass.async_block_till_done()
    assert runtime.office_plans["west"].status == "arrival_grace"
    await later(hass, freezer, minutes=14)
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert runtime.office_plans["west"].status == "arrival_grace"
    await later(hass, freezer, minutes=1)
    assert runtime.office_plans["west"].status == "open"
    assert runtime.zone_bands["west"] == ("away", "building_absent")
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "comfort"
    hass.states.async_set(PRESENCE, "off")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"


@pytest.mark.parametrize(
    "temperature,demand", [("20", "heating_demand"), ("27", "cooling_demand")]
)
async def test_preparation_controls_enabled_hvac_only(
    hass, freezer, temperature, demand
):
    mocks = await mock_climate(hass)
    central, _, _ = await office(hass, start=20, temperature=temperature)
    runtime = central.runtime_data
    await later(hass, freezer, minutes=4)
    clear(mocks)
    assert runtime.office_plans["west"].active
    assert runtime.zone_demands["west"].demand == demand
    assert not mocks["set_hvac_mode"]
    runtime.set_thermostat("west", regulation=True, hvac_mode="off")
    await hass.async_block_till_done()
    assert not mocks["set_hvac_mode"]
    runtime.set_thermostat("west", hvac_mode="heat_cool")
    await hass.async_block_till_done()
    assert mocks["set_hvac_mode"]


@pytest.mark.parametrize("preset", ["eco", "away"])
async def test_preparation_respects_saving_presets(hass, preset):
    central, _, _ = await office(hass, start=20)
    runtime = central.runtime_data
    runtime.set_thermostat("west", preset=preset)
    assert runtime.office_plans["west"].active
    assert runtime.zone_bands["west"] == (preset, "preset")


@pytest.mark.parametrize(
    "attributes",
    [
        None,
        {"restored": True},
        {"next_event": "invalid"},
        {"next_event": "2026-10-18T08:00:00"},
    ],
)
async def test_unusable_schedule_cannot_request_comfort(hass, attributes):
    central, _, opening = await office(hass, start=20)
    assert central.runtime_data.office_plans["west"].active
    hass.states.async_set(
        SCHEDULE, "unavailable" if attributes is None else "off", attributes or {}
    )
    await hass.async_block_till_done()
    assert not central.runtime_data.office_plans["west"].active
    assert central.runtime_data.zone_bands["west"][0] == "away"


async def test_removing_or_rescheduling_opening_cancels_preparation(hass):
    central, _, opening = await office(hass, start=20)
    runtime = central.runtime_data
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=1)})
    await hass.async_block_till_done()
    assert runtime.office_plans["west"].status == "scheduled"
    assert runtime.zone_bands["west"][0] == "away"
    flow = await open_section(hass, central, "presence")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"presence_entity": PRESENCE}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].status == "not_configured"
    assert SCHEDULE not in central.runtime_data.building.entity_ids


async def test_calendar_rooms_ignore_office_hours(hass):
    hass.states.async_set(CALENDAR, "off")
    calendar_service(hass, {CALENDAR: {"events": []}})
    central, _, _ = await office(hass, start=20, calendar_entity=CALENDAR)
    runtime = central.runtime_data
    assert runtime.office_plans["west"].status == "calendar_priority"
    assert runtime.zone_bands["west"][0] == "away"
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "eco"


async def test_open_office_at_startup_does_not_invent_arrival(hass):
    central, _, opening = await office(hass, start=120)
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening + timedelta(hours=8)})
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].status == "open"
    assert central.runtime_data.zone_bands["west"][0] == "away"


async def test_actual_presence_outside_hours_keeps_comfort(hass):
    central, _, _ = await office(hass, start=300)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert central.runtime_data.zone_bands["west"][0] == "comfort"


async def test_diagnostics_explain_plan_and_redact_schedule(hass):
    central, zone, _ = await office(hass, start=20)
    for entry in (central, zone):
        diagnostic = await async_get_config_entry_diagnostics(hass, entry)
        exported = json.dumps(diagnostic)
        assert SCHEDULE not in exported
        assert "office_schedule" in exported
        assert "preparing" in exported


async def test_winter_starts_earlier_and_rooms_share_schedule_not_start_time(hass):
    central, _, _ = await office(hass, start=60, temperature="20")
    flow = await open_section(hass, central, "general")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"outdoor_temperature_entity": "sensor.outside"}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    runtime = central.runtime_data
    mild = runtime.office_plans["west"]
    assert mild.status == "scheduled"
    hass.states.async_set("sensor.outside", "-5", CELSIUS)
    await hass.async_block_till_done()
    winter = runtime.office_plans["west"]
    assert winter.status == "preparing"
    assert winter.prepare_at < mild.prepare_at
    hass.states.async_set("sensor.east_temperature", "22", CELSIUS)
    await add_zone(hass, zone_options("east", group_id=None))
    runtime = central.runtime_data
    assert runtime.office_plans["east"].status == "scheduled"
    assert runtime.office_plans["west"].start == runtime.office_plans["east"].start
    assert runtime.zone_bands["east"][0] == "away"


async def test_office_learns_from_actual_heating(hass, freezer):
    from homeassistant.core import Context

    await mock_climate(hass)
    central, _, _ = await office(hass, start=20, temperature="20")
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    hass.states.async_set(
        "climate.west_ac",
        "heat",
        {"temperature": 22, "hvac_action": "heating"},
        context=Context(parent_id="test"),
    )
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=10)
    hass.states.async_set("sensor.west_temperature", "20.5", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.thermal_rates["west"]["mild"].heating == pytest.approx(0.05)


async def test_reaching_target_does_not_end_preparation_early(hass):
    central, _, _ = await office(hass, start=20)
    hass.states.async_set("sensor.west_temperature", "22.5", CELSIUS)
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].active
    assert central.runtime_data.zone_bands["west"][0] == "comfort"


async def test_zero_maximum_disables_preparation(hass):
    central, _, _ = await office(
        hass,
        start=20,
        use_building_preconditioning=False,
        preconditioning_max_minutes=0,
    )
    assert not central.runtime_data.office_plans["west"].active
    assert central.runtime_data.zone_bands["west"][0] == "away"


async def test_closing_short_block_ends_arrival_grace(hass, freezer):
    central, _, opening = await office(hass, start=20)
    await later(hass, freezer, minutes=20)
    hass.states.async_set(
        SCHEDULE, "on", {"next_event": opening + timedelta(minutes=5)}
    )
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].active
    await later(hass, freezer, minutes=5)
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=1)})
    await hass.async_block_till_done()
    assert not central.runtime_data.office_plans["west"].active
    assert central.runtime_data.zone_bands["west"][0] == "away"


async def test_schedule_transition_race_has_bounded_hold(hass, freezer):
    central, _, _ = await office(hass, start=20)
    await later(hass, freezer, minutes=20)
    runtime = central.runtime_data
    assert runtime.office_plans["west"].status == "awaiting_opening"
    assert runtime.zone_bands["west"][0] == "comfort"
    await later(hass, freezer, seconds=30)
    assert runtime.office_plans["west"].status == "unknown"
    assert runtime.zone_bands["west"][0] == "away"
