"""Device limits replace obsolete entity links; local sensor policy stays editable."""

from copy import deepcopy

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.zonentra.config_flow import (
    building_fields,
    zone_parameter_fields,
)
from custom_components.zonentra.migration import migrate_options
from tests.test_entry_layout import (
    CELSIUS,
    add_zone,
    edit_zone_subject,
    set_states,
    setup_central,
    zone_options,
)

HEAT = "input_number.west_heat"
COOL = "input_number.west_cool"
REMOVED = {"heating_limit_entity": HEAT, "cooling_limit_entity": COOL}


def test_limit_entities_are_absent_from_all_parameter_forms():
    for limits in (False, True):
        assert not set(REMOVED).intersection(
            key.schema for key in building_fields(limits)
        )
        for parameters in (False, True):
            fields = zone_parameter_fields(
                {"use_building_limits": limits, "use_building_parameters": parameters}
            )
            assert not set(REMOVED).intersection(key.schema for key in fields)
            assert bool(fields) is not parameters


@pytest.mark.parametrize(
    "local", [{}, {"heating_limit": 19}, {"heating_limit": 19, "cooling_limit": 26}]
)
def test_old_links_are_removed_without_mutating_input_and_migration_is_idempotent(
    local,
):
    options = {
        "building": {**REMOVED},
        "zones": [{"id": "west", "name": "West", **REMOVED, **local}],
    }
    original = deepcopy(options)
    migrated = migrate_options(options)
    assert migrated == migrate_options(migrated)
    assert options == original
    assert migrated["building"] == {"heating_limit": 21, "cooling_limit": 24}
    zone = migrated["zones"][0]
    assert not set(REMOVED).intersection(zone)
    if len(local) == 2:
        assert zone["heating_limit"] == 19
        assert zone["cooling_limit"] == 26
    else:
        assert zone["use_building_limits"] is True


async def test_existing_zone_links_are_cleaned_and_no_longer_observed(hass):
    set_states(hass, "west")
    hass.states.async_set(HEAT, "19", CELSIUS)
    hass.states.async_set(COOL, "26", CELSIUS)
    central = await setup_central(hass)
    zone = await add_zone(
        hass, zone_options("west", use_building_limits=False, **REMOVED)
    )
    assert not set(REMOVED).intersection(zone.options)
    runtime = central.runtime_data
    assert not {HEAT, COOL}.intersection(runtime.building.entity_ids)
    snapshot = runtime.zone_snapshots["west"]
    assert (snapshot.heating_limit.value, snapshot.cooling_limit.value) == (22, 24)
    hass.states.async_set(HEAT, "17", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.zone_snapshots["west"].heating_limit.value == 22


@pytest.mark.parametrize("static", [True, False])
async def test_existing_central_links_are_cleaned_on_reload(hass, static):
    central = await setup_central(hass)
    settings = dict(central.options["building"])
    if not static:
        settings.pop("heating_limit")
        settings.pop("cooling_limit")
    hass.config_entries.async_update_entry(
        central,
        options={**central.options, "building": {**settings, **REMOVED}},
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    assert not set(REMOVED).intersection(central.options["building"])
    assert not {HEAT, COOL}.intersection(central.runtime_data.building.entity_ids)
    settings = central.runtime_data.building.settings
    assert (settings.heating_limit, settings.cooling_limit) == (
        (22, 24) if static else (21, 24)
    )


async def test_local_parameter_edits_retain_device_limits_without_entity_step(hass):
    set_states(hass, "west")
    central = await setup_central(hass)
    zone = await add_zone(
        hass,
        zone_options(
            "west",
            use_building_limits=False,
            use_building_parameters=False,
            heating_limit=19,
            cooling_limit=26,
            hysteresis=0.4,
        ),
    )
    runtime = central.runtime_data
    runtime.set_thermostat("west", heating_limit=20, cooling_limit=25)
    flow = await hass.config_entries.options.async_init(zone.entry_id)
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_regulation",
        {
            "use_building_parameters": False,
            "hysteresis": 0.5,
        },
    )
    assert flow["type"] == FlowResultType.MENU
    assert zone.options["hysteresis"] == 0.4
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert zone.options["hysteresis"] == 0.5
    assert zone.options["heating_limit"] == 19
    snapshot = central.runtime_data.zone_snapshots["west"]
    assert (snapshot.heating_limit.value, snapshot.cooling_limit.value) == (20, 25)
