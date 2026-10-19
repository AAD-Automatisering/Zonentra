"""Office and booking exports explain the evidence and current learning gates."""

import json
from datetime import timedelta

import pytest
from homeassistant.core import Context
from homeassistant.util import dt as dt_util

from custom_components.zonentra.bookings import ThermalRates, ThermalSample, learn_rate
from custom_components.zonentra.diagnostics import async_get_config_entry_diagnostics
from tests.test_bookings_runtime import office as booking_office
from tests.test_entry_layout import CELSIUS
from tests.test_office_schedule import SCHEDULE, office
from tests.test_thermostat_runtime import later, mock_climate


async def test_office_exports_current_quality_even_without_a_learned_profile(hass):
    central, room, _ = await office(hass, start=20)
    for entry in (central, room):
        data = await async_get_config_entry_diagnostics(hass, entry)
        record = data["office_schedule"][0]
        assert record["learned_profiles"] == {}
        assert record["learning"]["outdoor_band"] == "mild"
        assert record["learning"]["profile_quality"]["heating"]["status"] == "untrained"
        assert record["learning"]["minimum_samples"] == 4
        assert "room_regulation_disabled" in record["learning"]["blockers"]
        assert record["preconditioning_settings"]["heating_rate_c_per_minute"] == 0.05
        assert record["preconditioning_settings"]["buffer_minutes"] == 10
        exported = json.dumps(data)
        assert SCHEDULE not in exported
        assert "sensor.west_temperature" not in exported
        assert "climate.west_ac" not in exported
        assert record["zone_id"] == "**REDACTED**"
        assert "temperature" not in (record["learning"].get("last_sample") or {})


@pytest.mark.parametrize(
    "count,age,status",
    [(1, 0, "insufficient_samples"), (4, 0, "reliable"), (4, 31, "expired")],
)
async def test_export_explains_why_a_profile_is_not_yet_used(hass, count, age, status):
    central, room, _ = await office(hass, start=20)
    runtime = central.runtime_data
    runtime.thermal_rates["west"] = {
        "mild": ThermalRates(
            heating=0.04,
            heating_samples=count,
            heating_updated=(runtime.evaluated_at - timedelta(days=age)).isoformat(),
        )
    }
    record = (await async_get_config_entry_diagnostics(hass, room))["office_schedule"][
        0
    ]
    assert record["learned_profiles"]["mild"]["heating"] == 0.04
    assert record["learning"]["profile_quality"]["heating"]["status"] == status
    assert record["learning"]["profile_quality"]["heating"]["samples"] == count


async def test_knx_idle_is_distinguished_from_missing_temperature_reports(hass):
    await mock_climate(hass)
    central, room, _ = await office(hass, start=20)
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    hass.states.async_set(
        "climate.west_ac",
        "heat",
        {"hvac_action": "idle", "temperature": 22},
        context=Context(parent_id="test"),
    )
    await hass.async_block_till_done()
    record = (await async_get_config_entry_diagnostics(hass, room))["office_schedule"][
        0
    ]
    learning = record["learning"]
    assert learning["reason"] == "no_active_heating_or_cooling"
    assert learning["reported_inactive_units"] == 1
    assert learning["activity_source"] == "reported_action"
    assert not learning["eligible"]
    hass.states.async_set(
        "climate.west_ac",
        "heat",
        {"hvac_action": "heating", "temperature": 22},
        context=Context(parent_id="test"),
    )
    await hass.async_block_till_done()
    runtime.refresh()
    record = (await async_get_config_entry_diagnostics(hass, room))["office_schedule"][
        0
    ]
    assert record["learning"]["eligible"]
    assert record["learning"]["reason"] in (
        "waiting_for_temperature_report",
        "waiting_for_fresh_temperature_report",
    )


async def test_sample_result_survives_timer_refresh_and_exports_no_raw_temperature(
    hass, freezer
):
    await mock_climate(hass)
    central, room, _ = await office(hass, start=20, temperature="20")
    runtime = central.runtime_data
    runtime.set_thermostat("west", regulation=True)
    hass.states.async_set(
        "climate.west_ac",
        "heat",
        {"hvac_action": "heating", "temperature": 22},
        context=Context(parent_id="test"),
    )
    await hass.async_block_till_done()
    hass.states.async_set("sensor.west_temperature", "20", CELSIUS)
    runtime.refresh()
    await later(hass, freezer, minutes=10)
    hass.states.async_set("sensor.west_temperature", "20.5", CELSIUS)
    await hass.async_block_till_done()
    runtime.refresh()
    record = (await async_get_config_entry_diagnostics(hass, room))["office_schedule"][
        0
    ]
    assert record["learned_profiles"]["mild"]["heating_samples"] == 1
    assert record["learning"]["last_sample"]["reason"] == "sample_recorded"
    assert record["learning"]["reason"] == "waiting_for_temperature_report"
    assert record["learning"]["last_sample"]["window_minutes"] == 10
    assert "temperature" not in record["learning"]["last_sample"]


async def test_bookings_receive_the_same_learning_explanation(hass):
    central, room, _ = await booking_office(hass)
    record = (await async_get_config_entry_diagnostics(hass, room))["bookings"][0]
    assert "learned_profiles" in record
    assert record["learning"]["profile_quality"]["heating"]["status"] == "untrained"
    assert "room_regulation_disabled" in record["learning"]["blockers"]


@pytest.mark.parametrize(
    "minutes,change,reason",
    [
        (0, 0, "non_increasing_timestamp"),
        (2, 0.1, "sample_too_short"),
        (31, 0.5, "sample_gap_too_long"),
        (10, 0, "no_temperature_change"),
        (10, -1, "rate_out_of_range"),
        (10, 0.5, "sample_recorded"),
    ],
)
def test_sample_explanation_does_not_change_learning_result(minutes, change, reason):
    now = dt_util.utcnow()
    sample = ThermalSample(now, 20, "heating", "mild")
    args = (
        ThermalRates(),
        sample,
        now + timedelta(minutes=minutes),
        20 + change,
        "heating",
        15,
    )
    explanation = {}
    assert learn_rate(*args, diagnostics=explanation) == learn_rate(*args)
    assert explanation["reason"] == reason
