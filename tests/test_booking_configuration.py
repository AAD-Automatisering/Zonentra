"""Room calendars are optional, validated and removable through options."""

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.zonentra.migration import PRECONDITIONING_FIELDS, migrate_options
from custom_components.zonentra.models import build_model
from tests.test_bookings_runtime import CALENDAR, calendar_service, office
from tests.test_entry_layout import (
    add_zone,
    edit_zone_subject,
    set_states,
    zone_options,
)

LOCAL = dict(zip(PRECONDITIONING_FIELDS, (120, 15, 0.04, 0.06), strict=True))
CENTRAL = dict(zip(PRECONDITIONING_FIELDS, (240, 20, 0.03, 0.08), strict=True))


def test_preparation_inheritance_preserves_existing_local_values():
    topology = {
        "building": CENTRAL,
        "zones": [
            {"id": "inherited", "name": "Inherited"},
            {"id": "existing", "name": "Existing", **LOCAL},
            {
                "id": "dormant",
                "name": "Dormant",
                **LOCAL,
                "use_building_preconditioning": True,
            },
            {
                "id": "local",
                "name": "Local",
                **LOCAL,
                "use_building_preconditioning": False,
            },
        ],
    }
    migrated = migrate_options(topology)
    assert migrated == migrate_options(migrated)
    assert "use_building_preconditioning" not in topology["zones"][0]
    rooms = build_model("test", "Test", migrated).zones
    assert [room.use_building_preconditioning for room in rooms] == [
        True,
        True,
        True,
        False,
    ]
    assert migrated["zones"][1]["preconditioning_max_minutes"] == 120
    assert [
        tuple(getattr(room, key) for key in PRECONDITIONING_FIELDS) for room in rooms
    ] == [
        tuple(CENTRAL.values()),
        tuple(CENTRAL.values()),
        tuple(CENTRAL.values()),
        tuple(LOCAL.values()),
    ]


@pytest.mark.parametrize(
    "key,value",
    [
        ("preconditioning_max_minutes", 361),
        ("preconditioning_buffer_minutes", -1),
        ("preconditioning_heating_rate", 0),
        ("preconditioning_cooling_rate", float("nan")),
    ],
)
def test_central_preparation_values_are_validated(key, value):
    with pytest.raises(ValueError):
        build_model("test", "Test", {"building": {key: value}})


async def test_central_preparation_updates_inheriting_rooms_only(hass):
    central, old, _ = await office(hass, use_building_preconditioning=False, **LOCAL)
    assert old.options["use_building_preconditioning"] is False
    set_states(hass, "east")
    inherited = await add_zone(hass, zone_options("east"))
    assert inherited.options["use_building_preconditioning"] is True
    flow = await hass.config_entries.options.async_init(central.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "preconditioning"}
    )
    assert set(flow["data_schema"].schema) == set(PRECONDITIONING_FIELDS)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], CENTRAL)
    assert flow["type"] == FlowResultType.MENU
    assert "preconditioning_max_minutes" not in central.options["building"]
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    rooms = {room.id: room for room in central.runtime_data.building.zones}
    assert rooms["west"].preconditioning_max_minutes == 120
    assert rooms["east"].preconditioning_max_minutes == 240


async def test_room_can_switch_to_central_and_restore_dormant_preparation(hass):
    central, zone, _ = await office(hass, use_building_preconditioning=False, **LOCAL)
    hass.config_entries.async_update_entry(
        central,
        options={
            **central.options,
            "building": {**central.options["building"], **CENTRAL},
        },
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    flow = await hass.config_entries.options.async_init(zone.entry_id)
    flow = await edit_zone_subject(
        hass,
        flow,
        "zone_preconditioning",
        {
            "calendar_entity": CALENDAR,
            "use_building_preconditioning": True,
        },
    )
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    await hass.async_block_till_done()
    assert central.runtime_data.building.zones[0].preconditioning_max_minutes == 240
    assert zone.options["preconditioning_max_minutes"] == 120
    flow = await hass.config_entries.options.async_init(zone.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "zone_preconditioning"}
    )
    suggested = {
        marker.schema: marker.description.get("suggested_value")
        for marker in flow["data_schema"].schema
        if marker.schema in LOCAL
    }
    assert suggested == LOCAL
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "calendar_entity": CALENDAR,
            "use_building_preconditioning": False,
            **LOCAL,
        },
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert central.runtime_data.building.zones[0].preconditioning_max_minutes == 120


@pytest.mark.parametrize(
    "settings",
    [
        {"calendar_entity": "sensor.wrong_domain"},
        {"preconditioning_heating_rate": 0},
        {"preconditioning_cooling_rate": float("nan")},
        {"preconditioning_max_minutes": 361},
        {"preconditioning_buffer_minutes": -1},
        {"preconditioning_max_minutes": True},
    ],
)
def test_booking_configuration_rejects_invalid_values(settings):
    with pytest.raises(ValueError):
        build_model(
            "test",
            "Test",
            {
                "zones": [
                    {
                        "id": "west",
                        "name": "West",
                        "use_building_preconditioning": False,
                        **settings,
                    }
                ]
            },
        )


async def test_calendar_can_be_changed_and_removed_using_room_options(hass):
    central, zone, _ = await office(hass)
    other = "calendar.other_meeting"
    hass.states.async_set(other, "off")
    calendar_service(hass, {other: {"events": []}})
    values = {"calendar_entity": other, "use_building_preconditioning": False, **LOCAL}
    flow = await hass.config_entries.options.async_init(zone.entry_id)
    flow = await edit_zone_subject(hass, flow, "zone_preconditioning", values)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    runtime = central.runtime_data
    room = runtime.building.zones[0]
    assert room.calendar_entity == other
    assert room.preconditioning_max_minutes == 120
    assert room.preconditioning_buffer_minutes == 15
    assert CALENDAR not in runtime.building.entity_ids
    assert other in runtime.building.entity_ids
    values.pop("calendar_entity")
    flow = await hass.config_entries.options.async_init(zone.entry_id)
    flow = await edit_zone_subject(hass, flow, "zone_preconditioning", values)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert central.runtime_data.building.zones[0].calendar_entity is None
    assert central.runtime_data.booking_plans["west"].status == "not_configured"
    assert other not in central.runtime_data.building.entity_ids


async def test_central_preparation_is_default_in_new_and_existing_room_forms(hass):
    central, zone, _ = await office(hass, **LOCAL)
    assert zone.options["use_building_preconditioning"] is True
    assert central.runtime_data.building.zones[0].preconditioning_max_minutes == 180
    flow = await hass.config_entries.options.async_init(zone.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "zone_preconditioning"}
    )
    marker = next(
        key
        for key in flow["data_schema"].schema
        if key.schema == "use_building_preconditioning"
    )
    assert marker.description["suggested_value"] is True
    flow = await hass.config_entries.flow.async_init(
        "zonentra", context={"source": "user"}
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "zone"}
    )
    marker = next(
        key
        for key in flow["data_schema"].schema
        if key.schema == "use_building_preconditioning"
    )
    assert marker.description["suggested_value"] is True
