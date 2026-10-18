"""0.3.1 regression with real HA states, events, timers and options flow."""

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
from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics

TEMPERATURE = "sensor.open_office_temperature"
CELSIUS = {"unit_of_measurement": "°C"}


def office_topology(**zone_parameters):
    """The installed 0.3 shape: central parameters without a validation strategy."""
    return {
        "building": {
            "heating_limit": 22,
            "cooling_limit": 24,
            "hysteresis": 0.3,
            "max_age_seconds": 1800,
        },
        "zones": [
            {
                "id": "open_office",
                "name": "Open Office",
                "temperature_entity": TEMPERATURE,
                "group_id": "buitenunit",
                "use_building_limits": True,
                "use_building_parameters": True,
                **zone_parameters,
            }
        ],
        "groups": [
            {
                "id": "buitenunit",
                "name": "Buitenunit",
                "mode_entity": "sensor.buitenunit_mode",
            }
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


async def setup_office(hass, topology=None, version=3):
    hass.states.async_set(TEMPERATURE, "20.7", CELSIUS)
    hass.states.async_set("climate.binnenunit", "heat")
    hass.states.async_set("sensor.buitenunit_mode", "heat")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Building",
        data={"name": "Building"},
        options=topology or office_topology(),
        version=version,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def report_mode(hass):
    """Mode feedback keeps its own (unchanged) report age; report it as fresh."""
    hass.states.async_set("sensor.buitenunit_mode", "heat")


def sensor_state(hass, entry, suffix):
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    return hass.states.get(entity_id)


def assert_usable(hass, entry):
    zone = sensor_state(hass, entry, "zone_open_office_demand")
    group = sensor_state(hass, entry, "group_buitenunit_mode")
    assert zone.state == "heating_demand"
    assert zone.attributes["sensor_health"] == "valid"
    assert zone.attributes["source_health"]["temperature"] == "valid"
    assert zone.attributes["temperature_c"] == 20.7
    assert zone.attributes["heating_score"] == 1.3
    assert zone.attributes["cooling_score"] == 0
    # First evaluation: below_heating_limit; later ones keep it via hysteresis.
    assert zone.attributes["reason"] in ("below_heating_limit", "heating_hysteresis")
    assert group.attributes["member_zones"] == 1
    assert group.attributes["reliable_zones"] == 1
    assert group.attributes["heating_score"] == 1.3
    assert group.attributes["desired_mode"] == "heat"
    assert group.attributes["blocked_reason"] is None
    return zone, group


def assert_unusable(hass, entry, health):
    zone = sensor_state(hass, entry, "zone_open_office_demand")
    group = sensor_state(hass, entry, "group_buitenunit_mode")
    assert zone.state == "unknown"
    assert zone.attributes["sensor_health"] == health
    assert zone.attributes["reason"] == "insufficient_data"
    assert group.attributes["reliable_zones"] == 0
    assert group.attributes["desired_mode"] == "unknown"
    assert group.attributes["blocked_reason"] == "insufficient_data"


async def test_acceptance_stable_temperature_stays_usable(hass, freezer):
    """Scenarios 1-3/7: 20.7 °C stays usable at 5 min, 44 min and 24 h."""
    with patch("homeassistant.core.ServiceRegistry.async_call") as service_call:
        entry = await setup_office(hass)
        runtime = entry.runtime_data
        start = dt_util.utcnow()
        assert_usable(hass, entry)
        for minutes in (5, 44, 24 * 60):
            moment = start + timedelta(minutes=minutes)
            # Any due timer runs here; none may expire the stable temperature.
            with patch("homeassistant.util.dt.utcnow", return_value=moment):
                async_fire_time_changed(hass, moment)
                await hass.async_block_till_done()
            assert runtime.zone_demands["open_office"].sensor_health == "valid"
            freezer.move_to(moment)
            report_mode(hass)  # Re-evaluates the building at this moment.
            await hass.async_block_till_done()
            zone, _ = assert_usable(hass, entry)
            validation = zone.attributes["temperature_validation"]
            assert validation["health"] == "valid"
            assert validation["strategy"] == "state_based"
            assert validation["value"] == 20.7
            assert validation["seconds_since_change"] == minutes * 60
            assert validation["seconds_since_report"] == minutes * 60
            assert validation["freshness_verified"] is False
            assert validation["reason"] is None
            assert zone.attributes["sensor_validation"] == "state_based"
            assert runtime.zone_demands["open_office"].sensor_health == "valid"
        service_call.assert_not_called()


async def test_unknown_unavailable_invalid_and_recovery(hass):
    """Scenarios 4-6, 13, 14: protections remain and recovery is immediate."""
    entry = await setup_office(hass)
    for state, attributes, health in (
        ("unknown", CELSIUS, "unknown"),
        ("unavailable", CELSIUS, "unavailable"),
        ("abc", CELSIUS, "unreliable"),
        ("20.7", {}, "unreliable"),
    ):
        hass.states.async_set(TEMPERATURE, state, attributes)
        await hass.async_block_till_done()
        assert_unusable(hass, entry, health)
        hass.states.async_set(TEMPERATURE, "20.7", CELSIUS)
        await hass.async_block_till_done()
        assert_usable(hass, entry)
    assert sensor_state(hass, entry, "status").state == "read_only"


async def test_restart_with_restored_placeholder_and_old_value(hass, freezer):
    """Scenario 11: restored placeholders are rejected; real values recover."""
    entry = await setup_office(hass)
    freezer.move_to(dt_util.utcnow() + timedelta(hours=24))
    report_mode(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    # During startup HA can expose a restored placeholder before the source loads.
    hass.states.async_set(TEMPERATURE, "unavailable", {**CELSIUS, "restored": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert_unusable(hass, entry, "unavailable")
    hass.states.async_set(TEMPERATURE, "20.7", CELSIUS)
    await hass.async_block_till_done()
    assert_usable(hass, entry)
    # A reload with a day-old unchanged value (no new report) is still usable.
    freezer.move_to(dt_util.utcnow() + timedelta(hours=24))
    report_mode(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_usable(hass, entry)


async def test_existing_entries_without_strategy_are_migrated_in_place(hass, topology):
    """Scenario 12: 0.1 entries keep their rooms and default to state_based."""
    topology["zones"][0].update(heating_limit=22, cooling_limit=24, max_age_seconds=600)
    topology["zones"][0]["group_id"] = "outdoor_1"
    hass.states.async_set("sensor.office_temperature", "20.7", CELSIUS)
    hass.states.async_set("climate.indoor_1", "heat")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office",
        data={"name": "Office"},
        options=topology,
        version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.options["zones"][0]["id"] == "office"
    assert "sensor_validation" not in entry.options["zones"][0]
    zone = entry.runtime_data.building.zones[0]
    assert (zone.sensor_validation, zone.max_age_seconds) == ("state_based", 600)
    later = dt_util.utcnow() + timedelta(minutes=44)
    with patch("homeassistant.util.dt.utcnow", return_value=later):
        async_fire_time_changed(hass, later)
        await hass.async_block_till_done()
        entry.runtime_data.refresh()
    assert entry.runtime_data.zone_demands["office"].demand == "heating_demand"
    assert entry.runtime_data.zone_demands["office"].sensor_health == "valid"


async def configure_zone(hass, entry, data):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"record": "open_office"}
    )
    assert "sensor_validation" in result["data_schema"].schema
    initial = result
    if (
        "max_age_seconds" in data
        and "max_age_seconds" not in result["data_schema"].schema
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {key: value for key, value in data.items() if key != "max_age_seconds"},
        )
        assert result["type"] == FlowResultType.FORM
        assert "max_age_seconds" in result["data_schema"].schema
    return initial, await hass.config_entries.options.async_configure(
        result["flow_id"], data
    )


async def test_update_based_via_options_flow(hass, freezer):
    """Scenarios 8-10: configured via Options Flow, driven by real HA reports."""
    topology = office_topology(
        use_building_parameters=False, hysteresis=0.3, max_age_seconds=1800
    )
    entry = await setup_office(hass, topology)
    zone_data = {
        "name": "Open Office",
        "temperature_entity": TEMPERATURE,
        "use_building_limits": True,
        "use_building_parameters": False,
        "hysteresis": 0.3,
        "weight": 1,
        "group_id": "buitenunit",
        "sensor_validation": "update_based",
        "max_age_seconds": 0,
    }
    _, result = await configure_zone(hass, entry, zone_data)
    assert result["errors"] == {"base": "invalid_parameters"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**zone_data, "max_age_seconds": 60}
    )
    assert result["type"] == FlowResultType.MENU
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert entry.options["zones"][0]["sensor_validation"] == "update_based"
    assert entry.options["zones"][0]["max_age_seconds"] == 60
    start = dt_util.utcnow()
    assert_usable(hass, entry)
    # Scenario 9: a recent report of an unchanged value keeps it valid.
    freezer.move_to(start + timedelta(seconds=50))
    hass.states.async_set(TEMPERATURE, "20.7", CELSIUS)  # state_reported only.
    await hass.async_block_till_done()
    zone, _ = assert_usable(hass, entry)
    validation = zone.attributes["temperature_validation"]
    assert validation["strategy"] == "update_based"
    assert validation["freshness_verified"] is True
    assert validation["max_update_age_seconds"] == 60
    assert validation["seconds_since_report"] == 0
    assert validation["seconds_since_change"] == 50
    # Scenario 8: no report within the maximum age expires via the timer.
    moment = start + timedelta(seconds=111)
    with patch("homeassistant.util.dt.utcnow", return_value=moment):
        async_fire_time_changed(hass, moment)
        await hass.async_block_till_done()
    assert_unusable(hass, entry, "stale")
    zone = sensor_state(hass, entry, "zone_open_office_demand")
    assert zone.attributes["temperature_validation"]["reason"] == (
        "no_report_within_max_age"
    )
    freezer.move_to(moment)
    # Scenario 10: the next report recovers without a value change.
    hass.states.async_set(TEMPERATURE, "20.7", CELSIUS)
    await hass.async_block_till_done()
    assert_usable(hass, entry)


async def test_building_strategy_option_and_diagnostics(hass):
    entry = await setup_office(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "building"}
    )
    assert "sensor_validation" in result["data_schema"].schema
    data = {
        "heating_limit": 22,
        "cooling_limit": 24,
        "hysteresis": 0.3,
        "sensor_validation": "update_based",
        "max_age_seconds": 0,
    }
    assert "max_age_seconds" not in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {key: value for key, value in data.items() if key != "max_age_seconds"},
    )
    assert result["type"] == FlowResultType.FORM
    assert "max_age_seconds" in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["errors"] == {"base": "invalid_parameters"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**data, "sensor_validation": "state_based"}
    )
    assert result["type"] == FlowResultType.MENU
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    source = diagnostic["source_validation"][0]
    assert source["zone_id"] == "**REDACTED**"
    assert source["temperature"]["health"] == "valid"
    assert source["temperature"]["strategy"] == "state_based"
    assert source["temperature"]["freshness_verified"] is False
    assert "value" not in source["temperature"]
    assert TEMPERATURE not in str(diagnostic)
    assert "Open Office" not in str(diagnostic)
