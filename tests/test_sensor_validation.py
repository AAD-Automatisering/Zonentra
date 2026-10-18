"""0.3.1 regression: a stable, valid temperature is not stale by default.

Pure tests (no HomeAssistant instance). Practical case: Open Office reads
20.7 °C, unchanged for 44 minutes; heating limit 22 °C, cooling limit 24 °C,
hysteresis 0.3 °C, one outdoor unit group.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.zonentra.demand import calculate_demand, collect_zone
from custom_components.zonentra.engine import GroupMemory, evaluate
from custom_components.zonentra.migration import migrate_options
from custom_components.zonentra.models import build_model
from custom_components.zonentra.observations import SourceState, describe, read

NOW = datetime(2026, 10, 12, 14, tzinfo=UTC)
TEMPERATURE = "sensor.open_office_temperature"

TOPOLOGY = {
    "zones": [
        {
            "id": "open_office",
            "name": "Open Office",
            "temperature_entity": TEMPERATURE,
            "heating_limit": 22,
            "cooling_limit": 24,
            "hysteresis": 0.3,
            "max_age_seconds": 1800,
            "group_id": "buitenunit",
        }
    ],
    "groups": [
        {"id": "buitenunit", "name": "Buitenunit", "mode_entity": "sensor.mode"}
    ],
    "equipment": [
        {
            "id": "binnenunit",
            "name": "Binnenunit",
            "kind": "hvac",
            "entity_id": "climate.binnenunit",
            "zone_id": "open_office",
        }
    ],
}


def office(**zone_parameters):
    topology = deepcopy(TOPOLOGY)
    topology["zones"][0].update(zone_parameters)
    return build_model("building", "Building", topology)


def temperature(value="20.7", changed=0.0, reported=None, unit="°C", **kwargs):
    """A source unchanged for `changed` minutes, last reported `reported` minutes ago."""
    reported = changed if reported is None else reported
    return SourceState(
        value,
        unit,
        NOW - timedelta(minutes=reported),
        changed_at=NOW - timedelta(minutes=changed),
        **kwargs,
    )


def sources_with(source):
    sources = {
        "climate.binnenunit": SourceState("heat", None, NOW),
        "sensor.mode": SourceState("heat", None, NOW),
    }
    if source is not None:
        sources[TEMPERATURE] = source
    return sources


def evaluate_office(building, sources, previous="unknown", now=NOW):
    zone = building.zones[0]
    snapshot = collect_zone(building, zone, sources, now)
    demand = calculate_demand(zone, snapshot, previous)
    memories = {"buitenunit": GroupMemory("heat", now - timedelta(hours=1))}
    decision = evaluate(building, {zone.id: demand}, sources, memories, now)[0]
    return snapshot, demand, decision


def assert_usable(snapshot, demand, decision):
    assert snapshot.temperature.quality == "valid"
    assert snapshot.temperature.value == pytest.approx(20.7)
    assert demand.sensor_health == "valid"
    assert demand.demand == "heating_demand"
    assert demand.reason == "below_heating_limit"
    assert demand.heating_score == pytest.approx(1.3)
    assert demand.cooling_score == 0
    assert decision.member_zone_ids == ("open_office",)
    assert decision.reliable_zones == 1
    assert decision.heating_score == pytest.approx(1.3)
    assert decision.desired_mode == "heat"
    assert decision.blocked_reason is None


def assert_unusable(demand, decision, health):
    assert demand.demand == "unknown"
    assert demand.reason == "insufficient_data"
    assert demand.sensor_health == health
    assert demand.heating_score is None and demand.cooling_score is None
    assert decision.reliable_zones == 0
    assert decision.desired_mode == "unknown"
    assert decision.blocked_reason == "insufficient_data"


# Scenarios 1-3 and 7: state-based (default) accepts a stable valid temperature.
@pytest.mark.parametrize("minutes", [5, 44, 24 * 60])
def test_unchanged_valid_temperature_remains_usable(minutes):
    building = office()
    assert building.zones[0].sensor_validation == "state_based"
    snapshot, demand, decision = evaluate_office(
        building, sources_with(temperature(changed=minutes))
    )
    assert_usable(snapshot, demand, decision)
    info = describe(snapshot.temperature, NOW)
    assert info["health"] == "valid"
    assert info["strategy"] == "state_based"
    assert info["seconds_since_change"] == minutes * 60
    assert info["seconds_since_report"] == minutes * 60
    assert info["max_update_age_seconds"] is None
    # A stable value is accepted, but never presented as proven fresh.
    assert info["freshness_verified"] is False
    assert info["reason"] is None


def test_state_based_without_any_recent_change_or_report():
    snapshot, demand, decision = evaluate_office(
        office(max_age_seconds=60), sources_with(temperature(changed=3 * 24 * 60))
    )
    assert_usable(snapshot, demand, decision)
    assert not snapshot.temperature.freshness_verified


# Scenarios 4-6: availability and plausibility protections remain intact.
@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_unknown_or_unavailable_source(state):
    _, demand, decision = evaluate_office(
        office(), sources_with(temperature(state, changed=1))
    )
    assert_unusable(demand, decision, state)
    reading = read(temperature(state), "temperature", NOW)
    assert reading.reason == f"source_{state}"


@pytest.mark.parametrize(
    ("value", "unit", "reason"),
    [
        ("abc", "°C", "not_numeric"),
        ("nan", "°C", "not_finite"),
        ("inf", "°C", "not_finite"),
        ("120", "°C", "out_of_range"),
        ("-80", "°C", "out_of_range"),
        ("20.7", None, "unsupported_unit"),
        ("20.7", "K", "unsupported_unit"),
    ],
)
def test_invalid_numeric_value(value, unit, reason):
    snapshot, demand, decision = evaluate_office(
        office(), sources_with(temperature(value, unit=unit))
    )
    assert_unusable(demand, decision, "unreliable")
    assert snapshot.temperature.reason == reason
    assert snapshot.temperature.value is None


def test_untrusted_restored_value_is_not_usable():
    snapshot, demand, decision = evaluate_office(
        office(), sources_with(temperature(trustworthy=False))
    )
    assert_unusable(demand, decision, "unreliable")
    assert snapshot.temperature.reason == "untrusted_source"


def test_missing_and_unconfigured_sources_are_distinguished():
    snapshot, demand, decision = evaluate_office(office(), sources_with(None))
    assert_unusable(demand, decision, "missing")
    assert snapshot.temperature.reason == "source_missing"


# Scenarios 8-10: optional update-based validation for periodic sources.
def test_update_based_expired_timeout_is_stale():
    building = office(sensor_validation="update_based", max_age_seconds=1800)
    snapshot, demand, decision = evaluate_office(
        building, sources_with(temperature(changed=44))
    )
    assert_unusable(demand, decision, "stale")
    info = describe(snapshot.temperature, NOW)
    assert info["strategy"] == "update_based"
    assert info["reason"] == "no_report_within_max_age"
    assert info["max_update_age_seconds"] == 1800
    assert info["freshness_verified"] is False


def test_update_based_recent_report_with_unchanged_value():
    building = office(sensor_validation="update_based", max_age_seconds=1800)
    snapshot, demand, decision = evaluate_office(
        building, sources_with(temperature(changed=44, reported=1))
    )
    assert_usable(snapshot, demand, decision)
    info = describe(snapshot.temperature, NOW)
    assert info["seconds_since_change"] == 44 * 60
    assert info["seconds_since_report"] == 60
    assert info["freshness_verified"] is True


def test_update_based_recovers_from_stale_to_valid():
    building = office(sensor_validation="update_based", max_age_seconds=1800)
    _, stale, decision = evaluate_office(
        building, sources_with(temperature(changed=31))
    )
    assert_unusable(stale, decision, "stale")
    # Same value, new report: recovers without a value change.
    snapshot, demand, decision = evaluate_office(
        building,
        sources_with(temperature(changed=31, reported=0)),
        previous=stale.demand,
    )
    assert_usable(snapshot, demand, decision)


def test_removed_limit_entities_do_not_replace_static_limits():
    building = office(
        sensor_validation="update_based",
        max_age_seconds=60,
        heating_limit=22,
        cooling_limit=24,
        heating_limit_entity="input_number.heat",
        cooling_limit_entity="input_number.cool",
    )
    sources = sources_with(temperature(changed=30, reported=0))
    week = NOW - timedelta(days=7)
    sources["input_number.heat"] = SourceState("22", "°C", week, changed_at=week)
    sources["input_number.cool"] = SourceState("24", "°C", week, changed_at=week)
    assert_usable(*evaluate_office(building, sources))


# Scenarios 13-14: demand and reliable_zones recover once the value is usable.
@pytest.mark.parametrize(
    "broken",
    [
        temperature("unavailable"),
        temperature("unknown"),
        temperature("abc"),
        None,
    ],
)
def test_demand_and_reliable_zones_recover(broken):
    building = office()
    _, demand, decision = evaluate_office(building, sources_with(broken))
    assert demand.demand == "unknown" and decision.reliable_zones == 0
    snapshot, demand, decision = evaluate_office(
        building, sources_with(temperature(changed=0)), previous=demand.demand
    )
    assert_usable(snapshot, demand, decision)


def test_recovered_demand_keeps_hysteresis():
    building = office()
    _, demand, _ = evaluate_office(building, sources_with(temperature("21.8")))
    assert demand.demand == "neutral"  # 22 - 0.3 <= 21.8 without previous demand.
    _, demand, _ = evaluate_office(
        building, sources_with(temperature("21.8")), previous="heating_demand"
    )
    assert demand.demand == "heating_demand"


# Scenario 11 (pure part): after a restart last_changed and last_reported reset.
def test_restart_resets_timestamps_without_staleness():
    restarted = SourceState(
        "20.7",
        "°C",
        NOW - timedelta(seconds=30),
        changed_at=NOW - timedelta(seconds=30),
    )
    snapshot, demand, decision = evaluate_office(office(), sources_with(restarted))
    assert_usable(snapshot, demand, decision)


# Scenario 12: existing configurations without an explicit strategy.
def test_existing_configurations_default_to_state_based():
    # 0.1/0.2 room with explicit local parameters (including a max age).
    old = deepcopy(TOPOLOGY)
    old["zones"][0]["max_age_seconds"] = 600
    migrated = migrate_options(old)
    assert "sensor_validation" not in migrated["zones"][0]
    assert migrated == migrate_options(migrated)
    zone = build_model("building", "Building", migrated).zones[0]
    assert (zone.sensor_validation, zone.max_age_seconds) == ("state_based", 600)
    # 0.3 room on central parameters created before sensor_validation existed.
    central = deepcopy(TOPOLOGY)
    for key in ("heating_limit", "cooling_limit", "hysteresis", "max_age_seconds"):
        central["zones"][0].pop(key)
    central["zones"][0].update(use_building_limits=True, use_building_parameters=True)
    central["building"] = {
        "heating_limit": 22,
        "cooling_limit": 24,
        "hysteresis": 0.3,
        "max_age_seconds": 1800,
    }
    building = build_model("building", "Building", migrate_options(central))
    assert building.settings.sensor_validation == "state_based"
    assert building.zones[0].sensor_validation == "state_based"
    assert_usable(*evaluate_office(building, sources_with(temperature(changed=44))))
    # The central strategy applies to zones that follow building parameters.
    central["building"]["sensor_validation"] = "update_based"
    building = build_model("building", "Building", central)
    assert building.zones[0].sensor_validation == "update_based"
    _, demand, _ = evaluate_office(building, sources_with(temperature(changed=44)))
    assert demand.sensor_health == "stale"


@pytest.mark.parametrize(
    "parameters",
    [
        {"sensor_validation": "periodic"},
        {"sensor_validation": None},
        {"sensor_validation": "update_based", "max_age_seconds": 0},
    ],
)
def test_invalid_validation_configuration(parameters):
    topology = deepcopy(TOPOLOGY)
    topology["zones"][0].update(parameters)
    with pytest.raises(ValueError, match="invalid_parameters"):
        build_model("building", "Building", topology)


def test_state_based_with_disabled_max_age_is_valid():
    assert office(max_age_seconds=0).zones[0].sensor_validation == "state_based"
