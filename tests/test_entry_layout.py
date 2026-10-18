"""0.6.0 integration: central configuration + zone entries (VTherm-style)."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zonentra import central_device_entry
from custom_components.zonentra.const import DOMAIN
from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics

CELSIUS = {"unit_of_measurement": "°C"}


def zone_options(zone_id, *units, **fields):
    return {
        "id": zone_id,
        "name": zone_id.replace("_", " ").title(),
        "temperature_entity": f"sensor.{zone_id}_temperature",
        "group_id": "outdoor_1",
        "indoor_units": list(units),
        "weight": 1.0,
        "use_building_limits": True,
        "use_building_parameters": True,
        **fields,
    }


async def edit_zone_subject(hass, flow, subject, values):
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": subject}
    )
    assert flow["type"] == FlowResultType.FORM
    return await hass.config_entries.options.async_configure(flow["flow_id"], values)


def set_states(hass, zone_id, *units, temperature="21"):
    hass.states.async_set(f"sensor.{zone_id}_temperature", temperature, CELSIUS)
    for unit in units:
        # These fixtures model units already off before configuration, not a
        # freshly observed physical stop. Fresh-stop timing has dedicated tests.
        if hass.states.get(unit) is None:
            hass.states.async_set(
                unit,
                "off",
                {"hvac_action": "off"},
                timestamp=(dt_util.utcnow() - timedelta(hours=1)).timestamp(),
            )
        else:
            hass.states.async_set(unit, "off", {"hvac_action": "off"})


async def setup_central(hass):
    central = MockConfigEntry(
        domain=DOMAIN,
        title="Building",
        data={"entry_type": "central", "name": "Building"},
        options={
            "building": {"heating_limit": 22, "cooling_limit": 24, "hysteresis": 0.3},
            "groups": [{"id": "outdoor_1", "name": "Outdoor 1"}],
        },
        unique_id="central",
        version=3,
    )
    central.add_to_hass(hass)
    assert await hass.config_entries.async_setup(central.entry_id)
    await hass.async_block_till_done()
    return central


async def add_zone(hass, options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=options["name"],
        data={"entry_type": "zone"},
        options=options,
        unique_id=options["id"],
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()  # Includes the central rebuild.
    return entry


def entity(hass, unique_id):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)


async def test_zones_join_the_central_building_without_room_commands(hass):
    with patch("homeassistant.core.ServiceRegistry.async_call") as service_call:
        set_states(hass, "west", "climate.west_ac")
        set_states(hass, "meeting", "climate.meeting_ac", temperature="23")
        central = await setup_central(hass)
        assert central.runtime_data.building.zones == ()
        west = await add_zone(hass, zone_options("west", "climate.west_ac"))
        meeting = await add_zone(hass, zone_options("meeting", "climate.meeting_ac"))
        building = central.runtime_data.building
        assert [zone.id for zone in building.zones] == ["west", "meeting"]
        assert west.state is ConfigEntryState.LOADED
        # Zone device with its own sensors, linked to the central device.
        demand_id = entity(hass, f"{west.entry_id}_demand")
        assert hass.states.get(demand_id).state == "heating_demand"
        assert hass.states.get(entity(hass, f"{meeting.entry_id}_demand")).state == (
            "neutral"
        )
        devices = dr.async_get(hass)
        zone_device = devices.async_get(
            er.async_get(hass).async_get(demand_id).device_id
        )
        central_device = central_device_entry(hass, central)
        assert zone_device.name == "West"
        assert zone_device.via_device_id == central_device.id
        control = hass.states.get(entity(hass, f"{west.entry_id}_hvac_west_ac_control"))
        assert control.attributes["simulation"] is True
        assert (
            hass.states.get(
                entity(hass, f"{west.entry_id}_hvac_west_ac_indoor_unit")
            ).state
            == "off"
        )
        # Group and building sensors stay on the central device.
        group = hass.states.get(
            entity(hass, f"{central.entry_id}_group_outdoor_1_mode")
        )
        assert group.attributes["member_zones"] == 2
        assert entity(hass, f"{central.entry_id}_zone_west_demand") is None
        # Live updates reach zone sensors through the central runtime.
        hass.states.async_set("sensor.meeting_temperature", "25", CELSIUS)
        await hass.async_block_till_done()
        assert hass.states.get(entity(hass, f"{meeting.entry_id}_demand")).state == (
            "cooling_demand"
        )
        # Only the 0.7 outdoor-stand presets; room regulation is off by default.
        services = {call.args[1] for call in service_call.call_args_list}
        assert services <= {"set_hvac_mode", "turn_off"}


async def test_config_flow_creates_central_then_zone(hass):
    set_states(hass, "lab", "climate.lab_ac")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "central_required"
    central = await setup_central(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "zone"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Lab",
            "temperature_entity": "sensor.lab_temperature",
            "indoor_units": ["climate.lab_ac"],
            "group_id": "outdoor_1",
            "weight": 1,
            "use_building_limits": False,
            "use_building_parameters": True,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {"entry_type": "zone"}
    options = result["result"].options
    assert options["indoor_units"] == ["climate.lab_ac"]
    assert "heating_limit_entity" not in options
    assert "cooling_limit_entity" not in options
    await hass.async_block_till_done()
    zone = central.runtime_data.building.zones[0]
    assert zone.name == "Lab"
    snapshot = central.runtime_data.zone_snapshots[zone.id]  # Generated ID.
    assert (snapshot.heating_limit.value, snapshot.cooling_limit.value) == (22, 24)


async def test_indoor_unit_cannot_join_two_zones(hass):
    set_states(hass, "west", "climate.shared_ac")
    await setup_central(hass)
    await add_zone(hass, zone_options("west", "climate.shared_ac"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "East",
            "indoor_units": ["climate.shared_ac"],
            "group_id": "outdoor_1",
        },
    )
    assert result["errors"] == {"base": "duplicate_equipment_entity"}


async def test_zone_options_rebuild_building_and_entities(hass):
    set_states(hass, "west", "climate.west_ac", "climate.west_ac_2")
    central = await setup_central(hass)
    west = await add_zone(hass, zone_options("west", "climate.west_ac"))
    result = await hass.config_entries.options.async_init(west.entry_id)
    assert result["step_id"] == "zone_menu"
    result = await edit_zone_subject(
        hass,
        result,
        "zone_general",
        {
            "name": "West wing",
            "weight": 2,
            "use_building_limits": True,
        },
    )
    result = await edit_zone_subject(
        hass,
        result,
        "zone_hvac",
        {
            "indoor_units": ["climate.west_ac", "climate.west_ac_2"],
            "group_id": "outdoor_1",
        },
    )
    assert west.title == "West"  # No reload or partial writes while editing.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert west.title == "West wing"
    building = central.runtime_data.building
    assert building.zones[0].weight == 2
    assert [item.entity_id for item in building.equipment] == [
        "climate.west_ac",
        "climate.west_ac_2",
    ]
    assert hass.states.get(entity(hass, f"{west.entry_id}_hvac_west_ac_2_control"))


async def test_removed_zone_leaves_the_building(hass):
    set_states(hass, "west", "climate.west_ac")
    central = await setup_central(hass)
    west = await add_zone(hass, zone_options("west", "climate.west_ac"))
    demand_id = entity(hass, f"{west.entry_id}_demand")
    assert await hass.config_entries.async_remove(west.entry_id)
    await hass.async_block_till_done()
    assert central.runtime_data.building.zones == ()
    assert er.async_get(hass).async_get(demand_id) is None


async def test_referenced_group_cannot_be_deleted(hass):
    set_states(hass, "west", "climate.west_ac")
    central = await setup_central(hass)
    await add_zone(hass, zone_options("west", "climate.west_ac"))
    result = await hass.config_entries.options.async_init(central.entry_id)
    assert result["menu_options"] == [
        "general",
        "presence",
        "regulation",
        "screens",
        "forecast",
        "preconditioning",
        "group",
        "finish",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "group"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"record": "outdoor_1"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Outdoor 1", "delete": True}
    )
    assert result["errors"] == {"base": "invalid_reference"}


async def test_zone_without_central_configuration_fails(hass):
    set_states(hass, "west", "climate.west_ac")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="West",
        data={"entry_type": "zone"},
        options=zone_options("west", "climate.west_ac"),
        version=3,
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_legacy_entry_works_and_raises_repair_issue(hass, topology):
    hass.states.async_set("sensor.office_temperature", "20", CELSIUS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office",
        data={"name": "Office"},
        options=topology,
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"legacy_configuration_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert entity(hass, f"{entry.entry_id}_zone_office_demand")  # Unchanged IDs.
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_zone_diagnostics_are_redacted(hass):
    set_states(hass, "west", "climate.west_ac")
    await setup_central(hass)
    west = await add_zone(hass, zone_options("west", "climate.west_ac"))
    diagnostic = await async_get_config_entry_diagnostics(hass, west)
    assert diagnostic["entry_type"] == "zone"
    assert diagnostic["central_loaded"] is True
    assert diagnostic["zone_demand"]["demand"] == "heating_demand"
    assert diagnostic["unit_intents"][0]["simulation"] is True
    assert "climate.west_ac" not in str(diagnostic)
    assert "sensor.west_temperature" not in str(diagnostic)
