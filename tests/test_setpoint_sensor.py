"""Presentation of calculated setpoints for rooms with multiple indoor units."""

from types import SimpleNamespace

import pytest

from custom_components.zonentra.sensor import RegulatedSetpointSensor
from custom_components.zonentra.thermostat import SetpointPlan


@pytest.mark.parametrize("status", ["in_sync", "interval", "setpoint_changed"])
def test_prefer_calculated_target_over_inactive_unit(status):
    units = [
        SimpleNamespace(id=name, zone_id="west", kind="hvac")
        for name in ("first", "second")
    ]
    runtime = SimpleNamespace(
        building=SimpleNamespace(equipment=units),
        zone_demands={"west": None},
        active_capable=True,
        setpoint_plans={
            "first": SetpointPlan("unit_not_running"),
            "second": SetpointPlan(status, 23, 22, 1),
        },
        regulated=lambda _: False,
    )
    entry = SimpleNamespace(
        entry_id="test", data={"name": "West"}, runtime_data=runtime
    )
    sensor = RegulatedSetpointSensor(entry, SimpleNamespace(id="west"))
    assert sensor.available
    assert sensor.native_value == 23
    assert sensor.extra_state_attributes == {
        "status": status,
        "base": 22,
        "offset": 1,
        "room_regulation": False,
    }
    runtime.setpoint_plans["second"] = SetpointPlan("manual_override")
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["status"] == "unit_not_running"
    runtime.setpoint_plans.clear()
    assert not sensor.available
    assert sensor.extra_state_attributes["status"] == "no_active_setpoint"
    entry.runtime_data = None
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["room_regulation"] is None
