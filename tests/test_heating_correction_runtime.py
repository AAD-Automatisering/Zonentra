"""Real setpoint commands react to sustained warming and respect room guards."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.zonentra.override import ManualOverride
from custom_components.zonentra.runtime import BuildingRuntime
from tests.test_entry_layout import CELSIUS
from tests.test_review_logic_fixes import standalone
from tests.test_thermostat_runtime import UNIT, clear, later, mock_climate


async def report(hass, freezer, runtime, temperature="21.1", **delta):
    freezer.tick(timedelta(**delta))
    hass.states.async_set(
        "sensor.review_temperature", temperature, CELSIUS, force_update=True
    )
    runtime.refresh()
    await hass.async_block_till_done()
    # Model confirmation of the exact temperature the mock climate service got.
    for entry in reversed(runtime.command_log):
        if entry["service"] == "set_temperature" and entry["result"] == "sent":
            current = hass.states.get(UNIT)
            hass.states.async_set(
                UNIT,
                current.state,
                {**current.attributes, "temperature": entry["temperature"]},
            )
            runtime.refresh()
            await hass.async_block_till_done()
            break


async def test_plateau_changes_real_airco_setpoint_without_floor_commands(
    hass, freezer
):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, temperature="21.1")
    runtime.set_thermostat("west", regulation=True)
    await report(hass, freezer, runtime)
    original = runtime.setpoint_plans["ac"].setpoint
    clear(mocks)
    for _ in range(8):
        await report(hass, freezer, runtime, minutes=5)
    assert runtime.heating_corrections["ac"].extra == 0
    await report(hass, freezer, runtime, minutes=5)
    assert runtime.heating_corrections["ac"].extra == 0.5
    assert runtime.setpoint_plans["ac"].setpoint == original + 0.5
    assert any(
        c.data["temperature"] == original + 0.5 for c in mocks["set_temperature"]
    )
    assert all(c.data["entity_id"] == UNIT for c in mocks["set_temperature"])
    # A timer with no new source report cannot accumulate another step.
    await later(hass, freezer, minutes=19)
    assert runtime.heating_corrections["ac"].extra == 0.5
    await later(hass, freezer, minutes=1)
    assert runtime.heating_corrections["ac"].extra == 0
    runtime.close()


@pytest.mark.parametrize(
    "guard",
    ["override", "idle", "cool", "disabled", "native", "eco", "invalid", "window"],
)
async def test_room_guards_reset_assistance(hass, freezer, guard):
    await mock_climate(hass)
    runtime = standalone(hass, temperature="21.1")
    runtime.set_thermostat("west", regulation=True)
    for _ in range(10):
        await report(hass, freezer, runtime, minutes=5)
    assert runtime.heating_corrections["ac"].extra == 0.5
    if guard == "override":
        now = dt_util.utcnow()
        runtime.overrides["ac"] = ManualOverride(
            "ac", now, now + timedelta(minutes=30), "heat"
        )
    elif guard in ("idle", "cool"):
        state = hass.states.get(UNIT)
        hass.states.async_set(
            UNIT,
            "cool" if guard == "cool" else "heat",
            {
                **state.attributes,
                "hvac_action": "cooling" if guard == "cool" else "idle",
            },
        )
    elif guard == "disabled":
        runtime.set_thermostat("west", regulation=False)
    elif guard == "native":
        runtime.building = replace(
            runtime.building,
            zones=tuple(
                replace(z, native_regulation=True) for z in runtime.building.zones
            ),
        )
    elif guard == "eco":
        runtime.set_thermostat("west", preset="eco")
    elif guard == "window":
        hass.states.async_set("binary_sensor.review_window", "on")
        runtime.building = replace(
            runtime.building,
            settings=replace(runtime.building.settings, window_delay_seconds=0),
            zones=tuple(
                replace(z, contact_entities=("binary_sensor.review_window",))
                for z in runtime.building.zones
            ),
        )
    elif guard == "invalid":
        hass.states.async_set("sensor.review_temperature", "unavailable")
    runtime.refresh()
    assert runtime.heating_corrections["ac"].extra == 0
    runtime.close()


async def test_reload_preserves_observation_but_changed_source_resets_it(hass, freezer):
    await mock_climate(hass)
    runtime = standalone(hass, temperature="21.1")
    runtime.set_thermostat("west", regulation=True)
    for _ in range(10):
        await report(hass, freezer, runtime, minutes=5)
    assert runtime.heating_corrections["ac"].extra == 0.5
    replacement = BuildingRuntime(hass, runtime.building, active=True)
    replacement.adopt(runtime)
    # Real entry setup restores the separately persisted thermostats as well.
    replacement.thermostats = dict(runtime.thermostats)
    runtime.close()
    replacement.refresh()
    assert replacement.heating_corrections["ac"].extra == 0.5
    hass.states.async_set("sensor.replaced_temperature", "21.1", CELSIUS)
    replacement.building = replace(
        replacement.building,
        zones=tuple(
            replace(z, temperature_entity="sensor.replaced_temperature")
            for z in replacement.building.zones
        ),
    )
    replacement.refresh()
    assert replacement.heating_corrections["ac"].extra == 0
    replacement.close()
