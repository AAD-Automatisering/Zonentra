"""Remove old ventilation links without changing other equipment or devices."""

from copy import deepcopy

from custom_components.zonentra.migration import migrate_options
from custom_components.zonentra.models import build_model
from tests.test_entry_layout import add_zone, set_states, setup_central, zone_options


def test_old_ventilation_equipment_is_removed_before_model_validation():
    options = {
        "zones": [
            {
                "id": "west",
                "name": "West",
                "temperature_entity": "sensor.west",
                "ventilation_entities": ["fan.extract"],
            }
        ],
        "equipment": [
            {
                "id": "extract",
                "kind": "ventilation",
                "name": "Extract",
                "entity_id": "fan.extract",
                "zone_id": "west",
            },
            {
                "id": "ac",
                "kind": "hvac",
                "name": "AC",
                "entity_id": "climate.ac",
                "zone_id": "west",
            },
        ],
    }
    original = deepcopy(options)
    migrated = migrate_options(options)
    assert options == original
    assert migrated == migrate_options(migrated)
    assert "ventilation_entities" not in migrated["zones"][0]
    assert migrated["equipment"] == [original["equipment"][1]]
    model = build_model("building", "Building", options)
    assert "fan.extract" not in model.entity_ids
    assert {
        "climate.ac",
        "sensor.west",
    } <= model.entity_ids


async def test_existing_rooms_drop_shared_ventilation_links_on_setup(hass):
    hass.states.async_set("fan.extract", "on", {"percentage": 50})
    set_states(hass, "west", "climate.west_ac")
    set_states(hass, "east", "climate.east_ac")
    central = await setup_central(hass)
    for name in ("west", "east"):
        room = await add_zone(
            hass,
            zone_options(
                name,
                f"climate.{name}_ac",
                ventilation_entities=["fan.extract"],
            ),
        )
        assert "ventilation_entities" not in room.options
    runtime = central.runtime_data
    assert "fan.extract" not in runtime.building.entity_ids
    assert "fan.extract" not in runtime.observations
    assert {item.entity_id for item in runtime.building.equipment} == {
        "climate.west_ac",
        "climate.east_ac",
    }
    assert hass.states.get("fan.extract").state == "on"
    assert hass.states.get("fan.extract").attributes["percentage"] == 50
