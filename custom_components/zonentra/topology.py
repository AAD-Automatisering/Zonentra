"""Pure assembly of the 0.6 entry layout into the existing topology format.

Like Versatile Thermostat, Zonentra 0.6 uses one *central configuration*
entry (building settings and outdoor unit groups) and one entry per *zone*
(sensors, indoor units, group and local overrides). The calculation core is
unchanged: assemble() turns those entries back into the single topology dict
that models.build_model() already validates.
"""

from typing import Any, Iterable

ENTRY_TYPE = "entry_type"
CENTRAL = "central"
ZONE = "zone"
CENTRAL_UNIQUE_ID = "central"

# Zone-entry list fields that become Equipment records, with their kind.
EQUIPMENT_FIELDS = (
    ("indoor_units", "hvac"),
    ("floor_heating_entities", "floor_heating"),
    ("shading_entities", "shading"),
)
PREFIX = {
    "hvac": "hvac",
    "floor_heating": "floor",
    "shading": "shade",
}


def equipment_id(kind: str, entity_id: str) -> str:
    """Stable, valid ID derived from the linked entity (max. 64 characters)."""
    object_id = entity_id.split(".", 1)[-1]
    return f"{PREFIX[kind]}_{object_id}"[:64]


def zone_equipment(zone: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": equipment_id(kind, entity_id),
            "name": entity_id.split(".", 1)[-1],
            "kind": kind,
            "entity_id": entity_id,
            "zone_id": zone["id"],
        }
        for field, kind in EQUIPMENT_FIELDS
        for entity_id in zone.get(field, ())
    ]


def zone_record(zone: dict[str, Any]) -> dict[str, Any]:
    fields = {field for field, _ in EQUIPMENT_FIELDS}
    return {key: value for key, value in zone.items() if key not in fields}


def assemble(
    central: dict[str, Any], zones: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Combine central options and zone-entry options into one topology."""
    zones = [dict(zone) for zone in zones]
    linked: dict[str, str] = {}
    for zone in zones:
        for field, _ in EQUIPMENT_FIELDS:
            for entity_id in zone.get(field, ()):
                if entity_id in linked:
                    # One indoor unit (or other device) belongs to one zone.
                    raise ValueError("duplicate_equipment_entity")
                linked[entity_id] = zone["id"]
    return {
        "building": dict(central.get("building", {})),
        "groups": [dict(group) for group in central.get("groups", [])],
        "zones": [zone_record(zone) for zone in zones],
        "equipment": [item for zone in zones for item in zone_equipment(zone)],
    }
