"""Central policy, local opt-outs, shared weather and safe old-config migration."""

from copy import deepcopy
from datetime import timedelta

from custom_components.zonentra.demand import calculate_demand, collect_zone
from custom_components.zonentra.migration import migrate_options
from custom_components.zonentra.models import build_model
from custom_components.zonentra.observations import SourceState
from tests.test_intelligence import NOW


def central_topology(topology):
    topology["building"] = {
        "heating_limit": 21,
        "cooling_limit": 24,
        "hysteresis": 0.4,
        "max_age_seconds": 120,
        "outdoor_temperature_entity": "sensor.outside",
        "outdoor_max_age_seconds": 30,
    }
    topology["zones"][0].update(use_building_limits=True, use_building_parameters=True)
    return topology


def test_effective_settings_are_resolved_without_mutating_storage(topology):
    topology = central_topology(topology)
    topology["zones"][0].update(
        heating_limit=18, cooling_limit=28, hysteresis=0.8, max_age_seconds=900
    )
    original = deepcopy(topology)
    building = build_model("building", "Office", topology)
    zone = building.zones[0]
    assert (
        zone.heating_limit,
        zone.cooling_limit,
        zone.hysteresis,
        zone.max_age_seconds,
    ) == (21, 24, 0.4, 120)
    assert topology == original
    topology["zones"][0].update(
        use_building_limits=False, use_building_parameters=False
    )
    own = build_model("building", "Office", topology).zones[0]
    assert (
        own.heating_limit,
        own.cooling_limit,
        own.hysteresis,
        own.max_age_seconds,
    ) == (18, 28, 0.8, 900)


def test_independent_overrides_and_central_updates(topology):
    topology = central_topology(topology)
    topology["zones"].append(
        {
            "id": "local",
            "name": "Local",
            "heating_limit": 18,
            "cooling_limit": 28,
            "use_building_limits": False,
            "use_building_parameters": True,
        }
    )
    topology["building"].update(heating_limit=22, cooling_limit=26, hysteresis=0.5)
    building = build_model("building", "Office", topology)
    assert [zone.heating_limit for zone in building.zones] == [22, 18]
    assert [zone.hysteresis for zone in building.zones] == [0.5, 0.5]


def test_shared_weather_uses_building_age_for_all_rooms(topology):
    topology = central_topology(topology)
    topology["zones"].append({"id": "other", "name": "Other", "max_age_seconds": 5})
    building = build_model("building", "Office", topology)
    sources = {"sensor.outside": SourceState("12", "°C", NOW - timedelta(seconds=10))}
    snapshots = [collect_zone(building, zone, sources, NOW) for zone in building.zones]
    assert [s.outdoor_temperature.value for s in snapshots] == [12, 12]
    assert [s.outdoor_temperature.quality for s in snapshots] == ["valid", "valid"]
    assert list(building.entity_ids).count("sensor.outside") == 1
    snapshots = [
        collect_zone(building, zone, sources, NOW + timedelta(seconds=21))
        for zone in building.zones
    ]
    assert all(s.outdoor_temperature.quality == "stale" for s in snapshots)


def test_removed_central_limit_entity_uses_configured_static_limits(topology):
    topology = central_topology(topology)
    topology["building"].update(heating_limit_entity="number.central_heat")
    topology["zones"][0].update(heating_limit=18, cooling_limit=28)
    building = build_model("building", "Office", topology)
    zone = building.zones[0]
    sources = {
        "sensor.office_temperature": SourceState("20", "°C", NOW),
        "number.central_heat": SourceState("unavailable", "°C", NOW),
    }
    snapshot = collect_zone(building, zone, sources, NOW)
    assert calculate_demand(zone, snapshot).demand == "heating_demand"
    assert snapshot.heating_limit.value == 21
    assert "number.central_heat" not in building.entity_ids


def test_migration_moves_unique_weather_and_keeps_room_policy(topology):
    topology["zones"][0].update(
        outdoor_temperature_entity="sensor.outside",
        heating_limit=19,
        cooling_limit=25,
        hysteresis=0.4,
        max_age_seconds=600,
    )
    original = deepcopy(topology)
    migrated = migrate_options(topology)
    assert migrated["building"] == {
        "outdoor_temperature_entity": "sensor.outside",
        "outdoor_max_age_seconds": 600,
    }
    assert "outdoor_temperature_entity" not in migrated["zones"][0]
    assert not migrated["zones"][0]["use_building_limits"]
    assert not migrated["zones"][0]["use_building_parameters"]
    assert build_model("building", "Office", migrated).zones[0].heating_limit == 19
    assert migrated == migrate_options(migrated)
    assert topology == original


def test_conflicting_old_weather_is_not_guessed(topology):
    topology["zones"][0]["outdoor_temperature_entity"] = "sensor.north"
    topology["zones"].append(
        {"id": "other", "name": "Other", "outdoor_temperature_entity": "sensor.south"}
    )
    migrated = migrate_options(topology)
    building = build_model("building", "Office", migrated)
    assert building.settings.outdoor_temperature_entity is None
    assert building.settings.outdoor_temperature_candidates == (
        "sensor.north",
        "sensor.south",
    )
    assert migrated == migrate_options(migrated)
    assert "sensor.north" not in building.entity_ids


def test_missing_central_policy_does_not_reactivate_dormant_room_limits(topology):
    topology["zones"][0].update(
        use_building_limits=True, heating_limit=19, cooling_limit=25
    )
    building = build_model("building", "Office", topology)
    assert building.zones[0].heating_limit is None
    assert building.zones[0].cooling_limit is None
