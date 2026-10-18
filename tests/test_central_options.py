"""0.13.0: the central configuration is edited per subject (menu)."""

from homeassistant.data_entry_flow import FlowResultType

from custom_components.zonentra.config_flow import (
    CENTRAL_SECTIONS,
    building_fields,
)
from tests.test_entry_layout import setup_central


def test_every_central_field_has_exactly_one_section():
    fields = [marker.schema for marker in building_fields(limits=False)]
    placed = [key for keys in CENTRAL_SECTIONS.values() for key in keys]
    assert sorted(placed) == sorted(fields)
    assert len(placed) == len(set(placed))


async def open_section(hass, entry, section):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] == FlowResultType.MENU
    assert flow["menu_options"] == [*CENTRAL_SECTIONS, "group", "finish"]
    return await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": section}
    )


async def test_a_section_changes_only_its_own_settings(hass):
    hass.states.async_set("sensor.solar_power", "0", {"unit_of_measurement": "W"})
    central = await setup_central(hass)
    flow = await open_section(hass, central, "screens")
    assert set(flow["data_schema"].schema) == set(CENTRAL_SECTIONS["screens"])
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"sun_power_entity": "sensor.solar_power", "sun_power_threshold": 800},
    )
    assert flow["type"] == FlowResultType.MENU  # Back to the subjects.
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    building = central.options["building"]
    assert building["sun_power_entity"] == "sensor.solar_power"
    assert building["sun_power_threshold"] == 800
    # Limits (device) and other subjects are untouched.
    assert (building["heating_limit"], building["hysteresis"]) == (22, 0.3)


async def test_clearing_a_field_in_a_section_removes_it(hass):
    hass.states.async_set("binary_sensor.building", "on")
    central = await setup_central(hass)
    flow = await open_section(hass, central, "presence")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"presence_entity": "binary_sensor.building"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "presence"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"motion_delay_seconds": 30}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert "presence_entity" not in central.options["building"]


async def test_invalid_section_input_stays_on_the_form(hass):
    central = await setup_central(hass)
    flow = await open_section(hass, central, "general")
    assert "max_age_seconds" not in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"sensor_validation": "update_based"}
    )
    assert flow["type"] == FlowResultType.FORM
    assert "max_age_seconds" in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"sensor_validation": "update_based", "max_age_seconds": 0}
    )
    assert flow["type"] == FlowResultType.FORM
    assert flow["errors"] == {"base": "invalid_parameters"}


async def test_new_central_configuration_asks_only_name_and_outdoor(hass):
    flow = await hass.config_entries.flow.async_init(
        "zonentra", context={"source": "user"}
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "central"}
    )
    assert set(flow["data_schema"].schema) == {"name", "outdoor_temperature_entity"}
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "Office"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    building = flow["result"].options["building"]
    # Defaults are stored, so the sections show them.
    assert building["heating_limit"] == 21.0
    assert building["screen_override_minutes"] == 120.0


async def test_screen_sun_entity_is_saved_and_followed(hass):
    from tests.test_shading_runtime import office

    central, _ = await office(hass)
    hass.states.async_set(
        "sun.local_sun", "below_horizon", {"azimuth": 300, "elevation": -5}
    )
    flow = await open_section(hass, central, "screens")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"sun_entity": "sun.local_sun", "sun_power_entity": "sensor.solar_power"},
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    runtime = central.runtime_data
    assert runtime.building.settings.sun_entity == "sun.local_sun"
    assert "sun.local_sun" in runtime.building.entity_ids
    assert runtime.screen_decisions["shade_west_screen"].reason == "night"
    hass.states.async_set(
        "sun.local_sun", "above_horizon", {"azimuth": 180, "elevation": 50}
    )
    await hass.async_block_till_done()
    assert runtime.screen_decisions["shade_west_screen"].reason == "no_sun"


async def test_state_based_hides_but_preserves_configured_timeout(hass):
    central = await setup_central(hass)
    options = dict(central.options)
    options["building"] = {
        **options["building"],
        "sensor_validation": "update_based",
        "max_age_seconds": 600,
    }
    hass.config_entries.async_update_entry(central, options=options)
    await hass.async_block_till_done()
    flow = await open_section(hass, central, "general")
    assert "max_age_seconds" in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"sensor_validation": "state_based", "max_age_seconds": 600}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "general"}
    )
    assert "max_age_seconds" not in flow["data_schema"].schema
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"sensor_validation": "state_based"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert central.options["building"]["max_age_seconds"] == 600
