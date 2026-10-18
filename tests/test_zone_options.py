"""Room subjects remain isolated and changes are saved as one transaction."""

from copy import deepcopy

import pytest
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
                    "sensor_validation": "update_based",
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
    hass.states.async_set("binary_sensor.west_presence", "on")
    hass.states.async_set("cover.west_screen", "open")
    central = await setup_central(hass)
    room = await add_zone(
        hass,
        zone_options(
            "west",
            "climate.west_ac",
            occupancy_entity="binary_sensor.west_presence",
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
        key.schema: (key.description or {}).get("suggested_value")
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
    assert "occupancy_entity" not in room.options
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


async def test_local_parameters_are_shown_only_after_disabling_inheritance(hass):
    set_states(hass, "west")
    await setup_central(hass)
    room = await add_zone(
        hass,
        zone_options("west", hysteresis=0.7, max_age_seconds=600),
    )
    flow = await hass.config_entries.options.async_init(room.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "zone_regulation"}
    )
    assert set(flow["data_schema"].schema) == {"use_building_parameters"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"use_building_parameters": False}
    )
    assert flow["type"] == FlowResultType.FORM
    assert "hysteresis" in flow["data_schema"].schema
    assert "max_age_seconds" not in flow["data_schema"].schema
    suggestions = {
        key.schema: (key.description or {}).get("suggested_value")
        for key in flow["data_schema"].schema
    }
    assert suggestions["hysteresis"] == 0.7
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "use_building_parameters": False,
            "hysteresis": 0.7,
            "sensor_validation": "update_based",
        },
    )
    assert flow["type"] == FlowResultType.FORM
    assert "max_age_seconds" in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "use_building_parameters": False,
            "hysteresis": 0.7,
            "sensor_validation": "update_based",
            "max_age_seconds": 600,
        },
    )
    assert flow["type"] == FlowResultType.MENU
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert room.options["max_age_seconds"] == 600
    assert room.options["hysteresis"] == 0.7
    assert room.options["use_building_parameters"] is False
    assert room.options["sensor_validation"] == "update_based"


@pytest.mark.parametrize(
    ("section", "flag", "field", "value"),
    [
        ("zone_floor_heating", "use_building_floor_heating", "floor_heating_limit", 17),
        (
            "zone_preconditioning",
            "use_building_preconditioning",
            "preconditioning_max_minutes",
            90,
        ),
    ],
)
async def test_inherited_values_stay_hidden_and_survive_round_trip(
    hass, section, flag, field, value
):
    set_states(hass, "west")
    await setup_central(hass)
    room = await add_zone(hass, zone_options("west", **{flag: True, field: value}))
    flow = await hass.config_entries.options.async_init(room.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": section}
    )
    assert field not in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {flag: False}
    )
    assert flow["type"] == FlowResultType.FORM
    suggestions = {
        key.schema: (key.description or {}).get("suggested_value")
        for key in flow["data_schema"].schema
    }
    assert suggestions[field] == value
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {flag: False, field: value}
    )
    assert flow["type"] == FlowResultType.MENU
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": section}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {flag: True}
    )
    assert flow["type"] == FlowResultType.MENU
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": section}
    )
    assert field not in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {flag: True}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert room.options[flag] is True
    assert room.options[field] == value
