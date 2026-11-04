"""0.11.0 integration: screens follow sunshine, sun position and room climate."""

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.zonentra.const import DOMAIN
from tests.test_entry_layout import CELSIUS, add_zone, setup_central, zone_options
from tests.test_thermostat_runtime import later

SCREEN = "cover.west_screen"
EQUIPMENT = "shade_west_screen"


def entity(hass, platform, unique_id):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def office(hass, **settings):
    hass.states.async_set("sensor.west_temperature", "23.5", CELSIUS)
    hass.states.async_set("sensor.solar_power", "2500", {"unit_of_measurement": "W"})
    hass.states.async_set("sun.sun", "above_horizon", {"azimuth": 180, "elevation": 50})
    hass.states.async_set(SCREEN, "open", {"current_position": 100})
    central = await setup_central(hass)
    building = {
        **central.options["building"],
        "sun_power_entity": "sensor.solar_power",
        **settings,
    }
    hass.config_entries.async_update_entry(
        central, options={**central.options, "building": building}
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    zone = await add_zone(
        hass, zone_options("west", shading_entities=[SCREEN], facade="south")
    )
    return central, zone


async def test_screen_simulates_then_shades_and_respects_people(hass, freezer):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, zone = await office(hass)
    runtime = central.runtime_data
    sensor_id = entity(hass, "sensor", f"{zone.entry_id}_{EQUIPMENT}_screen")
    # Sunshine needs 5 minutes of solar power above the threshold.
    assert hass.states.get(sensor_id).attributes["reason"] == "no_sun"
    await later(hass, freezer, minutes=6)
    state = hass.states.get(sensor_id)
    # Sun on the south facade and a warm room (23.5 °C in 22-24): down.
    assert state.state == "shade"
    assert state.attributes["reason"] == "room_warm"
    assert state.attributes["status"] == "simulation"  # Off by default.
    assert state.attributes["current_position"] == 100  # Advice, not the screen.
    assert moves == []

    switch_id = entity(hass, "switch", f"{zone.entry_id}_screen_regulation")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert not moves
    assert runtime.screen_moves[EQUIPMENT].status == "stabilizing"
    await later(hass, freezer, minutes=5)
    assert [call.data["position"] for call in moves] == [0]
    hass.states.async_set(SCREEN, "closing", {"current_position": 40})
    hass.states.async_set(SCREEN, "closed", {"current_position": 0})
    await hass.async_block_till_done()
    assert runtime.last_transitions[EQUIPMENT][0] == "zonentra_screen"
    assert EQUIPMENT not in runtime.screen_overrides

    # A person opens it in Home Assistant: respected for 2 hours.
    hass.states.async_set(
        SCREEN, "open", {"current_position": 100}, context=Context(user_id="u")
    )
    await hass.async_block_till_done()
    assert EQUIPMENT in runtime.screen_overrides
    await later(hass, freezer, minutes=30)
    assert len(moves) == 1
    assert runtime.screen_moves[EQUIPMENT].status == "manual_override"
    await later(hass, freezer, minutes=91)
    assert len(moves) == 1
    await later(hass, freezer, minutes=5)
    assert [call.data["position"] for call in moves] == [0, 0]


async def test_night_and_heating_open_the_screen(hass, freezer):
    async_mock_service(hass, "cover", "set_cover_position")
    central, zone = await office(hass)
    runtime = central.runtime_data
    await later(hass, freezer, minutes=6)
    hass.states.async_set("sensor.west_temperature", "20", CELSIUS)  # Heating.
    await hass.async_block_till_done()
    assert runtime.screen_decisions[EQUIPMENT].reason == "solar_gain_heating"
    hass.states.async_set("sun.sun", "below_horizon", {"azimuth": 300, "elevation": -4})
    await hass.async_block_till_done()
    decision = runtime.screen_decisions[EQUIPMENT]
    assert (decision.target, decision.reason, decision.position) == (
        "open",
        "night",
        100,
    )


async def test_manual_closing_is_respected_before_reaching_the_end(hass, freezer):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await office(hass, screen_open_delay_minutes=0)
    runtime = central.runtime_data
    hass.states.async_set("sensor.west_temperature", "20", CELSIUS)
    runtime.set_thermostat("west", screen_regulation=True)
    await hass.async_block_till_done()
    assert not moves

    # The wall button is visible in HA as a moving cover. Do not wait for
    # 'closed': a position report while closing must not trigger an opening.
    hass.states.async_set(SCREEN, "closing", {"current_position": 100})
    await hass.async_block_till_done()
    assert EQUIPMENT in runtime.screen_overrides
    hass.states.async_set(SCREEN, "closing", {"current_position": 50})
    await hass.async_block_till_done()
    assert not moves
    assert runtime.screen_moves[EQUIPMENT].status == "manual_override"
    hass.states.async_set(SCREEN, "closed", {"current_position": 0})
    await later(hass, freezer, minutes=60)
    assert not moves


async def test_person_taking_over_right_after_our_move(hass, freezer):
    """0.13.4: a person closing the screen at the room controller right after
    Zonentra opened it (command not yet confirmed) is respected too."""
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, zone = await office(hass)
    runtime = central.runtime_data
    # Closed by an automation (no manual override), the room in its cool half.
    hass.states.async_set(
        SCREEN, "closed", {"current_position": 0}, context=Context(parent_id="a")
    )
    hass.states.async_set("sensor.west_temperature", "22.5", CELSIUS)
    await later(hass, freezer, minutes=6)
    runtime.set_thermostat("west", screen_regulation=True)
    await hass.async_block_till_done()
    assert not moves
    await later(hass, freezer, minutes=15)
    assert [call.data["position"] for call in moves] == [100]  # Up: room cool.
    # Within the confirmation time the room controller sends it down again.
    hass.states.async_set(SCREEN, "opening", {"current_position": 0})
    await hass.async_block_till_done()
    assert EQUIPMENT not in runtime.screen_overrides
    hass.states.async_set(SCREEN, "closing", {"current_position": 20})
    await hass.async_block_till_done()
    assert EQUIPMENT in runtime.screen_overrides
    assert not runtime.cover_tracker.pending
    hass.states.async_set(SCREEN, "closed", {"current_position": 0})
    await hass.async_block_till_done()
    assert runtime.last_transitions[EQUIPMENT][0] == "manual_screen"
    assert EQUIPMENT in runtime.screen_overrides
    await later(hass, freezer, minutes=60)
    assert len(moves) == 1  # Not opened again during the manual time.
    assert runtime.screen_moves[EQUIPMENT].status == "manual_override"


async def test_first_move_waits_for_sun_and_continuous_shading_demand(hass, freezer):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await office(hass)
    runtime = central.runtime_data
    runtime.set_thermostat("west", screen_regulation=True)
    await later(hass, freezer, minutes=5)
    assert not moves
    assert runtime.screen_moves[EQUIPMENT].status == "stabilizing"
    await later(hass, freezer, minutes=4, seconds=59)
    assert not moves
    await later(hass, freezer, seconds=1)
    assert [call.data["position"] for call in moves] == [0]


async def test_reversal_waits_for_interval_and_restarts_when_target_changes(
    hass, freezer
):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await office(hass)
    runtime = central.runtime_data
    runtime.set_thermostat("west", screen_regulation=True)
    await later(hass, freezer, minutes=5)
    await later(hass, freezer, minutes=5)
    assert len(moves) == 1
    hass.states.async_set(SCREEN, "closed", {"current_position": 0})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.west_temperature", "20", CELSIUS)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=15)
    assert len(moves) == 1
    assert runtime.screen_moves[EQUIPMENT].status == "interval"
    # Returning to shade cancels the pending opening request.
    hass.states.async_set("sensor.west_temperature", "23.5", CELSIUS)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=14)
    hass.states.async_set("sensor.west_temperature", "20", CELSIUS)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=1)
    assert len(moves) == 1  # 30-minute move interval ended, target still too new.
    assert runtime.screen_moves[EQUIPMENT].status == "stabilizing"
    await later(hass, freezer, minutes=14)
    assert [call.data["position"] for call in moves] == [0, 100]


async def test_unknown_sun_interrupts_stability_and_reload_preserves_new_timer(
    hass, freezer
):
    moves = async_mock_service(hass, "cover", "set_cover_position")
    central, _ = await office(hass)
    runtime = central.runtime_data
    runtime.set_thermostat("west", screen_regulation=True)
    await later(hass, freezer, minutes=5)
    await later(hass, freezer, minutes=4)
    hass.states.async_set("sun.sun", "unavailable")
    await hass.async_block_till_done()
    assert EQUIPMENT not in runtime.screen_targets
    hass.states.async_set("sun.sun", "above_horizon", {"azimuth": 180, "elevation": 50})
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=3)
    assert not moves
    pending = runtime.screen_targets[EQUIPMENT]
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    assert central.runtime_data.screen_targets[EQUIPMENT] == pending
    await later(hass, freezer, minutes=2)
    assert [call.data["position"] for call in moves] == [0]
