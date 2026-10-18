"""Pure climate intelligence tests: no HomeAssistant instance is needed."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.zonentra.demand import calculate_demand, collect_zone
from custom_components.zonentra.engine import GroupMemory, evaluate
from custom_components.zonentra.models import (
    Building,
    Equipment,
    EquipmentGroup,
    Zone,
    build_model,
)
from custom_components.zonentra.observations import SourceState, read

NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)


def scenario(temperatures=(20,), mode="off", weights=None, **group_parameters):
    zones = tuple(
        Zone(
            str(i),
            f"Room {i}",
            temperature_entity=f"sensor.room_{i}",
            heating_limit=21,
            cooling_limit=24,
            weight=(weights or [1] * len(temperatures))[i],
        )
        for i in range(len(temperatures))
    )
    group = EquipmentGroup(
        "group", "Outdoor", mode_entity="sensor.mode", **group_parameters
    )
    equipment = tuple(
        Equipment(f"unit_{i}", "Unit", "hvac", f"climate.unit_{i}", zone.id, "group")
        for i, zone in enumerate(zones)
    )
    building = Building("building", "Office", zones, (group,), equipment)
    sources = {
        zone.temperature_entity: SourceState(str(value), "°C", NOW)
        for zone, value in zip(zones, temperatures, strict=True)
    }
    sources.update(
        {item.entity_id: SourceState("off", None, NOW) for item in equipment}
    )
    if mode is not None:
        sources["sensor.mode"] = SourceState(mode, None, NOW)
    return building, sources


def demands_for(building, sources):
    return {
        zone.id: calculate_demand(zone, collect_zone(building, zone, sources, NOW))
        for zone in building.zones
    }


def decision_for(building, sources, elapsed=3600, now=NOW):
    memories = {
        group.id: GroupMemory("off", now - timedelta(seconds=elapsed))
        for group in building.groups
    }
    return evaluate(building, demands_for(building, sources), sources, memories, now)[0]


@pytest.mark.parametrize(
    ("temperature", "expected", "heating", "cooling"),
    [
        (20, "heating_demand", 1, 0),
        (25, "cooling_demand", 0, 1),
        (23, "neutral", 0, 0),
    ],
)
def test_temperature_demand(temperature, expected, heating, cooling):
    building, sources = scenario((temperature,))
    demand = demands_for(building, sources)["0"]
    assert (demand.demand, demand.heating_score, demand.cooling_score) == (
        expected,
        heating,
        cooling,
    )


@pytest.mark.parametrize(
    ("temperature", "previous", "expected"),
    [
        (20.8, "neutral", "neutral"),
        (21.2, "heating_demand", "heating_demand"),
        (21.3, "heating_demand", "neutral"),
        (24.2, "neutral", "neutral"),
        (23.8, "cooling_demand", "cooling_demand"),
        (23.7, "cooling_demand", "neutral"),
    ],
)
def test_zone_hysteresis(temperature, previous, expected):
    building, sources = scenario((temperature,))
    zone = building.zones[0]
    assert (
        calculate_demand(
            zone, collect_zone(building, zone, sources, NOW), previous
        ).demand
        == expected
    )


@pytest.mark.parametrize(
    ("source", "quality"),
    [
        (None, "missing"),
        (SourceState("unknown", "°C", NOW), "unknown"),
        (SourceState("unavailable", "°C", NOW), "unavailable"),
        (SourceState("nan", "°C", NOW), "unreliable"),
        (SourceState("inf", "°C", NOW), "unreliable"),
        (SourceState("oops", "°C", NOW), "unreliable"),
        (SourceState("99", "°C", NOW), "unreliable"),
        (SourceState("20", None, NOW), "unreliable"),
        (SourceState("20", "K", NOW), "unreliable"),
        (SourceState("20", "°C", None), "unreliable"),
        (SourceState("20", "°C", NOW + timedelta(minutes=1)), "unreliable"),
        (SourceState("20", "°C", NOW, trustworthy=False), "unreliable"),
        (SourceState("20", "°C", NOW - timedelta(seconds=1800)), "stale"),
    ],
)
def test_sensor_health(source, quality):
    assert read(source, "temperature", NOW, 1800).quality == quality
    building, sources = scenario()
    # Since 0.3.1 report age is only enforced with explicit update_based validation.
    zone = replace(building.zones[0], sensor_validation="update_based")
    building = replace(building, zones=(zone,))
    if source:
        sources["sensor.room_0"] = source
    else:
        sources.pop("sensor.room_0")
    demand = demands_for(building, sources)["0"]
    assert demand.demand == "unknown"
    assert demand.heating_score is None and demand.cooling_score is None
    assert demand.sensor_health == quality


def test_fahrenheit_and_optional_context():
    building, sources = scenario()
    sources["sensor.room_0"] = SourceState("68", "°F", NOW)
    zone = replace(
        building.zones[0],
        contact_entities=("binary_sensor.window",),
    )
    building = replace(building, zones=(zone,))
    snapshot = collect_zone(building, zone, sources, NOW)
    assert snapshot.temperature.value == 20
    assert snapshot.occupancy.value is None and snapshot.occupancy.quality == "missing"
    assert snapshot.contacts[0].quality == "missing"
    assert calculate_demand(zone, snapshot).demand == "heating_demand"


def test_static_limits_and_invalid_band():
    building, sources = scenario()
    zone = replace(building.zones[0], heating_limit=21, cooling_limit=24)
    assert (
        calculate_demand(zone, collect_zone(building, zone, sources, NOW)).demand
        == "heating_demand"
    )
    zone = replace(zone, cooling_limit=21)
    assert (
        calculate_demand(zone, collect_zone(building, zone, sources, NOW)).reason
        == "invalid_limits"
    )
    zone = replace(zone, cooling_limit=None)
    assert (
        calculate_demand(zone, collect_zone(building, zone, sources, NOW)).demand
        == "unknown"
    )


def test_weighted_conflicting_demand_example():
    building, sources = scenario((20, 23, 24.8), mode="off")
    decision = decision_for(building, sources)
    assert decision.heating_score == 1
    assert decision.cooling_score == 0.8
    assert decision.proposed_mode == "unknown"  # Difference 0.2 < default deadband 0.5.
    assert decision.blocked_reason == "balanced_demand"
    building, sources = scenario((20, 23, 24.8), mode="off", weights=(2, 1, 1))
    decision = decision_for(building, sources)
    assert decision.proposed_mode == "heat"
    assert decision.mode_change_proposed


def test_mode_hysteresis_retains_current_mode():
    building, sources = scenario((20, 24.8), mode="cool")
    decision = decision_for(building, sources)
    assert decision.desired_mode == decision.proposed_mode == "cool"
    assert decision.reason == "score_deadband"
    assert not decision.mode_change_proposed


def test_small_unopposed_demand_is_not_blocked_by_group_scores():
    building, sources = scenario((20.65,))
    decision = decision_for(building, sources)
    assert decision.heating_score == 0.35
    assert decision.desired_mode == decision.proposed_mode == "heat"
    assert decision.reason == "heating_only_demand"


@pytest.mark.parametrize("mode", [None, "unknown", "unavailable", "auto", "heat_cool"])
def test_unknown_actual_mode_is_never_off(mode):
    building, sources = scenario(mode=mode)
    decision = decision_for(building, sources)
    assert decision.current_mode == decision.proposed_mode == "unknown"
    assert decision.desired_mode == "heat"
    assert decision.blocked_reason == "current_mode_unknown"
    assert not decision.mode_change_proposed


def test_stale_mode_blocks_switch():
    building, sources = scenario()
    sources["sensor.mode"] = SourceState("off", None, NOW - timedelta(hours=1))
    decision = decision_for(building, sources)
    assert decision.current_mode == "unknown" and decision.mode_quality == "stale"


def test_incomplete_group_cannot_propose_new_mode():
    building, sources = scenario((20, 25), mode="heat")
    sources.pop("sensor.room_1")
    decision = decision_for(building, sources)
    assert decision.proposed_mode == decision.desired_mode == "unknown"
    assert decision.reliable_zones == 1
    assert decision.blocked_reason == "insufficient_data"


def test_group_does_not_double_count_multiple_indoor_units():
    building, sources = scenario()
    unit = replace(building.equipment[0], id="extra", entity_id="climate.extra")
    building = replace(building, equipment=(*building.equipment, unit))
    sources["climate.extra"] = SourceState("off", None, NOW)
    assert decision_for(building, sources).heating_score == 1


def test_two_independent_groups():
    building, sources = scenario((20, 25), min_switch_seconds=0)
    other = replace(building.groups[0], id="other", mode_entity="sensor.other_mode")
    sources["sensor.other_mode"] = SourceState("off", None, NOW)
    building = replace(
        building,
        groups=(*building.groups, other),
        equipment=(
            building.equipment[0],
            replace(building.equipment[1], group_id="other"),
        ),
    )
    decisions = evaluate(
        building,
        demands_for(building, sources),
        sources,
        {group.id: GroupMemory("off", NOW) for group in building.groups},
        NOW,
    )
    assert [decision.proposed_mode for decision in decisions] == ["heat", "cool"]


@pytest.mark.parametrize(("elapsed", "blocked"), [(0, True), (899, True), (900, False)])
def test_minimum_switch_interval(elapsed, blocked):
    building, sources = scenario(mode="cool")
    decision = decision_for(building, sources, elapsed)
    assert decision.desired_mode == "heat"
    assert decision.proposed_mode == ("cool" if blocked else "heat")
    assert decision.blocked_reason == ("minimum_switch_interval" if blocked else None)
    assert decision.mode_change_proposed == (not blocked)


def test_missing_equipment_and_observed_conflict():
    building, sources = scenario((20, 20), mode="heat")
    sources.pop("climate.unit_1")
    assert decision_for(building, sources).blocked_reason == "insufficient_data"
    sources["climate.unit_0"] = SourceState("heat", None, NOW)
    sources["climate.unit_1"] = SourceState("cool", None, NOW)
    assert decision_for(building, sources).blocked_reason == "observed_group_conflict"


def test_existing_01_topology_and_new_zone_group(topology):
    old = build_model("building", "Office", topology)
    assert old.zones[0].heating_limit is None
    assert old.groups[0].min_switch_seconds == 900
    assert old.equipment[0].group_id == "outdoor_1"
    topology["zones"][0].update(
        group_id="outdoor_1", heating_limit=21, cooling_limit=24
    )
    topology["equipment"][0].pop("group_id")
    assert (
        build_model("building", "Office", topology).equipment[0].group_id == "outdoor_1"
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"weight": 0},
        {"weight": float("nan")},
        {"max_age_seconds": -1},
        {"heating_limit": 24, "cooling_limit": 21},
        {"heating_limit": 21},
    ],
)
def test_configuration_validation(topology, parameters):
    topology["zones"][0].update(parameters)
    with pytest.raises(ValueError):
        build_model("building", "Office", topology)


def test_restored_equipment_is_insufficient_data():
    building, sources = scenario()
    sources["climate.unit_0"] = SourceState("off", None, NOW, trustworthy=False)
    assert decision_for(building, sources).blocked_reason == "insufficient_data"


@pytest.mark.parametrize(
    ("kind", "value", "unit", "quality"),
    [
        ("occupancy", "unavailable", None, "unavailable"),
    ],
)
def test_optional_reading_validation(kind, value, unit, quality):
    assert read(SourceState(value, unit, NOW), kind, NOW).quality == quality
