"""Central policy, local opt-outs, shared weather and safe old-config migration."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.zonentra.const import DOMAIN
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


async def test_real_migration_shared_weather_and_no_commands(hass, topology):
    topology["zones"][0].update(
        outdoor_temperature_entity="sensor.outside", heating_limit=21, cooling_limit=24
    )
    hass.states.async_set("sensor.outside", "10", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "sensor.office_temperature", "20", {"unit_of_measurement": "°C"}
    )
    hass.states.async_set("climate.indoor_1", "off")
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, data={"name": "Office"}, options=topology
    )
    entry.add_to_hass(hass)
    with patch("homeassistant.core.ServiceRegistry.async_call") as service_call:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.version == 3
        assert (
            entry.options["building"]["outdoor_temperature_entity"] == "sensor.outside"
        )
        assert entry.options["zones"][0]["id"] == "office"
        assert entry.runtime_data.zone_demands["office"].heating_score == 1
        assert entry.runtime_data.summary()["outdoor_temperature_c"] == 10
        hass.states.async_set("sensor.outside", "11", {"unit_of_measurement": "°C"})
        await hass.async_block_till_done()
        assert (
            entry.runtime_data.zone_snapshots["office"].outdoor_temperature.value == 11
        )
        service_call.assert_not_called()


async def test_central_options_transaction_override_form_and_clear_source(
    hass, topology
):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"name": "Office"}, options=topology, version=2
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.outside", "12", {"unit_of_measurement": "°C"})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "building"}
    )
    assert "outdoor_temperature_entity" in flow["data_schema"].schema
    profile = {
        "outdoor_temperature_entity": "sensor.outside",
        "heating_limit": 21,
        "cooling_limit": 24,
        "hysteresis": 0.3,
    }
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], profile)
    assert flow["type"] == FlowResultType.MENU
    assert "building" not in entry.options
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "zone"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"record": "office"}
    )
    assert "outdoor_temperature_entity" not in flow["data_schema"].schema
    assert "heating_limit" not in flow["data_schema"].schema  # Shared by default.
    data = {
        "name": "Office",
        "temperature_entity": "sensor.office_temperature",
        "use_building_limits": False,
        "use_building_parameters": False,
    }
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], data)
    assert flow["type"] == FlowResultType.FORM
    assert "heating_limit" in flow["data_schema"].schema
    assert "hysteresis" in flow["data_schema"].schema
    data.update(heating_limit=19, cooling_limit=25, hysteresis=0.4)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], data)
    assert flow["type"] == FlowResultType.MENU
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    assert entry.options["building"] == profile
    assert not entry.options["zones"][0]["use_building_limits"]
    # Clear the building source explicitly; no legacy room fallback may return.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "building"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"heating_limit": 21, "cooling_limit": 24}
    )
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    assert "outdoor_temperature_entity" not in entry.options["building"]


async def test_global_weather_expiry_without_source_change(hass, topology):
    topology = central_topology(topology)
    topology["building"]["outdoor_max_age_seconds"] = 10
    hass.states.async_set("sensor.outside", "10", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "sensor.office_temperature", "20", {"unit_of_measurement": "°C"}
    )
    hass.states.async_set("climate.indoor_1", "off")
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"name": "Office"}, options=topology
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    after = dt_util.utcnow() + timedelta(seconds=11)
    with patch("homeassistant.util.dt.utcnow", return_value=after):
        async_fire_time_changed(hass, after)
        await hass.async_block_till_done()
    assert entry.runtime_data.outdoor_temperature.quality == "stale"
    assert (
        entry.runtime_data.zone_demands["office"].demand == "heating_demand"
    )  # Optional building context.
    status_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_status"
    )
    assert (
        hass.states.get(status_id).attributes["outdoor_temperature_quality"] == "stale"
    )


async def test_switch_to_central_retains_dormant_overrides_and_updates_sensors(
    hass, topology
):
    topology = central_topology(topology)
    topology["zones"][0].update(
        heating_limit=18, cooling_limit=28, use_building_limits=False
    )
    hass.states.async_set("sensor.outside", "10", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "sensor.office_temperature", "20", {"unit_of_measurement": "°C"}
    )
    hass.states.async_set("climate.indoor_1", "off")
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"name": "Office"}, options=topology
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zone_demands["office"].demand == "neutral"
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "zone"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"record": "office"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "name": "Office",
            "temperature_entity": "sensor.office_temperature",
            "use_building_limits": True,
            "use_building_parameters": True,
        },
    )
    assert flow["type"] == FlowResultType.MENU
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert entry.options["zones"][0]["heating_limit"] == 18  # Retained but inactive.
    assert entry.runtime_data.zone_demands["office"].heating_score == 1
    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_zone_office_demand"
    )
    assert hass.states.get(sensor_id).attributes["limits_source"] == "building"
    # A subsequent central change affects inherited rooms on reload.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "building"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"heating_limit": 19, "cooling_limit": 24}
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.zone_demands["office"].demand == "neutral"


async def test_migration_conflict_can_be_resolved_without_sensor_state(hass, topology):
    topology["zones"][0]["outdoor_temperature_entity"] = "sensor.north"
    topology["zones"].append(
        {"id": "other", "name": "Other", "outdoor_temperature_entity": "sensor.south"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, data={"name": "Office"}, options=topology
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.summary()["outdoor_temperature_selection_required"]
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "building"}
    )
    assert "sensor.north" in flow["description_placeholders"]["outdoor_candidates"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"outdoor_temperature_entity": "sensor.north"}
    )
    assert flow["type"] == FlowResultType.MENU
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert (
        entry.runtime_data.building.settings.outdoor_temperature_entity
        == "sensor.north"
    )
    assert not entry.runtime_data.summary()["outdoor_temperature_selection_required"]


async def test_removed_controller_is_not_tracked_and_does_not_degrade(hass, topology):
    topology["zones"][0].update(
        controller_entity="climate.old_vtherm", heating_limit=21, cooling_limit=24
    )
    hass.states.async_set(
        "sensor.office_temperature", "20", {"unit_of_measurement": "°C"}
    )
    hass.states.async_set("climate.indoor_1", "off")
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"name": "Office"}, options=topology
    )
    entry.add_to_hass(hass)
    with patch("homeassistant.core.ServiceRegistry.async_call") as service_call:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.version == 3
        assert "controller_entity" not in entry.options["zones"][0]
        assert "climate.old_vtherm" not in entry.runtime_data.building.entity_ids
        assert entry.runtime_data.status == "read_only"
        assert entry.runtime_data.zone_demands["office"].heating_score == 1
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "zone"}
        )
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"record": "office"}
        )
        assert "controller_entity" not in flow["data_schema"].schema
        service_call.assert_not_called()
