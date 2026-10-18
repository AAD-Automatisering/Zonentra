"""0.12.0 integration: the hourly forecast saves energy on warm and cold days."""

from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.zonentra.const import DOMAIN
from tests.test_entry_layout import CELSIUS, add_zone, setup_central, zone_options
from tests.test_thermostat_runtime import later

WEATHER = "weather.home"
SCREEN = "cover.west_screen"
EQUIPMENT = "shade_west_screen"


def entity(hass, platform, unique_id):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def forecast(*temperatures):
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    return {
        WEATHER: {
            "forecast": [
                {
                    "datetime": (start + timedelta(hours=hour)).isoformat(),
                    "temperature": temperature,
                }
                for hour, temperature in enumerate(temperatures)
            ]
        }
    }


def weather(hass, *temperatures, error=None):
    return async_mock_service(
        hass,
        "weather",
        "get_forecasts",
        response=forecast(*temperatures),
        supports_response="only",
        raise_exception=error,
    )


async def office(hass, temperature="22.5", **building):
    """West zone (comfort 22-24) with a south screen in the sun."""
    if hass.states.get(WEATHER) is None:
        hass.states.async_set(WEATHER, "sunny")
    hass.states.async_set("sensor.west_temperature", temperature, CELSIUS)
    hass.states.async_set("sensor.solar_power", "2500", {"unit_of_measurement": "W"})
    hass.states.async_set("sun.sun", "above_horizon", {"azimuth": 180, "elevation": 50})
    hass.states.async_set(SCREEN, "open", {"current_position": 100})
    central = await setup_central(hass)
    settings = {
        **central.options["building"],
        "sun_power_entity": "sensor.solar_power",
        "weather_entity": WEATHER,
        **building,
    }
    hass.config_entries.async_update_entry(
        central, options={**central.options, "building": settings}
    )
    assert await hass.config_entries.async_reload(central.entry_id)
    await hass.async_block_till_done()
    zone = await add_zone(
        hass, zone_options("west", shading_entities=[SCREEN], facade="south")
    )
    return central, zone


async def test_warm_day_shades_early_and_delays_heating(hass, freezer):
    async_mock_service(hass, "cover", "set_cover_position")
    calls = weather(hass, 17, 19, 22, 25, 26, 24, 21)
    central, zone = await office(hass)
    runtime = central.runtime_data
    assert calls[0].data == {"entity_id": WEATHER, "type": "hourly"}
    assert runtime.day_type == "warm"
    maximum = hass.states.get(
        entity(hass, "sensor", f"{central.entry_id}_forecast_maximum")
    )
    minimum = hass.states.get(
        entity(hass, "sensor", f"{central.entry_id}_forecast_minimum")
    )
    day = hass.states.get(entity(hass, "sensor", f"{central.entry_id}_day_type"))
    assert (float(maximum.state), float(minimum.state), day.state) == (26, 17, "warm")
    assert day.attributes["heating_start_margin"] == 1.0

    # Sunshine after 5 minutes: 22.5 °C is the cool half of 22-24, yet the
    # screen goes down before the warm afternoon.
    await later(hass, freezer, minutes=6)
    assert runtime.screen_decisions[EQUIPMENT].reason == "warm_day_forecast"

    # 21.5 °C is below 22 - 0.3, but on a warm day heating waits 1 °C longer.
    hass.states.async_set("sensor.west_temperature", "21.5", CELSIUS)
    await hass.async_block_till_done()
    demand = runtime.zone_demands["west"]
    assert (demand.demand, demand.reason) == ("neutral", "heating_deferred_warm_day")
    hass.states.async_set("sensor.west_temperature", "20.6", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].demand == "heating_demand"


async def test_cold_day_lets_the_sun_in_and_delays_cooling(hass, freezer):
    async_mock_service(hass, "cover", "set_cover_position")
    weather(hass, 4, 6, 9, 11, 10)
    central, _ = await office(hass, temperature="23.6")
    runtime = central.runtime_data
    assert runtime.day_type == "cold"
    await later(hass, freezer, minutes=6)
    # 23.6 °C is the warm half, but on a cold day the solar heat is welcome.
    assert runtime.screen_decisions[EQUIPMENT].reason == "cold_day_forecast"
    hass.states.async_set("sensor.west_temperature", "24.6", CELSIUS)
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].reason == "cooling_deferred_cold_day"


async def test_thresholds_on_the_central_device(hass):
    weather(hass, 15, 18, 20)
    central, _ = await office(hass)
    runtime = central.runtime_data
    assert runtime.day_type == "neutral"
    number_id = entity(hass, "number", f"{central.entry_id}_warm_day_temperature")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": number_id, "value": 19.5}, blocking=True
    )
    await hass.async_block_till_done()
    assert central.options["building"]["warm_day_temperature"] == 19.5
    assert runtime.day_type == "warm"


async def test_without_forecast_everything_works_as_before(hass, freezer):
    async_mock_service(hass, "cover", "set_cover_position")
    weather(hass, error=HomeAssistantError("no forecast"))
    central, _ = await office(hass)
    runtime = central.runtime_data
    assert runtime.day_type == "unknown"
    assert runtime.forecast_error == "HomeAssistantError"
    day = hass.states.get(entity(hass, "sensor", f"{central.entry_id}_day_type"))
    assert day.state == "unknown"
    await later(hass, freezer, minutes=6)
    assert runtime.screen_decisions[EQUIPMENT].reason == "room_cool"

    # The weather integration loads later: the retry picks the forecast up.
    weather(hass, 20, 23)
    await later(hass, freezer, minutes=3)
    assert runtime.day_type == "warm"


async def test_no_weather_entity_no_forecast_entities(hass):
    central = await setup_central(hass)
    assert entity(hass, "sensor", f"{central.entry_id}_day_type") is None
    assert entity(hass, "number", f"{central.entry_id}_forecast_margin") is None
    assert central.runtime_data.day_type == "unknown"


async def test_weather_entity_unit_overrides_system_unit(hass):
    weather(hass, 41, 50)
    hass.states.async_set(WEATHER, "cloudy", {"temperature_unit": "°F"})
    central, _ = await office(hass)
    assert central.runtime_data.forecast.maximum == 10
    assert central.runtime_data.day_type == "cold"


async def test_forecast_without_entity_unit_uses_system_unit(hass):
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    hass.config.units = US_CUSTOMARY_SYSTEM
    weather(hass, 41, 50)
    central, _ = await office(hass)
    assert central.runtime_data.forecast.maximum == 10
    assert central.runtime_data.day_type == "cold"


async def test_invalid_weather_unit_disables_forecast_policy(hass):
    weather(hass, 41, 50)
    hass.states.async_set(WEATHER, "cloudy", {"temperature_unit": "unsupported"})
    central, _ = await office(hass)
    assert central.runtime_data.day_type == "unknown"
    assert central.runtime_data.forecast_error == "invalid_forecast"


@pytest.mark.parametrize(
    "forecast_temperatures,room_temperature,expected_demand,expected_deferral",
    [
        ((25, 26, 27), "21.5", "heating_demand", "heating_deferred_warm_day"),
        ((4, 6, 9), "24.5", "cooling_demand", "cooling_deferred_cold_day"),
    ],
)
async def test_manual_target_overrides_forecast_only_until_it_expires(
    hass,
    freezer,
    forecast_temperatures,
    room_temperature,
    expected_demand,
    expected_deferral,
):
    weather(hass, *forecast_temperatures)
    central, zone = await office(hass, temperature=room_temperature)
    runtime = central.runtime_data
    assert runtime.zone_demands["west"].reason == expected_deferral
    climate_id = entity(hass, "climate", f"{zone.entry_id}_thermostat")
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate_id, "target_temp_low": 22, "target_temp_high": 24},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert runtime.thermostat("west").manual
    assert runtime.zone_demands["west"].demand == expected_demand
    # A manual target never bypasses source validation.
    hass.states.async_set("sensor.west_temperature", "unavailable")
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].demand == "unknown"
    hass.states.async_set("sensor.west_temperature", room_temperature, CELSIUS)
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].demand == expected_demand
    # Stop the current hysteresis cycle before the override expires.
    hass.states.async_set("sensor.west_temperature", "23", CELSIUS)
    await hass.async_block_till_done()
    await later(hass, freezer, minutes=31)
    assert not runtime.thermostat("west").manual
    hass.states.async_set("sensor.west_temperature", room_temperature, CELSIUS)
    await hass.async_block_till_done()
    assert runtime.zone_demands["west"].reason == expected_deferral
