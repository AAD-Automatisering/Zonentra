"""Pure, idempotent configuration normalization for central building settings."""

from copy import deepcopy

LIMIT_FIELDS = (
    "heating_limit",
    "cooling_limit",
)
REMOVED_LIMIT_ENTITIES = (
    "heating_limit_entity",
    "cooling_limit_entity",
)
# Eco and away bands (0.8): central, optionally different per zone (0.10).
BAND_FIELDS = (
    "eco_heating_limit",
    "eco_cooling_limit",
    "away_heating_limit",
    "away_cooling_limit",
)
# Static limits that the central configuration sets on its device (0.10),
# not in the wizard: comfort, eco and away.
DEVICE_LIMITS = ("heating_limit", "cooling_limit", *BAND_FIELDS)
PARAMETER_FIELDS = ("hysteresis", "max_age_seconds", "sensor_validation")
PRECONDITIONING_FIELDS = (
    "preconditioning_max_minutes",
    "preconditioning_buffer_minutes",
    "preconditioning_heating_rate",
    "preconditioning_cooling_rate",
)
FLOOR_FIELDS = ("floor_heating_limit",)
NEW_BUILDING_DEFAULTS = {
    "heating_limit": 21.0,
    "cooling_limit": 24.0,
    "hysteresis": 0.3,
    "max_age_seconds": 1800.0,
    "sensor_validation": "state_based",
}


def migrate_options(options: dict) -> dict:
    """Keep existing room policy explicit; never guess a shared weather source."""
    result = deepcopy(options)
    # Remove obsolete observation-only links and legacy ventilation equipment
    # before validation, without touching the physical entities.
    for field in ("ventilation_entities", "humidity_entity", "co2_entity"):
        result.pop(field, None)
    if isinstance(result.get("equipment"), list):
        result["equipment"] = [
            item
            for item in result["equipment"]
            if not isinstance(item, dict) or item.get("kind") != "ventilation"
        ]
    _remove_limit_entities(result)
    if "id" in result:
        _room_policy(result)
    settings = result.get("building", {})
    if _remove_limit_entities(settings):
        for key in LIMIT_FIELDS:
            if settings.get(key) is None:
                settings[key] = NEW_BUILDING_DEFAULTS[key]
    outside = set(settings.get("outdoor_temperature_candidates", []))
    ages = []
    for zone in result.get("zones", []):
        for field in ("ventilation_entities", "humidity_entity", "co2_entity"):
            zone.pop(field, None)
        _remove_limit_entities(zone)
        _room_policy(zone)
        zone.pop("controller_entity", None)
        entity = zone.pop("outdoor_temperature_entity", None)
        if entity:
            outside.add(entity)
            ages.append(zone.get("max_age_seconds", 1800))
        zone.setdefault(
            "use_building_limits",
            not any(zone.get(key) is not None for key in LIMIT_FIELDS),
        )
        zone.setdefault(
            "use_building_parameters", not any(key in zone for key in PARAMETER_FIELDS)
        )
    if outside and not settings.get("outdoor_temperature_entity"):
        if len(outside) == 1:
            settings["outdoor_temperature_entity"] = next(iter(outside))
            settings.setdefault(
                "outdoor_max_age_seconds",
                min((age for age in ages if age > 0), default=0 if ages else 1800),
            )
            settings.pop("outdoor_temperature_candidates", None)
        else:
            settings["outdoor_temperature_candidates"] = sorted(outside)
    elif settings.get("outdoor_temperature_entity"):
        settings.pop("outdoor_temperature_candidates", None)
    if settings or "building" in result:
        result["building"] = settings
    return result


def _room_policy(zone: dict) -> None:
    """Preparation inherits by default; preserve explicit room choices."""
    zone.setdefault(
        "use_building_floor_heating", not any(key in zone for key in FLOOR_FIELDS)
    )
    zone.setdefault("use_building_preconditioning", True)


def _remove_limit_entities(record: dict) -> bool:
    """Drop obsolete links; incomplete local bands inherit central limits."""
    removed = any(key in record for key in REMOVED_LIMIT_ENTITIES)
    for key in REMOVED_LIMIT_ENTITIES:
        record.pop(key, None)
    if removed and any(record.get(key) is None for key in LIMIT_FIELDS):
        for key in LIMIT_FIELDS:
            record.pop(key, None)
    return removed
