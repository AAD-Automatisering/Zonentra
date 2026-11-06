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

# 09:00 in HA's default US/Pacific timezone keeps ordinary openings on today.
# The local-midnight test below explicitly moves the clock across the boundary.
pytestmark = pytest.mark.freeze_time("2026-10-19T16:00:00+00:00")

SCHEDULE = "schedule.office_hours"
PRESENCE = "input_boolean.building_present"
WORKDAY = "binary_sensor.workday"


async def office(
    hass,
    *,
    start=45,
    temperature="21",
    workday=None,
    presence_entity=PRESENCE,
    **zone_fields,
):
    opening = dt_util.utcnow() + timedelta(minutes=start)
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening})
    hass.states.async_set(
        presence_entity,
        "armed_away" if presence_entity.startswith("alarm_control_panel.") else "off",
    )
    if workday is not None:
        hass.states.async_set(WORKDAY, workday)
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
        {
            "office_schedule_entity": SCHEDULE,
            "presence_entity": presence_entity,
            **({"workday_entity": WORKDAY} if workday is not None else {}),
        },
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


async def test_building_presence_outside_hours_does_not_heat_every_room(hass):
    central, _, _ = await office(hass, start=300)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert central.runtime_data.zone_bands["west"][0] == "away"


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


@pytest.mark.parametrize(
    "entity", ["schedule.office", "sensor.workday", "binary_sensor", True]
)
def test_workday_requires_a_binary_sensor(entity):
    with pytest.raises((ValueError, TypeError)):
        build_model("test", "Test", {"building": {"workday_entity": entity}})


async def test_workday_sensor_is_saved_followed_and_can_be_removed(hass):
    central, room, _ = await office(hass, start=20, workday="off")
    assert central.options["building"]["workday_entity"] == WORKDAY
    runtime = central.runtime_data
    assert WORKDAY in runtime.building.entity_ids
    assert "workday_entity" not in room.options
    assert runtime.office_plans["west"].status == "not_workday"
    assert runtime.zone_bands["west"][0] == "away"
    hass.states.async_set(WORKDAY, "on")
    await hass.async_block_till_done()
    assert runtime.office_plans["west"].active
    hass.states.async_set(WORKDAY, "off")
    await hass.async_block_till_done()
    assert not runtime.office_plans["west"].active
    flow = await open_section(hass, central, "presence")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"office_schedule_entity": SCHEDULE, "presence_entity": PRESENCE},
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert WORKDAY not in central.runtime_data.building.entity_ids
    assert central.runtime_data.office_plans["west"].active


@pytest.mark.parametrize(
    "state,attributes",
    [
        ("unknown", {}),
        ("unavailable", {}),
        ("on", {"restored": True}),
        ("on", {"assumed_state": True}),
        (None, {}),
    ],
)
async def test_unusable_workday_requires_local_use(hass, state, attributes):
    central, _, _ = await office(hass, start=20, workday="on")
    assert central.runtime_data.office_plans["west"].active
    if state is None:
        hass.states.async_remove(WORKDAY)
    else:
        hass.states.async_set(WORKDAY, state, attributes)
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].status == "workday_unknown"
    assert central.runtime_data.zone_bands["west"][0] == "away"
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert central.runtime_data.zone_bands["west"][0] == "away"


async def test_holiday_cancels_arrival_grace_and_requires_local_use(hass, freezer):
    central, _, opening = await office(hass, start=20, workday="on")
    await later(hass, freezer, minutes=20)
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening + timedelta(hours=8)})
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].status == "arrival_grace"
    hass.states.async_set(WORKDAY, "off")
    await hass.async_block_till_done()
    assert central.runtime_data.office_plans["west"].status == "not_workday"
    assert central.runtime_data.zone_bands["west"][0] == "away"
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert central.runtime_data.zone_bands["west"][0] == "away"


async def test_holiday_does_not_block_calendar_preparation(hass):
    opening = dt_util.utcnow() + timedelta(minutes=20)
    hass.states.async_set(CALENDAR, "off")
    calendar_service(
        hass,
        {
            CALENDAR: {
                "events": [
                    {
                        "start": opening.isoformat(),
                        "end": (opening + timedelta(hours=1)).isoformat(),
                        "summary": "Meeting",
                    }
                ]
            }
        },
    )
    central, _, _ = await office(
        hass, start=20, workday="off", calendar_entity=CALENDAR
    )
    assert central.runtime_data.office_plans["west"].status == "calendar_priority"
    assert central.runtime_data.booking_plans["west"].active
    assert central.runtime_data.zone_bands["west"][0] == "comfort"


@pytest.mark.parametrize("workday", ["on", "off"])
async def test_tomorrows_opening_is_reassessed_after_local_midnight(
    hass, freezer, workday
):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    freezer.move_to(
        "2026-10-18T21:30:00+00:00"
    )  # 23:30 local, opening tomorrow at 00:15.
    central, _, _ = await office(hass, start=45, temperature="18", workday=workday)
    runtime = central.runtime_data
    assert not runtime.office_plans["west"].active
    assert runtime.office_plans["west"].status == (
        "waiting_for_workday" if workday == "on" else "not_workday"
    )
    await later(hass, freezer, minutes=30)
    hass.states.async_set(WORKDAY, "on")
    await hass.async_block_till_done()
    assert runtime.office_plans["west"].active
    hass.states.async_set(WORKDAY, "off")
    await hass.async_block_till_done()
    assert not runtime.office_plans["west"].active


async def test_workday_entity_is_redacted_from_diagnostics(hass):
    central, room, _ = await office(hass, start=20, workday="off")
    for entry in (central, room):
        exported = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
        assert WORKDAY not in exported
        assert "not_workday" in exported


@pytest.mark.parametrize("workday", [None, "off"])
async def test_disarming_outside_hours_only_allows_manually_selected_room(
    hass, freezer, workday
):
    central, _, _ = await office(hass, start=240, workday=workday)
    hass.states.async_set("sensor.east_temperature", "21", CELSIUS)
    hass.states.async_set("climate.east_ac", "off")
    await add_zone(hass, zone_options("east", "climate.east_ac", group_id=None))
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"] == ("away", "outside_office_hours")
    assert runtime.zone_bands["east"][0] == "away"

    hass.states.async_set("climate.west_ac", "heat", {"temperature": 22})
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"] == ("comfort", "room_controller_use")
    assert runtime.zone_bands["east"][0] == "away"
    assert "hvac_west_ac" not in runtime.overrides
    await later(hass, freezer, minutes=90)
    assert "hvac_west_ac" not in runtime.overrides
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert runtime.zone_bands["west"] == ("comfort", "room_controller_use")

    hass.states.async_set(PRESENCE, "off")
    await hass.async_block_till_done()
    assert not runtime.room_controller_use
    assert "hvac_west_ac" not in runtime.overrides
    assert runtime.zone_bands["west"][0] == "away"
    # Disarming again must not revive a still-running unit's old authorization.
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"


async def test_room_controller_use_continues_after_closing_until_manual_off(
    hass, freezer
):
    central, _, opening = await office(hass, start=240)
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening})
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "comfort"
    hass.states.async_set("climate.west_ac", "heat", {"temperature": 22})
    await hass.async_block_till_done()
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=1)})
    await hass.async_block_till_done()
    await later(hass, freezer, hours=2)
    assert runtime.zone_bands["west"] == ("comfort", "room_controller_use")
    hass.states.async_set("climate.west_ac", "off", {"temperature": 22})
    await hass.async_block_till_done()
    assert not runtime.room_controller_use
    assert runtime.zone_bands["west"][0] == "away"


async def test_automation_and_startup_do_not_authorize_room_use(hass):
    from homeassistant.core import Context

    central, _, _ = await office(hass, start=240)
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set(
        "climate.west_ac", "heat", context=Context(parent_id="automation")
    )
    await hass.async_block_till_done()
    assert not runtime.room_controller_use
    assert runtime.zone_bands["west"][0] == "away"
    runtime.refresh()
    assert not runtime.room_controller_use


async def test_real_alarm_releases_manual_hold_and_sends_stop(hass, freezer):
    alarm = "alarm_control_panel.office"
    mocks = await mock_climate(hass)
    central, _, _ = await office(hass, start=300, presence_entity=alarm)
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    await hass.async_block_till_done()
    hass.states.async_set(alarm, "disarmed")
    await hass.async_block_till_done()
    clear(mocks)
    assert runtime.zone_bands["west"][0] == "away"
    assert not mocks["set_hvac_mode"]
    hass.states.async_set("climate.west_ac", "heat", {"temperature": 22})
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=90)
    assert "hvac_west_ac" not in runtime.overrides
    clear(mocks)
    hass.states.async_set(alarm, "armed_away")
    await hass.async_block_till_done()
    assert not runtime.room_controller_use
    assert "hvac_west_ac" not in runtime.overrides
    assert any(
        call.data["entity_id"] == "climate.west_ac" for call in mocks["turn_off"]
    )


@pytest.mark.parametrize(
    "changes", [{"hvac_mode": "off"}, {"preset": "eco"}, {"preset": "away"}]
)
async def test_explicit_thermostat_choice_ends_room_controller_use(hass, changes):
    central, _, _ = await office(hass, start=300)
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set("climate.west_ac", "heat")
    await hass.async_block_till_done()
    assert runtime.room_controller_use
    runtime.set_thermostat("west", **changes)
    await hass.async_block_till_done()
    assert not runtime.room_controller_use
    assert "hvac_west_ac" not in runtime.overrides


async def select_workday_room(hass, central, selected="west", workday=WORKDAY):
    flow = await open_section(hass, central, "presence")
    options = flow["data_schema"].schema["workday_presence_zone_id"].config["options"]
    assert {"value": "west", "label": "West"} in options
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "office_schedule_entity": SCHEDULE,
            "presence_entity": central.options["building"]["presence_entity"],
            "workday_presence_zone_id": selected,
            **({"workday_entity": workday} if workday else {}),
        },
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    return central.runtime_data


async def test_workday_arrival_only_activates_selected_room_with_normal_regulation(
    hass,
    freezer,
):
    mocks = await mock_climate(hass)
    alarm = "alarm_control_panel.office"
    central, _, _ = await office(hass, start=300, workday="on", presence_entity=alarm)
    hass.states.async_set("sensor.east_temperature", "21", CELSIUS)
    hass.states.async_set("climate.east_ac", "off")
    await add_zone(hass, zone_options("east", "climate.east_ac", group_id=None))
    runtime = await select_workday_room(hass, central)
    runtime.set_thermostat("west", regulation=True)
    await later(hass, freezer, minutes=4)
    clear(mocks)
    hass.states.async_set(alarm, "disarmed")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"] == ("comfort", "workday_presence")
    assert runtime.zone_bands["east"] == ("away", "outside_office_hours")
    assert not runtime.room_controller_use
    assert not runtime.overrides
    assert any(c.data.get("hvac_mode") == "heat" for c in mocks["set_hvac_mode"])
    assert all(c.data["entity_id"] == "climate.west_ac" for c in mocks["set_hvac_mode"])
    hass.states.async_set(alarm, "armed_away")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"


@pytest.mark.parametrize(
    "state,attrs",
    [
        ("off", {}),
        ("unknown", {}),
        ("unavailable", {}),
        ("on", {"restored": True}),
        ("on", {"assumed_state": True}),
        (None, {}),
    ],
)
async def test_selected_room_requires_confirmed_workday(hass, state, attrs):
    central, _, _ = await office(hass, start=300, workday="on")
    runtime = await select_workday_room(hass, central)
    if state is None:
        hass.states.async_remove(WORKDAY)
    else:
        hass.states.async_set(WORKDAY, state, attrs)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"


@pytest.mark.parametrize(
    "day", ["2026-10-24T16:00:00+00:00", "2026-10-25T16:00:00+00:00"]
)
async def test_weekend_never_automatically_activates_selected_room(hass, freezer, day):
    freezer.move_to(day)
    # Even an accidentally-on workday sensor cannot enable the weekend exception.
    central, _, _ = await office(hass, start=300, workday="on")
    runtime = await select_workday_room(hass, central)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"
    hass.states.async_set("climate.west_ac", "heat")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"] == ("comfort", "room_controller_use")


async def test_workday_room_requires_sensor_and_can_be_cleared(hass):
    central, _, _ = await office(hass, start=300)
    runtime = await select_workday_room(hass, central, workday=None)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"
    hass.states.async_set(WORKDAY, "on")
    runtime = await select_workday_room(hass, central)
    assert runtime.zone_bands["west"][1] == "workday_presence"
    runtime = await select_workday_room(hass, central, selected="")
    assert runtime.building.settings.workday_presence_zone_id is None
    assert runtime.zone_bands["west"][0] == "away"


async def test_manual_off_suppresses_workday_exception_until_next_alarm_cycle(
    hass, freezer
):
    central, _, _ = await office(hass, start=300, workday="on")
    runtime = await select_workday_room(hass, central)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    # This is an automatic start, not a manual room-controller takeover.

    runtime.command_tracker.register("climate.west_ac", "heat", dt_util.utcnow())
    hass.states.async_set("climate.west_ac", "heat")
    await hass.async_block_till_done()
    hass.states.async_set("climate.west_ac", "off")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"
    await later(hass, freezer, minutes=45)
    assert runtime.zone_bands["west"][0] == "away"
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert runtime.zone_bands["west"][0] == "away"
    hass.states.async_set(PRESENCE, "off")
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][1] == "workday_presence"


async def test_workday_exception_ends_at_local_saturday_midnight(hass, freezer):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    freezer.move_to("2026-10-23T21:30:00+00:00")  # Friday 23:30 local.
    central, _, _ = await office(hass, start=3000, workday="on")
    runtime = await select_workday_room(hass, central)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][1] == "workday_presence"
    await later(hass, freezer, minutes=30)
    assert runtime.zone_bands["west"][0] == "away"


async def test_workday_room_selection_is_redacted(hass):
    central, _, _ = await office(hass, start=300, workday="on")
    await select_workday_room(hass, central)
    diagnostic = await async_get_config_entry_diagnostics(hass, central)
    assert '"workday_presence_zone_id": "**REDACTED**"' in json.dumps(diagnostic)


async def test_room_controller_use_keeps_setpoint_fan_and_learning_active(
    hass, freezer
):
    mocks = await mock_climate(hass)
    hass.states.async_set("cover.review_screen", "open", {"current_position": 100})
    central, room, _ = await office(
        hass, start=300, shading_entities=["cover.review_screen"]
    )
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    await later(hass, freezer, minutes=4)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    clear(mocks)
    hass.states.async_set(
        "climate.west_ac",
        "heat",
        {
            "temperature": 22,
            "hvac_action": "heating",
            "fan_mode": "25%",
            "fan_modes": ["25%", "50%", "75%", "100%"],
        },
    )
    await hass.async_block_till_done()
    equipment = "hvac_west_ac"
    assert equipment in runtime.room_controller_use
    assert equipment not in runtime.overrides
    assert runtime.setpoint_plans[equipment].setpoint is not None
    assert runtime.setpoint_plans[equipment].status != "manual_override"
    assert mocks["set_temperature"]
    assert mocks["set_fan_mode"]
    assert runtime.thermal_learning["west"]["eligible"]
    await later(hass, freezer, minutes=5)
    hass.states.async_set("sensor.west_temperature", "21", CELSIUS, force_update=True)
    await hass.async_block_till_done()
    assert runtime.heating_corrections[equipment].status == "observing"
    details = runtime.manual_control_details(equipment)
    assert details["room_controller_use"]
    assert details["override_started_at"] is None
    assert details["last_manual_event"]["transition"] == "manual_on"
    assert details["last_manual_event"]["from_mode"] == "off"
    assert details["last_manual_event"]["to_mode"] == "heat"
    initial_event = details["last_manual_event"]

    # The afternoon screen remote only creates a screen override.
    hass.states.async_set("cover.review_screen", "closed", {"current_position": 0})
    await hass.async_block_till_done()
    assert runtime.screen_overrides
    assert equipment not in runtime.overrides
    assert (
        runtime.manual_control_details(equipment)["last_manual_event"] == initial_event
    )
    assert runtime.setpoint_plans[equipment].setpoint is not None
    diagnostic = await async_get_config_entry_diagnostics(hass, room)
    assert diagnostic["unit_intents"][0]["room_controller_use"]
    assert not diagnostic["unit_intents"][0]["manual_override_active"]


async def test_reload_migrates_old_indefinite_override_without_losing_room_use(hass):
    from custom_components.zonentra.override import ManualOverride

    central, _, _ = await office(hass, start=300)
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set("climate.west_ac", "heat", {"temperature": 22})
    await hass.async_block_till_done()
    equipment = "hvac_west_ac"
    runtime.overrides[equipment] = ManualOverride(
        equipment, dt_util.utcnow(), None, "heat"
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert equipment in runtime.room_controller_use
    assert equipment not in runtime.overrides
    assert runtime.setpoint_plans[equipment].setpoint is not None


async def test_screen_remote_does_not_start_airco_room_use(hass):
    hass.states.async_set("cover.review_screen", "open", {"current_position": 100})
    central, _, _ = await office(
        hass, start=300, shading_entities=["cover.review_screen"]
    )
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    hass.states.async_set("cover.review_screen", "closed", {"current_position": 0})
    await hass.async_block_till_done()
    assert runtime.screen_overrides
    assert not runtime.room_controller_use
    assert not runtime.overrides
    assert "hvac_west_ac" not in runtime.manual_events


async def test_explicit_user_setpoint_still_temporarily_pauses_room_controller_use(
    hass, freezer
):
    from homeassistant.core import Context

    await mock_climate(hass)
    central, _, _ = await office(hass, start=300)
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set("climate.west_ac", "heat", {"temperature": 22})
    await hass.async_block_till_done()
    equipment = "hvac_west_ac"
    assert equipment in runtime.room_controller_use
    assert equipment not in runtime.overrides
    hass.states.async_set(
        "climate.west_ac", "heat", {"temperature": 25}, context=Context(user_id="u")
    )
    await hass.async_block_till_done()
    assert runtime.overrides[equipment].expires_at is not None
    assert runtime.setpoint_plans[equipment].status == "manual_override"
    details = runtime.manual_control_details(equipment)
    assert details["last_manual_event"]["transition"] == "manual_setpoint"
    assert details["last_manual_event"]["origin"] == "ha_user"
    assert details["override_started_at"] is not None
    await later(hass, freezer, minutes=31)
    assert equipment in runtime.room_controller_use
    assert equipment not in runtime.overrides
    assert runtime.setpoint_plans[equipment].setpoint is not None


async def test_normal_workday_use_continues_after_closing_until_alarm_is_armed(
    hass, freezer
):
    mocks = await mock_climate(hass)
    central, _, opening = await office(
        hass, start=300, workday="on", native_regulation=True
    )
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    await later(hass, freezer, minutes=4)
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening})
    await hass.async_block_till_done()
    assert "west" in runtime.office_use
    hass.states.async_set(
        "climate.west_ac", "heat", {"temperature": 22, "hvac_action": "heating"}
    )
    hass.states.async_set("sensor.west_temperature", "22.4", CELSIUS)
    await hass.async_block_till_done()
    clear(mocks)
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=3)})
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"] == ("comfort", "office_continued_use")
    assert not runtime.room_controller_use
    assert not runtime.overrides
    assert runtime.unit_intents["hvac_west_ac"].reason == "native_regulation"
    assert not mocks["turn_off"]
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert runtime.zone_bands["west"] == ("comfort", "office_continued_use")
    hass.states.async_set(PRESENCE, "off")
    await hass.async_block_till_done()
    assert not runtime.office_use
    assert runtime.zone_bands["west"][0] == "away"
    # A second visit after hours must not revive the preceding office day.
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert runtime.zone_bands["west"][0] == "away"


async def test_closing_without_actual_arrival_does_not_continue_preparation(hass):
    central, _, opening = await office(hass, start=20)
    runtime = central.runtime_data
    assert runtime.office_plans["west"].active
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening})
    await hass.async_block_till_done()
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=3)})
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert not runtime.office_use
    assert runtime.zone_bands["west"][0] == "away"


async def test_manual_off_cancels_continued_office_use(hass, freezer):
    central, _, opening = await office(hass, start=300)
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening})
    await hass.async_block_till_done()
    runtime.command_tracker.register("climate.west_ac", "heat", dt_util.utcnow())
    hass.states.async_set("climate.west_ac", "heat", {"temperature": 22})
    await hass.async_block_till_done()
    assert "west" in runtime.office_use
    hass.states.async_set("climate.west_ac", "off", {"temperature": 22})
    await hass.async_block_till_done()
    assert "west" not in runtime.office_use
    assert "west" in runtime.office_use_suppressed
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=3)})
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=45)
    assert runtime.zone_bands["west"][0] == "away"


async def test_continued_office_use_ends_when_thermostat_is_turned_off(hass):
    central, _, opening = await office(hass, start=300)
    runtime = central.runtime_data
    hass.states.async_set(PRESENCE, "on")
    hass.states.async_set(SCHEDULE, "on", {"next_event": opening})
    await hass.async_block_till_done()
    hass.states.async_set(SCHEDULE, "off", {"next_event": opening + timedelta(days=3)})
    await hass.async_block_till_done()
    assert "west" in runtime.office_use
    runtime.set_thermostat("west", hvac_mode="off")
    assert not runtime.office_use
    runtime.set_thermostat("west", hvac_mode="heat_cool")
    assert runtime.zone_bands["west"][0] == "away"
