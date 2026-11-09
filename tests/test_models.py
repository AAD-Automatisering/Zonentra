"""Topology validation and simulation invariants."""

from copy import deepcopy

import pytest

from custom_components.zonentra.decision import Observation, simulate
from custom_components.zonentra.models import build_model


def test_empty_building():
    building = build_model("building", "Office", {})
    assert building.entity_ids == frozenset()
    assert simulate(building, {}) == ()


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda t: t["zones"].append(deepcopy(t["zones"][0])), "duplicate_id"),
        (lambda t: t["equipment"][0].update(zone_id="missing"), "invalid_reference"),
        (lambda t: t["equipment"][0].update(group_id="missing"), "invalid_reference"),
        (
            lambda t: t["equipment"].append(dict(t["equipment"][0], id="second")),
            "duplicate_equipment_entity",
        ),
        (lambda t: t["zones"][0].update(id="../bad"), "invalid_id"),
        (
            lambda t: t["zones"][0].update(temperature_entity="climate.wrong"),
            "invalid_entity",
        ),
        (lambda t: t["equipment"][0].update(kind="floor_heating"), "invalid_group"),
    ],
)
def test_invalid_topology(topology, mutate, error):
    mutate(topology)
    with pytest.raises(ValueError, match=error):
        build_model("building", "Office", topology)


def test_group_conflict_only_simulated(topology):
    topology["equipment"].append(
        dict(topology["equipment"][0], id="indoor_2", entity_id="climate.indoor_2")
    )
    building = build_model("building", "Office", topology)
    decisions = simulate(
        building,
        {
            "climate.indoor_1": Observation("heat", "observed"),
            "climate.indoor_2": Observation("cool", "observed"),
        },
    )
    assert {d.reason for d in decisions} == {"observed_group_conflict"}
    assert {d.action for d in decisions} == {"hold"}


def test_unavailable_has_no_action(topology):
    decisions = simulate(build_model("building", "Office", topology), {})
    assert decisions[0].action == "hold"
    assert decisions[0].reason == "source_unavailable"


def test_cached_topology_indexes_follow_replaced_buildings(topology):
    from dataclasses import replace

    building = build_model("building", "Office", topology)
    first = building.equipment[0]
    assert building.equipment_ids_by_zone[(first.zone_id, first.kind)] == (first.id,)
    assert first.entity_id in building.entity_ids

    replacement = replace(first, id="replacement", entity_id="climate.replacement")
    updated = replace(building, equipment=(replacement,))
    assert updated.equipment_ids_by_zone[(first.zone_id, first.kind)] == (
        "replacement",
    )
    assert "climate.replacement" in updated.entity_ids
    assert first.entity_id not in updated.entity_ids
    assert building.equipment_ids_by_zone[(first.zone_id, first.kind)] == (first.id,)
