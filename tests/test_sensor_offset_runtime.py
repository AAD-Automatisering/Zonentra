"""Verify that paired reports drive real commands and expire without new events."""

from datetime import timedelta

import pytest

from tests.test_entry_layout import CELSIUS
from tests.test_review_logic_fixes import standalone
from tests.test_thermostat_runtime import UNIT, clear, later, mock_climate

ROOM_SENSOR = "sensor.review_temperature"


@pytest.mark.parametrize(
    ("mode", "action", "temperature", "difference"),
    [("heat", "heating", 21.6, 1.5), ("cool", "cooling", 24.4, -1.5)],
)
async def test_stable_offset_steers_setpoint_and_expires(
    hass, freezer, mode, action, temperature, difference
):
    mocks = await mock_climate(hass)
    runtime = standalone(hass, mode=mode, temperature=str(temperature))
    runtime.set_thermostat("west", regulation=True)

    async def report(bias, *, minutes=0, unit_report=True, reported_action=action):
        freezer.tick(timedelta(minutes=minutes))
        hass.states.async_set(ROOM_SENSOR, str(temperature), CELSIUS, force_update=True)
        if unit_report:
            state = hass.states.get(UNIT)
            hass.states.async_set(
                UNIT,
                mode,
                {
                    **state.attributes,
                    "current_temperature": temperature + bias,
                    "hvac_action": reported_action,
                },
                force_update=True,
            )
        runtime.refresh()
        await hass.async_block_till_done()

    def confirm_target():
        if calls := mocks["set_temperature"]:
            state = hass.states.get(UNIT)
            hass.states.async_set(
                UNIT,
                mode,
                {**state.attributes, "temperature": calls[-1].data["temperature"]},
            )
            runtime.refresh()

    await report(difference)
    confirm_target()
    initial = runtime.setpoint_plans["ac"].setpoint
    clear(mocks)
    await report(difference, minutes=5)
    await report(difference, minutes=5, unit_report=False)
    assert runtime.sensor_offsets["ac"].summary()["profiles"][mode]["samples"] == 1
    for _ in range(4):
        await report(difference, minutes=5)
    assert not mocks["set_temperature"]
    assert runtime.sensor_offsets["ac"].correction == 0
    await report(difference, minutes=5)
    expected = initial + (1 if difference > 0 else -1)
    assert runtime.setpoint_plans["ac"].setpoint == expected
    assert mocks["set_temperature"][-1].data["temperature"] == expected
    confirm_target()
    await report(0, minutes=5, reported_action="idle")
    assert runtime.setpoint_plans["ac"].setpoint == expected
    clear(mocks)
    await later(hass, freezer, minutes=20)
    assert runtime.sensor_offsets["ac"].status == "waiting_for_fresh_pair"
    assert runtime.setpoint_plans["ac"].setpoint == initial
    assert mocks["set_temperature"][-1].data["temperature"] == initial
    runtime.close()


async def test_inconsistent_pairs_do_not_change_target(hass, freezer):
    await mock_climate(hass)
    runtime = standalone(hass, temperature="21.6")
    runtime.set_thermostat("west", regulation=True)
    for difference in (0, 2, -1, 2, -1, 2, -1):
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(ROOM_SENSOR, "21.6", CELSIUS, force_update=True)
        state = hass.states.get(UNIT)
        hass.states.async_set(
            UNIT, "heat", {**state.attributes, "current_temperature": 21.6 + difference}
        )
        runtime.refresh()
        await hass.async_block_till_done()
    offset = runtime.sensor_offsets["ac"]
    assert offset.status == "inconsistent"
    assert offset.correction == 0
    runtime.close()
