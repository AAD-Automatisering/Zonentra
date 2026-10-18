"""0.10.0 integration: room-state sensors, open window and automatic fan."""

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.zonentra.const import DOMAIN
from tests.test_entry_layout import (
    CELSIUS,
    add_zone,
    set_states,
    setup_central,
    zone_options,
)
from tests.test_thermostat_runtime import (
    EQUIPMENT,
    KNX,
    UNIT,
    calls,
    clear,
    later,
    mock_climate,
    unit,
)

WINDOW = "binary_sensor.west_window"


def entity(hass, platform, unique_id):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def office(hass, freezer, mocks, **fields):
    set_states(hass, "west", UNIT)
    hass.states.async_set(WINDOW, "off")
    central = await setup_central(hass)
    zone = await add_zone(
        hass, zone_options("west", UNIT, contact_entities=[WINDOW], **fields)
    )
    await unit(hass, "heat", **KNX)  # The integration applies the preset pulse.
    await later(hass, freezer, seconds=4)
    await unit(hass, "off", **KNX)
    clear(mocks)
    return central, zone


def state(hass, zone, key, platform="sensor"):
    return hass.states.get(entity(hass, platform, f"{zone.entry_id}_{key}"))


async def test_room_state_sensors(hass, freezer):
    mocks = await mock_climate(hass)
    central, zone = await office(hass, freezer, mocks)
    # 0.13.3: no motion sensor and no building presence: no presence sensor.
    assert entity(hass, "sensor", f"{zone.entry_id}_presence") is None
    # 21 °C, heating target 22.3 -> 1.3 °C away -> third speed. Since 0.13.5
    # the automatic fan is the default (in use, also in simulation).
    fan = state(hass, zone, "auto_fan")
    assert (fan.state, fan.attributes["in_use"]) == ("75%", True)
    assert state(hass, zone, "safety").state == "safe"
    assert state(hass, zone, "window").state == "closed"
    assert float(state(hass, zone, "ema_temperature").state) == 21
    assert state(hass, zone, "temperature_slope").state == "unknown"  # One sample.
    assert state(hass, zone, "regulated_setpoint").attributes["status"] == (
        "unit_not_running"
    )
    assert state(hass, zone, "window_bypass", "switch").state == "off"
    # The room sensor fails: unsafe, and no regulation on it.
    hass.states.async_set("sensor.west_temperature", "unavailable")
    await hass.async_block_till_done()
    assert state(hass, zone, "safety").state == "sensor_fault"
    # It recovers warmer: the trend rises.
    await later(hass, freezer, minutes=10)
    hass.states.async_set("sensor.west_temperature", "22", CELSIUS)
    await hass.async_block_till_done()
    assert state(hass, zone, "safety").state == "safe"
    assert float(state(hass, zone, "temperature_slope").state) > 0


async def test_open_window_stops_the_room(hass, freezer):
    mocks = await mock_climate(hass)
    central, zone = await office(hass, freezer, mocks)
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    await later(hass, freezer, minutes=4)
    await unit(hass, "heat", **KNX)  # Switched on by Zonentra for the demand.
    clear(mocks)
    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()
    assert state(hass, zone, "window").state == "opening"
    assert calls(mocks) == []
    await later(hass, freezer, seconds=31)
    assert state(hass, zone, "window").state == "open"
    assert runtime.zone_demands["west"].reason == "window_open"
    # Off at once, even within the minimum run time.
    assert calls(mocks) == [("turn_off", None)]
    await unit(hass, "off", **KNX)
    clear(mocks)
    # Bypass: the window is ignored and the room is regulated again.
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": entity(hass, "switch", f"{zone.entry_id}_window_bypass")},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert state(hass, zone, "window").state == "bypass"
    await later(hass, freezer, minutes=4)
    assert calls(mocks) == [("set_hvac_mode", "heat")]


async def test_automatic_fan(hass, freezer):
    mocks = await mock_climate(hass)
    central, zone = await office(hass, freezer, mocks)
    runtime = central.runtime_data
    thermostat = hass.states.get(entity(hass, "climate", f"{zone.entry_id}_thermostat"))
    # Automatic fan regulation works without thermostat fan-speed exposure.
    assert "fan_modes" not in thermostat.attributes
    runtime.set_thermostat("west", regulation=True, fan_mode="auto")
    await later(hass, freezer, minutes=4)
    await unit(hass, "heat", **KNX)
    # 21 °C, target 22.3: 1.3 °C away -> third speed.
    assert ("set_fan_mode", "75%") in calls(mocks)
    assert state(hass, zone, "auto_fan").state == "75%"
    assert EQUIPMENT not in runtime.overrides


async def test_vane_follows_cooling_and_heating(hass, freezer):
    mocks = await mock_climate(hass)
    vane = async_mock_service(hass, "select", "select_option")
    set_states(hass, "west", UNIT)
    hass.states.async_set("select.west_vane", "3")
    central = await setup_central(hass)
    await add_zone(
        hass,
        zone_options(
            "west",
            UNIT,
            vane_entity="select.west_vane",
            vane_cool_value="1",
            vane_heat_value="5",
        ),
    )
    # The preset pulse switches the unit to heat for a moment: vane down.
    await unit(hass, "heat", **KNX)
    assert [call.data["option"] for call in vane] == ["5"]
    await unit(hass, "heat", **(KNX | {"fan_mode": "75%"}))
    assert len(vane) == 1  # Once per start or mode change.
    await later(hass, freezer, seconds=4)
    await unit(hass, "off", **KNX)
    await unit(hass, "cool", **KNX)  # Someone starts it cooling: vane up.
    assert [call.data["option"] for call in vane] == ["5", "1"]
    log = central.runtime_data.command_log[-1]
    assert (log["service"], log["value"], log["reason"]) == (
        "select_option",
        "1",
        "vane_cool",
    )
    clear(mocks)


async def test_limits_are_set_on_the_devices_not_in_the_wizard(hass, freezer):
    mocks = await mock_climate(hass)
    central, zone = await office(hass, freezer, mocks, use_building_limits=False)
    runtime = central.runtime_data
    # The central wizard no longer asks for fixed limits.
    flow = await hass.config_entries.options.async_init(central.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "general"}
    )
    schema = flow["data_schema"].schema
    assert "heating_limit" not in schema and "eco_heating_limit" not in schema
    assert "heating_limit_entity" not in schema
    assert "cooling_limit_entity" not in schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"hysteresis": 0.3}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    central = hass.config_entries.async_get_entry(central.entry_id)
    assert central.options["building"]["heating_limit"] == 22  # Kept.
    runtime = central.runtime_data

    # Central device: the building's eco band, applied without a reload.
    eco_id = entity(hass, "number", f"{central.entry_id}_eco_heating_limit")
    assert float(hass.states.get(eco_id).state) == 19
    await hass.services.async_call(
        "number", "set_value", {"entity_id": eco_id, "value": 18}, blocking=True
    )
    await hass.async_block_till_done()
    assert central.options["building"]["eco_heating_limit"] == 18
    assert central.runtime_data is runtime  # No reload.
    assert runtime.building.settings.eco_heating_limit == 18
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": eco_id, "value": 26}, blocking=True
        )

    # Zone device (own limits): it got the central limits once, when the zone
    # was made; a later central change does not follow (0.13.3).
    zone_eco = entity(hass, "number", f"{zone.entry_id}_eco_heating_limit")
    assert float(hass.states.get(zone_eco).state) == 19
    assert hass.states.get(zone_eco).attributes["source"] == "zone"
    await hass.services.async_call(
        "number", "set_value", {"entity_id": zone_eco, "value": 20}, blocking=True
    )
    await hass.async_block_till_done()
    runtime.set_thermostat("west", preset="eco")
    snapshot = runtime.zone_snapshots["west"]
    assert (snapshot.heating_limit.value, snapshot.cooling_limit.value) == (20, 26)


async def test_window_entities_only_with_contacts(hass):
    """0.13.3: no window sensor or bypass switch without window contacts."""
    set_states(hass, "west", UNIT)
    hass.states.async_set(WINDOW, "off")
    await setup_central(hass)
    zone = await add_zone(hass, zone_options("west", UNIT))
    assert entity(hass, "sensor", f"{zone.entry_id}_window") is None
    assert entity(hass, "switch", f"{zone.entry_id}_window_bypass") is None
    assert entity(hass, "switch", f"{zone.entry_id}_room_regulation") is not None

    # Contacts added later: both appear; removed again: both disappear.
    hass.config_entries.async_update_entry(
        zone, options={**zone.options, "contact_entities": [WINDOW]}
    )
    await hass.async_block_till_done()
    assert entity(hass, "sensor", f"{zone.entry_id}_window") is not None
    assert entity(hass, "switch", f"{zone.entry_id}_window_bypass") is not None
    hass.config_entries.async_update_entry(
        zone, options={**zone.options, "contact_entities": []}
    )
    await hass.async_block_till_done()
    assert entity(hass, "sensor", f"{zone.entry_id}_window") is None
    assert entity(hass, "switch", f"{zone.entry_id}_window_bypass") is None


async def test_only_configured_entities(hass):
    """0.13.3: presence only with presence/motion; regulation only with units."""
    hass.states.async_set("sensor.store_temperature", "16", CELSIUS)
    hass.states.async_set("binary_sensor.building", "on")
    central = await setup_central(hass)
    zone = await add_zone(hass, zone_options("store"))  # No indoor units.
    for platform, key in (
        ("sensor", "presence"),
        ("sensor", "regulated_setpoint"),
        ("sensor", "auto_fan"),
        ("switch", "room_regulation"),
        ("switch", "fan_control"),
    ):
        assert entity(hass, platform, f"{zone.entry_id}_{key}") is None, key
    assert entity(hass, "sensor", f"{zone.entry_id}_safety") is not None

    # Building presence set centrally: the zones get their presence sensor.
    building = {
        **central.options["building"],
        "presence_entity": "binary_sensor.building",
    }
    hass.config_entries.async_update_entry(
        central, options={**central.options, "building": building}
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    presence = entity(hass, "sensor", f"{zone.entry_id}_presence")
    assert presence is not None
    assert hass.states.get(presence).state == "present"


async def test_zone_limits_only_without_central_limits(hass):
    """0.13.3: "use central climate limits" on: no limit fields on the room's
    device, and earlier own limits are ignored; off: the fields appear."""
    set_states(hass, "west", UNIT)
    central = await setup_central(hass)
    zone = await add_zone(hass, zone_options("west", UNIT, use_building_limits=False))
    runtime = central.runtime_data
    low = f"{zone.entry_id}_comfort_heating_limit"
    assert entity(hass, "number", low) is not None
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity(hass, "number", low), "value": 21},
        blocking=True,
    )
    assert runtime.zone_snapshots["west"].heating_limit.value == 21

    hass.config_entries.async_update_entry(
        zone, options={**zone.options, "use_building_limits": True}
    )
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert entity(hass, "number", low) is None
    assert runtime.zone_snapshots["west"].heating_limit.value == 22  # Central.
    assert runtime.thermostat("west").heating_limit is None  # Own ones cleared.

    # Off again: the central limits once, not the old own 21.
    zone = hass.config_entries.async_get_entry(zone.entry_id)
    hass.config_entries.async_update_entry(
        zone, options={**zone.options, "use_building_limits": False}
    )
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert float(hass.states.get(entity(hass, "number", low)).state) == 22
    assert runtime.thermostat("west").heating_limit == 22


async def test_fan_left_alone_with_the_fan_switch_off(hass, freezer):
    """0.13.5: "Automatic fan" off: no fan commands, no fan on the thermostat."""
    mocks = await mock_climate(hass)
    central, zone = await office(hass, freezer, mocks)
    runtime = central.runtime_data
    climate_id = entity(hass, "climate", f"{zone.entry_id}_thermostat")
    assert "fan_mode" not in hass.states.get(climate_id).attributes
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": entity(hass, "switch", f"{zone.entry_id}_fan_control")},
        blocking=True,
    )
    runtime.set_thermostat("west", regulation=True)
    await later(hass, freezer, minutes=4)
    await unit(hass, "heat", **KNX)
    await later(hass, freezer, minutes=6)
    assert not [call for call in calls(mocks) if call[0] == "set_fan_mode"]
    assert "fan_modes" not in hass.states.get(climate_id).attributes
    assert state(hass, zone, "auto_fan").attributes["in_use"] is False


async def test_automatic_fan_does_not_hunt_after_feedback_and_interval(hass, freezer):
    from tests.test_review_logic_fixes import standalone

    mocks = await mock_climate(hass)
    runtime = standalone(hass, temperature="20", fan="25%")
    try:

        def temperature(value):
            hass.states.async_set("sensor.review_temperature", str(value), CELSIUS)
            runtime.refresh()

        # Keep a heating cycle active, close to the first fan-speed boundary.
        temperature(21.79)
        runtime.set_thermostat("west", regulation=True)
        await hass.async_block_till_done()
        assert not mocks["set_fan_mode"]
        assert runtime.auto_fans["west"] == "25%"
        temperature(21.81)
        await hass.async_block_till_done()
        assert not mocks["set_fan_mode"]
        temperature(21.65)
        await hass.async_block_till_done()
        assert [c.data["fan_mode"] for c in mocks["set_fan_mode"]] == ["50%"]
        source = hass.states.get(UNIT)
        hass.states.async_set(UNIT, "heat", {**source.attributes, "fan_mode": "50%"})
        runtime.refresh()
        await hass.async_block_till_done()
        freezer.tick(301)
        for value in (21.79, 21.81, 21.79):
            temperature(value)
            await hass.async_block_till_done()
            assert runtime.auto_fans["west"] == "50%"
        assert len(mocks["set_fan_mode"]) == 1
        temperature(21.95)
        await hass.async_block_till_done()
        assert [c.data["fan_mode"] for c in mocks["set_fan_mode"]] == ["50%", "25%"]
    finally:
        runtime.close()
