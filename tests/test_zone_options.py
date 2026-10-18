"""Room subjects remain isolated and changes are saved as one transaction."""

from copy import deepcopy

from homeassistant.data_entry_flow import FlowResultType

from custom_components.zonentra.config_flow import (
    ZONE_OPTION_SECTIONS,
    zone_fields,
    zone_parameter_fields,
    zone_section_fields,
)
from tests.test_entry_layout import (
    add_zone,
    edit_zone_subject,
    set_states,
    setup_central,
    zone_options,
)


def test_room_subjects_cover_each_editable_field_once():
    """A split must not lose a setting or expose it on conflicting pages."""
    all_fields = {
        marker.schema
        for marker in {
            **zone_fields([]),
            **zone_parameter_fields(
                {
                    "use_building_parameters": False,
                    "use_building_floor_heating": False,
                    "use_building_preconditioning": False,
                }
            ),
        }
    }
    covered = []
    for section in ZONE_OPTION_SECTIONS:
        fields = {marker.schema for marker in zone_section_fields(section, [])}
        assert 1 <= len(fields) <= 6
        covered.extend(fields)
    assert set(covered) == all_fields
    assert len(covered) == len(all_fields)


async def test_room_subjects_preserve_other_pages_and_clear_own_fields(hass):
    set_states(hass, "west", "climate.west_ac")
    hass.states.async_set("sensor.west_humidity", "45")
    hass.states.async_set("cover.west_screen", "open")
    central = await setup_central(hass)
    room = await add_zone(
        hass,
        zone_options(
            "west",
            "climate.west_ac",
            humidity_entity="sensor.west_humidity",
            shading_entities=["cover.west_screen"],
            facade="south",
            use_building_parameters=False,
            hysteresis=0.4,
        ),
    )
    original = deepcopy(dict(room.options))
    runtime = central.runtime_data
    flow = await hass.config_entries.options.async_init(room.entry_id)
    assert flow["type"] == FlowResultType.MENU
    assert flow["menu_options"] == [*ZONE_OPTION_SECTIONS, "finish"]
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_general",
        {
            "name": "West wing",
            "weight": 2,
            "use_building_limits": True,
        },
    )
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_sensors",
        {
            "temperature_entity": "sensor.west_temperature",
        },
    )
    # Reopening a page uses the draft, not the old saved settings.
    form = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "zone_general"}
    )
    suggested = {
        key.schema: key.description.get("suggested_value")
        for key in form["data_schema"].schema
    }
    assert suggested["name"] == "West wing"
    assert room.options == original
    assert central.runtime_data is runtime
    flow = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "name": "West wing",
            "weight": 2,
            "use_building_limits": True,
        },
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert room.title == "West wing"
    assert room.options["weight"] == 2
    assert "humidity_entity" not in room.options
    for key in (
        "indoor_units",
        "group_id",
        "shading_entities",
        "facade",
        "hysteresis",
        "use_building_parameters",
    ):
        assert room.options[key] == original[key]


async def test_closing_room_settings_discards_draft(hass):
    set_states(hass, "west")
    await setup_central(hass)
    room = await add_zone(hass, zone_options("west"))
    original = deepcopy(dict(room.options))
    flow = await hass.config_entries.options.async_init(room.entry_id)
    flow = await edit_zone_subject(hass, flow, "zone_general", {"name": "Changed"})
    hass.config_entries.options.async_abort(flow["flow_id"])
    assert room.options == original
    assert room.title == "West"


async def test_invalid_room_subject_keeps_previous_draft(hass):
    set_states(hass, "west")
    await setup_central(hass)
    room = await add_zone(hass, zone_options("west"))
    flow = await hass.config_entries.options.async_init(room.entry_id)
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_sensors",
        {
            "temperature_entity": "sensor.does_not_exist",
        },
    )
    assert flow["type"] == FlowResultType.FORM
    assert flow["step_id"] == "zone_sensors"
    assert flow["errors"] == {"base": "entity_not_found"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "temperature_entity": "sensor.west_temperature",
        },
    )
    assert flow["type"] == FlowResultType.MENU
    assert room.options["temperature_entity"] == "sensor.west_temperature"
