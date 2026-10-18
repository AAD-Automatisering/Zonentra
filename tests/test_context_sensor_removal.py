"""Obsolete indoor context sensors are removed without affecting temperature."""

from copy import deepcopy

from custom_components.zonentra.migration import migrate_options
from custom_components.zonentra.models import build_model
from tests.test_entry_layout import add_zone, set_states, setup_central, zone_options


def test_legacy_context_links_are_removed_idempotently():
    room = {
        "id": "west",
        "name": "West",
        "temperature_entity": "sensor.west_temperature",
        "humidity_entity": "sensor.humidity",
        "co2_entity": "sensor.co2",
    }
    options = {"zones": [room]}
    original = deepcopy(options)
    migrated = migrate_options(options)
    assert options == original
    assert migrate_options(migrated) == migrated
    assert "humidity_entity" not in migrated["zones"][0]
    assert "co2_entity" not in migrated["zones"][0]
    assert migrated["zones"][0]["temperature_entity"] == room["temperature_entity"]
    model = build_model("building", "Building", options)
    assert "sensor.humidity" not in model.entity_ids
    assert "sensor.co2" not in model.entity_ids
    assert room["temperature_entity"] in model.entity_ids


async def test_existing_room_stops_tracking_context_sensors(hass):
    hass.states.async_set("sensor.humidity", "45", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.co2", "900", {"unit_of_measurement": "ppm"})
    set_states(hass, "west", "climate.west_ac")
    central = await setup_central(hass)
    room = await add_zone(
        hass,
        zone_options(
            "west",
            "climate.west_ac",
            humidity_entity="sensor.humidity",
            co2_entity="sensor.co2",
        ),
    )
    for key, entity in (
        ("humidity_entity", "sensor.humidity"),
        ("co2_entity", "sensor.co2"),
    ):
        assert key not in room.options
        assert entity not in central.runtime_data.building.entity_ids
        assert entity not in central.runtime_data.observations
        assert hass.states.get(entity) is not None
    assert central.runtime_data.zone_snapshots["west"].temperature.value == 21
