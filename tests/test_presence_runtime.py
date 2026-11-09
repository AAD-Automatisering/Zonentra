"""0.8.0 integration: presence and motion select comfort / eco / away bands."""

from homeassistant.helpers import entity_registry as er

from custom_components.zonentra.const import DOMAIN
from tests.test_entry_layout import add_zone, set_states, setup_central, zone_options

CELSIUS = {"unit_of_measurement": "°C"}
PRESENCE = "binary_sensor.office_occupied"
MOTION = "binary_sensor.office_motion"


async def test_thermostat_preset_follows_absence(hass):
    """0.10: an absent building shows preset away and the away limits."""
    set_states(hass, "west", "climate.west_ac")
    hass.states.async_set(PRESENCE, "on")
    central = await setup_central(hass)
    building = {**central.options["building"], "presence_entity": PRESENCE}
    hass.config_entries.async_update_entry(
        central, options={**central.options, "building": building}
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    zone = await add_zone(hass, zone_options("west", "climate.west_ac"))
    climate_id = er.async_get(hass).async_get_entity_id(
        "climate", DOMAIN, f"{zone.entry_id}_thermostat"
    )
    assert hass.states.get(climate_id).attributes["preset_mode"] == "comfort"
    hass.states.async_set(PRESENCE, "off")
    await hass.async_block_till_done()
    thermostat = hass.states.get(climate_id)
    assert thermostat.attributes["preset_mode"] == "away"
    assert thermostat.attributes["chosen_preset"] == "comfort"
    assert (
        thermostat.attributes["target_temp_low"],
        thermostat.attributes["target_temp_high"],
    ) == (16, 30)
    hass.states.async_set(PRESENCE, "on")
    await hass.async_block_till_done()
    assert hass.states.get(climate_id).attributes["preset_mode"] == "comfort"
