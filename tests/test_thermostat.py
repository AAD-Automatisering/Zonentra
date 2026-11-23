"""0.9.0 pure tests: room thermostat, band choice and room regulation."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.zonentra.control import standalone_coordination, unit_intent
from custom_components.zonentra.demand import ZoneDemand, collect_zone, comfort_limits
from custom_components.zonentra.indoor import IndoorUnitObservation
from custom_components.zonentra.models import BuildingSettings, build_model
from custom_components.zonentra.observations import SourceState
from custom_components.zonentra.override import ManualOverride, update_override
from custom_components.zonentra.presence import Occupancy
from custom_components.zonentra.thermostat import (
    Thermostat,
    active_band,
    plan_fan,
    plan_room_mode,
    plan_room_unit,
    plan_setpoint,
    room_demand,
    safe_modes,
    single_target,
    update_thermostat,
)

NOW = datetime(2026, 10, 14, 16, tzinfo=UTC)


# --- Thermostat state ---------------------------------------------------------


def test_defaults_keep_0_8_behaviour():
    thermostat = Thermostat()
    assert thermostat.hvac_mode == "heat_cool"
    assert thermostat.preset == "comfort"
    assert thermostat.regulation is False  # Room regulation is off by default.
    assert not thermostat.has_band


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, Thermostat()),
        ("garbage", Thermostat()),
        (
            {
                "hvac_mode": "off",
                "preset": "eco",
                "heating_limit": 20,
                "cooling_limit": 25,
                "regulation": True,
            },
            Thermostat("off", "eco", 20.0, 25.0, True),
        ),
        ({"hvac_mode": "auto", "preset": "boost"}, Thermostat()),
        ({"heating_limit": 20}, Thermostat()),  # Half a band is no band.
        ({"heating_limit": 26, "cooling_limit": 22}, Thermostat()),
        ({"heating_limit": 2, "cooling_limit": 25}, Thermostat()),
        ({"regulation": "yes"}, Thermostat()),
    ],
)
def test_restore_is_tolerant(stored, expected):
    assert Thermostat.from_dict(stored) == expected


def test_round_trip():
    thermostat = Thermostat("off", "comfort", 20.5, 24.5, True)
    assert Thermostat.from_dict(thermostat.as_dict()) == thermostat


def test_valid_changes():
    thermostat = update_thermostat(
        Thermostat(), 0.3, heating_limit=20, cooling_limit=24.5
    )
    assert (thermostat.heating_limit, thermostat.cooling_limit) == (20, 24.5)
    thermostat = update_thermostat(thermostat, 0.3, preset="eco", regulation=True)
    assert thermostat.preset == "eco" and thermostat.regulation
    assert update_thermostat(thermostat, 0.3, hvac_mode="off").hvac_mode == "off"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"heating_limit": 22, "cooling_limit": 22.5}, "invalid_limits"),  # < 2*hyst
        ({"heating_limit": 24, "cooling_limit": 21}, "invalid_limits"),
        ({"heating_limit": 4, "cooling_limit": 24}, "invalid_limits"),
        ({"heating_limit": 20, "cooling_limit": 36}, "invalid_limits"),
        ({"heating_limit": 20}, "invalid_limits"),
        ({"hvac_mode": "auto"}, "invalid_hvac_mode"),
        ({"preset": "boost"}, "invalid_preset"),
        ({"regulation": "on"}, "invalid_parameters"),
    ],
)
def test_invalid_changes(changes, code):
    with pytest.raises(ValueError, match=code):
        update_thermostat(Thermostat(), 0.3, **changes)


# --- Band choice --------------------------------------------------------------

OCCUPIED = Occupancy("comfort", "motion", "present", "active")
NO_MOTION = Occupancy("eco", "no_motion", "present", "inactive")
ABSENT = Occupancy("away", "building_absent", "absent", "active")


@pytest.mark.parametrize(
    ("preset", "occupancy", "expected"),
    [
        # Comfort follows presence and motion (eco without motion, away).
        ("comfort", OCCUPIED, ("comfort", "motion")),
        ("comfort", NO_MOTION, ("eco", "no_motion")),
        ("eco", OCCUPIED, ("eco", "preset")),
        ("away", OCCUPIED, ("away", "preset")),
        # Building absence always wins, also over a fixed preset.
        ("comfort", ABSENT, ("away", "building_absent")),
        ("away", NO_MOTION, ("away", "preset")),
        ("eco", ABSENT, ("away", "building_absent")),
    ],
)
def test_active_band(preset, occupancy, expected):
    assert active_band(preset, occupancy) == expected


def zone_model(**zone):
    return build_model(
        "b",
        "B",
        {
            "building": {"heating_limit": 21, "cooling_limit": 24},
            "zones": [
                {
                    "id": "room",
                    "name": "Room",
                    "temperature_entity": "sensor.room",
                    "use_building_limits": True,
                    **zone,
                }
            ],
        },
    )


def test_thermostat_band_replaces_comfort_but_not_eco_or_away():
    building = zone_model()
    zone = building.zones[0]
    sources = {"sensor.room": SourceState("20", "°C", NOW)}
    configured = collect_zone(building, zone, sources, NOW)
    assert (configured.heating_limit.value, configured.cooling_limit.value) == (21, 24)
    own = collect_zone(building, zone, sources, NOW, comfort=(19.5, 25))
    assert (own.heating_limit.value, own.cooling_limit.value) == (19.5, 25)
    eco = collect_zone(building, zone, sources, NOW, band="eco", comfort=(19.5, 25))
    assert (eco.heating_limit.value, eco.cooling_limit.value) == (19, 26)
    heat, cool = comfort_limits(zone, sources, NOW)
    assert (heat.value, cool.value) == (21, 24)


def test_thermostat_band_gives_a_zone_without_limits_a_demand():
    building = build_model(
        "b",
        "B",
        {
            "zones": [
                {"id": "room", "name": "Room", "temperature_entity": "sensor.room"}
            ]
        },
    )
    zone = building.zones[0]
    sources = {"sensor.room": SourceState("20", "°C", NOW)}
    assert collect_zone(building, zone, sources, NOW).heating_limit.quality != "valid"
    own = collect_zone(building, zone, sources, NOW, comfort=(21, 24))
    assert own.heating_limit.quality == "valid"


def test_zone_without_own_limits_keeps_the_central_ones():
    # 0.10: static limits are set on the device; the wizard only has entities.
    zone = zone_model(use_building_limits=False).zones[0]
    assert (zone.heating_limit, zone.cooling_limit) == (21, 24)


def test_room_bands_win_over_the_central_ones():
    building = zone_model()
    zone = building.zones[0]
    sources = {"sensor.room": SourceState("20", "°C", NOW)}
    eco = collect_zone(
        building,
        zone,
        sources,
        NOW,
        band="eco",
        bands={"eco_heating_limit": 18},
    )
    assert (eco.heating_limit.value, eco.cooling_limit.value) == (18, 26)


def test_room_band_limits_are_stored_and_validated():
    thermostat = update_thermostat(Thermostat(), 0.3, eco_heating_limit=18.5)
    assert thermostat.bands == {"eco_heating_limit": 18.5}
    restored = Thermostat.from_dict(thermostat.as_dict())
    assert restored.eco_heating_limit == 18.5 and restored.away_cooling_limit is None
    with pytest.raises(ValueError, match="invalid_limits"):
        update_thermostat(Thermostat(), 0.3, away_cooling_limit=40)
    cleared = update_thermostat(thermostat, 0.3, eco_heating_limit=None)
    assert cleared.bands == {}


# --- Room regulation: switching on and off ------------------------------------


def room(action, reason="zone_demand", **kwargs):
    defaults = {"enabled": True, "safe_modes": frozenset({"heat"}), "busy": False}
    return plan_room_unit(action, reason, "unit", "climate.unit", **defaults | kwargs)


def test_regulation_off_is_simulation_only():
    control = room("turn_on_heat", enabled=False)
    assert (control.status, control.command) == ("simulation", None)


def test_switch_on_in_the_outdoor_mode():
    control = room("turn_on_heat")
    assert control.status == "turn_on_heat"
    command = control.command
    assert (command.service, command.hvac_mode, command.reason) == (
        "set_hvac_mode",
        "heat",
        "room_demand",
    )


def test_never_switch_on_against_the_outdoor_mode():
    control = room("turn_on_cool")
    assert (control.status, control.command) == ("blocked_outdoor_mode", None)


def test_switch_off_and_wait_for_confirmation():
    control = room("turn_off", "no_zone_demand")
    assert (control.command.service, control.command.reason) == (
        "turn_off",
        "no_zone_demand",
    )
    assert room("turn_off", busy=True).status == "waiting_for_confirmation"
    assert room("turn_on_heat", busy=True).status == "waiting_for_confirmation"


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("hold", "hold"),
        ("blocked", "blocked"),
        ("correct_mode", "left_to_stand_control"),
    ],
)
def test_other_actions_send_nothing(action, status):
    control = room(action)
    assert (control.status, control.command) == (status, None)


@pytest.mark.parametrize(
    ("running", "preset", "expected"),
    [
        ((), "heat", {"heat"}),  # Active stand control: the preset decides.
        (("cool",), "heat", {"heat"}),
        ((), None, {"heat", "cool"}),  # Nothing running: free.
        (("heat", "off"), None, {"heat"}),
        (("dry",), None, {"cool"}),
        (("heat", "cool"), None, set()),  # Conflict: never add a side.
        (("fan_only",), None, {"heat", "cool"}),
    ],
)
def test_safe_modes(running, preset, expected):
    assert safe_modes(running, preset) == frozenset(expected)


# --- Self-regulating setpoint -------------------------------------------------


def setpoint(mode="heat", room_temperature=20.0, current=22.0, last=None, **kwargs):
    defaults = {"max_offset": 2.0, "interval_seconds": 300}
    return plan_setpoint(
        mode,
        21.0,
        24.0,
        0.3,
        room_temperature,
        current,
        last,
        NOW,
        **defaults | kwargs,
    )


def test_heat_setpoint_adds_the_room_error():
    plan = setpoint(room_temperature=20.0)  # base 21.3, error 1.3 -> 22.6 -> 22.5
    assert (plan.base, plan.setpoint, plan.status, plan.send) == (
        21.3,
        22.5,
        "setpoint_changed",
        True,
    )


def test_heat_setpoint_is_bounded_and_lowers_when_warm():
    assert setpoint(room_temperature=15.0).setpoint == 23.5  # 21.3 + max 2
    assert setpoint(room_temperature=22.3).setpoint == 20.5  # 21.3 - 1.0


def test_cool_setpoint_subtracts_the_room_error():
    plan = setpoint("cool", room_temperature=25.0, current=22.0)
    assert (plan.base, plan.setpoint) == (23.7, 22.5)  # 23.7 - 1.3 = 22.4 -> 22.5
    assert setpoint("cool", room_temperature=30.0).setpoint == 21.5  # max 2


def test_learned_sensor_offset_and_heating_assistance_share_setpoint_limit():
    heat = setpoint(room_temperature=21.0, heating_extra=1.0, sensor_offset=1.0)
    cool = setpoint("cool", room_temperature=25.0, sensor_offset=-1.0)
    assert heat.setpoint == 23.5
    assert cool.setpoint == 21.5


def test_unit_step_and_limits():
    assert setpoint(room_temperature=20.0, step=1).setpoint == 23.0
    assert setpoint(room_temperature=15.0, max_temp=23).setpoint == 23
    assert setpoint("cool", room_temperature=30.0, min_temp=22).setpoint == 22


def test_in_sync_and_interval():
    assert setpoint(current=22.5).status == "in_sync"
    plan = setpoint(last=NOW - timedelta(minutes=2))
    assert (plan.status, plan.send) == ("interval", False)
    assert plan.next_check == NOW + timedelta(minutes=3)
    assert setpoint(last=NOW - timedelta(minutes=6)).send


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"mode": "off"}, "unit_not_running"),
        ({"mode": "dry"}, "unit_not_running"),
        ({"current": None}, "no_setpoint_support"),
        ({"room_temperature": None}, "room_temperature_unknown"),
    ],
)
def test_nothing_to_steer(kwargs, status):
    plan = setpoint(**kwargs)
    assert (plan.status, plan.send) == (status, False)


def test_setpoint_settings_are_validated():
    with pytest.raises(ValueError, match="invalid_parameters"):
        build_model("b", "B", {"building": {"setpoint_max_offset": 6}})
    with pytest.raises(ValueError, match="invalid_parameters"):
        build_model("b", "B", {"building": {"setpoint_interval_seconds": 10}})
    defaults = BuildingSettings()
    assert (defaults.setpoint_max_offset, defaults.setpoint_interval_seconds) == (
        2.0,
        300.0,
    )


# --- Manual override in a regulated room --------------------------------------


def override(transition, regulated, current=None, state="heat"):
    return update_override(
        current,
        transition,
        "unit",
        state,
        NOW,
        enabled=True,
        duration_minutes=30,
        regulated=regulated,
    )


def test_manual_off_is_respected_only_in_a_regulated_room():
    held, event = override("manual_off", True, state="off")
    assert event == "override_started"
    assert held.mode_at_start == "off"
    assert held.expires_at == NOW + timedelta(minutes=30)
    assert override("manual_off", False, state="off") == (None, None)


def test_manual_setpoint_starts_an_override_only_when_regulated():
    held, event = override("manual_setpoint", True)
    assert (event, held.mode_at_start) == ("override_started", "heat")
    assert override("manual_setpoint", False) == (None, None)


def test_overrides_disabled_centrally():
    assert update_override(
        None,
        "manual_off",
        "unit",
        "off",
        NOW,
        enabled=False,
        duration_minutes=30,
        regulated=True,
    ) == (None, "override_disabled")


def test_switched_off_unit_is_held_off():
    unit = IndoorUnitObservation(
        "unit",
        "room",
        None,
        "available",
        "off",
        "off",
        None,
        None,
        None,
        None,
        None,
        "complete",
        False,
    )
    demand = ZoneDemand("room", "heating_demand", 1.0, 0.0, "below", "valid")
    hold = ManualOverride("unit", NOW, NOW + timedelta(minutes=30), "off")
    intent = unit_intent(
        unit,
        NOW - timedelta(hours=1),
        demand,
        standalone_coordination("unit", demand),
        hold,
        (),
        BuildingSettings(),
        NOW,
    )
    assert (intent.action, intent.reason) == ("hold", "manual_override_off")
    intent = unit_intent(
        unit,
        NOW - timedelta(hours=1),
        demand,
        standalone_coordination("unit", demand),
        None,
        (),
        BuildingSettings(),
        NOW,
    )
    assert intent.action == "turn_on_heat"


# --- Standard Home Assistant modes: heat, cool, heat_cool, off ----------------


@pytest.mark.parametrize(
    "mode", ["heat", "cool", "heat_cool", "dry", "fan_only", "off"]
)
def test_standard_modes_are_accepted_and_restored(mode):
    thermostat = update_thermostat(Thermostat(), 0.3, hvac_mode=mode)
    assert Thermostat.from_dict(thermostat.as_dict()).hvac_mode == mode


HEATING = ZoneDemand("room", "heating_demand", 1.0, 0.0, "below", "valid")
COOLING = ZoneDemand("room", "cooling_demand", 0.0, 1.0, "above", "valid")
UNKNOWN = ZoneDemand("room", "unknown", None, None, "insufficient_data", "missing")


@pytest.mark.parametrize(
    ("mode", "demand", "expected", "reason"),
    [
        ("heat_cool", HEATING, "heating_demand", "below"),
        ("heat_cool", COOLING, "cooling_demand", "above"),
        ("heat", HEATING, "heating_demand", "below"),
        ("heat", COOLING, "neutral", "thermostat_heat_only"),
        ("cool", COOLING, "cooling_demand", "above"),
        ("cool", HEATING, "neutral", "thermostat_cool_only"),
        ("off", HEATING, "neutral", "thermostat_off"),
        ("off", COOLING, "neutral", "thermostat_off"),
        ("dry", COOLING, "neutral", "thermostat_dry"),
        ("fan_only", HEATING, "neutral", "thermostat_fan_only"),
        ("heat", UNKNOWN, "unknown", "insufficient_data"),
    ],
)
def test_room_demand_follows_the_thermostat_mode(mode, demand, expected, reason):
    result = room_demand(mode, demand)
    assert (result.demand, result.reason) == (expected, reason)
    if expected == "neutral":
        assert (result.heating_score, result.cooling_score) == (0.0, 0.0)
        assert result.sensor_health == demand.sensor_health


@pytest.mark.parametrize(
    ("mode", "value", "band", "expected"),
    [
        ("heat", 21.5, (21, 24), (21.5, 24)),  # The other limit stays.
        ("heat", 23.8, (21, 24), (23.8, 26.8)),  # Too close: band moves along.
        ("cool", 25, (21, 24), (21, 25)),
        ("cool", 21.2, (21, 24), (18.2, 21.2)),
        ("heat", 20, (None, None), (20, 22)),  # No band yet: 2 °C wide.
        ("cool", 25, (None, None), (23, 25)),
    ],
)
def test_single_target_sets_one_limit(mode, value, band, expected):
    low, high = single_target(mode, value, *band, 0.3)
    assert (round(low, 2), round(high, 2)) == expected
    update_thermostat(Thermostat(), 0.3, heating_limit=low, cooling_limit=high)


# --- Unit options: dry, fan_only and fan speed --------------------------------


def test_fan_until_is_restored():
    thermostat = Thermostat(fan_mode="high", fan_until="2026-10-15T10:00:00+00:00")
    restored = Thermostat.from_dict(thermostat.as_dict())
    assert (restored.fan_mode, restored.fan_until) == (
        "high",
        "2026-10-15T10:00:00+00:00",
    )
    assert Thermostat.from_dict({"fan_until": 5}).fan_until is None
    # 0.13.5: the automatic fan switch is on unless stored off.
    assert Thermostat.from_dict({}).fan_control is True
    assert Thermostat.from_dict({"fan_control": False}).fan_control is False
    # 0.13.6: the units' last known options are restored, invalid parts dropped.
    restored = Thermostat.from_dict(
        {"unit_hvac_modes": ["heat", 3, "cool"], "unit_fan_modes": "25%"}
    )
    assert (restored.unit_hvac_modes, restored.unit_fan_modes) == (("heat", "cool"), ())


def test_fan_mode_is_stored_and_validated():
    thermostat = update_thermostat(Thermostat(), 0.3, fan_mode="75%")
    assert Thermostat.from_dict(thermostat.as_dict()).fan_mode == "75%"
    assert Thermostat.from_dict({"fan_mode": 3}).fan_mode is None
    with pytest.raises(ValueError, match="invalid_parameters"):
        update_thermostat(Thermostat(), 0.3, fan_mode=3)


def mode(target, unit_mode, applied=None, **kwargs):
    defaults = {
        "enabled": True,
        "safe_modes": frozenset({"cool"}),
        "busy": False,
        "applied": applied,
    }
    return plan_room_mode(
        target, unit_mode, "unit", "climate.unit", **defaults | kwargs
    )


def test_dry_and_fan_only_switch_the_unit_once():
    control = mode("dry", "off")
    assert (control.status, control.command.hvac_mode) == ("set_dry", "dry")
    assert control.command.reason == "thermostat_mode"
    assert mode("dry", "dry", "dry").status == "hold"
    # A person changed the unit afterwards: not fought.
    assert mode("dry", "cool", "dry").command is None
    # Switched off again (after any manual override): back on.
    assert mode("dry", "off", "dry").status == "set_dry"
    assert mode(
        "fan_only", "heat", safe_modes=frozenset({"heat"})
    ).command.hvac_mode == ("fan_only")


def test_dry_needs_a_cooling_group():
    assert mode("dry", "off", safe_modes=frozenset({"heat"})).status == (
        "blocked_outdoor_mode"
    )
    # The group switched our drying unit to heating: off instead of heating.
    control = mode("dry", "heat", "dry", safe_modes=frozenset({"heat"}))
    assert (control.command.service, control.command.reason) == (
        "turn_off",
        "dry_not_possible",
    )


def test_dry_mode_simulation_and_busy():
    assert mode("dry", "off", enabled=False).status == "simulation"
    assert mode("dry", "off", busy=True).status == "waiting_for_confirmation"


FANS = ("off", "25%", "50%", "75%", "100%")


@pytest.mark.parametrize(
    ("desired", "unit_mode", "unit_fan", "applied", "expected"),
    [
        ("75%", "cool", "50%", None, "75%"),
        ("75%", "cool", "75%", None, None),  # Already there.
        ("75%", "cool", "25%", "75%", None),  # Changed at the unit: not fought.
        ("75%", "off", "50%", None, None),  # Only for running units.
        (None, "cool", "50%", None, None),  # Leave the units alone.
        ("turbo", "cool", "50%", None, None),  # Not supported by the unit.
    ],
)
def test_fan_speed_is_passed_once(desired, unit_mode, unit_fan, applied, expected):
    assert plan_fan(desired, unit_mode, unit_fan, applied, FANS) == expected


def test_window_sensor_options_match_the_window_states():
    import ast
    from pathlib import Path

    from custom_components.zonentra.roomstate import WINDOW_STATES

    tree = ast.parse(
        (Path(__file__).parents[1] / "custom_components/zonentra/sensor.py").read_text()
    )
    window = next(
        node for node in tree.body if getattr(node, "name", "") == "WindowSensor"
    )
    options = next(
        statement.value
        for statement in window.body
        if isinstance(statement, ast.Assign)
        and statement.targets[0].id == "_attr_options"
    )
    assert [item.value for item in options.elts] == list(WINDOW_STATES)


# --- One-off manual targets ---------------------------------------------------


def test_manual_target_is_stored_and_ended_by_a_preset():
    manual = update_thermostat(
        Thermostat(),
        0.3,
        manual_heating_limit=20,
        manual_cooling_limit=25,
        manual_band="comfort",
    )
    assert manual.manual and manual.heating_limit is None
    assert Thermostat.from_dict(manual.as_dict()) == manual
    ended = update_thermostat(manual, 0.3, preset="eco")
    assert not ended.manual and ended.manual_heating_limit is None


@pytest.mark.parametrize(
    "changes",
    [
        {
            "manual_heating_limit": 22,
            "manual_cooling_limit": 22.4,
            "manual_band": "eco",
        },
        {
            "manual_heating_limit": 20,
            "manual_cooling_limit": None,
            "manual_band": "eco",
        },
        {"manual_heating_limit": 20, "manual_cooling_limit": 25, "manual_band": "x"},
    ],
)
def test_invalid_manual_target(changes):
    with pytest.raises(ValueError, match="invalid_limits"):
        update_thermostat(Thermostat(), 0.3, **changes)


def test_restore_drops_an_incomplete_manual_target():
    stored = {"manual_heating_limit": 20, "manual_cooling_limit": 25}
    assert not Thermostat.from_dict(stored).manual  # No band: not restored.
