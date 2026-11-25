"""0.5.0 pure tests: manual override, group coordination and control intents."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.zonentra.control import (
    ACTIONS,
    PHASES,
    CoordinationMemory,
    plan,
)
from custom_components.zonentra.demand import calculate_demand, collect_zone
from custom_components.zonentra.engine import GroupMemory, evaluate
from custom_components.zonentra.indoor import observe_group
from custom_components.zonentra.models import build_model
from custom_components.zonentra.observations import ClimateAttributes, SourceState
from custom_components.zonentra.override import (
    CommandTracker,
    ManualOverride,
    classify_transition,
    update_override,
)

NOW = datetime(2026, 10, 12, 14, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=2)
ROOMS = ("west", "meeting", "director")
TEMPERATURE = {"heat": "21", "neutral": "23", "cool": "25", "slight_heat": "21.6"}


def building(**settings):
    return build_model(
        "building",
        "Building",
        {
            "building": {
                "heating_limit": 22,
                "cooling_limit": 24,
                "hysteresis": 0.3,
                **settings,
            },
            "zones": [
                {
                    "id": room,
                    "name": room.title(),
                    "temperature_entity": f"sensor.{room}",
                    "group_id": "outdoor_1",
                    "use_building_limits": True,
                    "use_building_parameters": True,
                }
                for room in ROOMS
            ],
            "groups": [{"id": "outdoor_1", "name": "Outdoor 1"}],
            "equipment": [
                {
                    "id": room,
                    "name": f"{room} unit",
                    "kind": "hvac",
                    "entity_id": f"climate.{room}",
                    "zone_id": room,
                }
                for room in ROOMS
            ],
        },
    )


def unit(state, changed=LONG_AGO, action=None):
    return SourceState(
        state,
        None,
        NOW,
        changed_at=changed,
        climate=ClimateAttributes(action, 21, 22, "auto", "°C", changed),
    )


def simulate(
    demand, modes, model=None, memory=None, overrides=None, events=None, now=NOW
):
    """demand/modes map room -> 'heat'/'neutral'/'cool' and room -> unit source."""
    model = model or building()
    sources = {
        f"sensor.{room}": SourceState(TEMPERATURE[value], "°C", now)
        for room, value in demand.items()
    }
    sources.update(
        {
            f"climate.{room}": value if isinstance(value, SourceState) else unit(value)
            for room, value in modes.items()
            if value is not None
        }
    )
    demands = {
        zone.id: calculate_demand(zone, collect_zone(model, zone, sources, now))
        for zone in model.zones
    }
    memories = {"outdoor_1": GroupMemory("unknown", now - timedelta(hours=1))}
    decisions = {
        item.group_id: item for item in evaluate(model, demands, sources, memories, now)
    }
    units, observation = observe_group(model, model.groups[0], sources)
    changed = {
        room: sources[f"climate.{room}"].changed_at
        for room in ROOMS
        if f"climate.{room}" in sources
    }
    return plan(
        model,
        decisions,
        {"outdoor_1": observation},
        {item.equipment_id: item for item in units},
        changed,
        demands,
        overrides or {},
        events or {},
        {"outdoor_1": memory} if memory else {},
        now,
    )


def override(room, minutes_ago=1, duration=30, mode="heat"):
    started = NOW - timedelta(minutes=minutes_ago)
    return {
        room: ManualOverride(room, started, started + timedelta(minutes=duration), mode)
    }


# 1-4: automatic zone control within one outdoor group.
def test_example_group_heat_meeting_off():
    coordinations, intents, _ = simulate(
        {"west": "heat", "meeting": "neutral", "director": "heat"},
        {"west": "off", "meeting": "heat", "director": "heat"},
    )
    group = coordinations["outdoor_1"]
    assert group.group_mode == "heat"
    assert group.simulation and not group.active_switch_permitted
    assert "read_only_simulation" in group.blockers
    assert "current_mode_unknown" in group.blockers
    assert (intents["west"].action, intents["west"].desired_mode) == (
        "turn_on_heat",
        "heat",
    )
    assert (intents["meeting"].action, intents["meeting"].reason) == (
        "turn_off",
        "no_zone_demand",
    )
    assert (intents["director"].action, intents["director"].reason) == (
        "hold",
        "following_group_mode",
    )
    assert all(intent.simulation for intent in intents.values())
    assert dict(group.planned_actions) == {
        "west": "turn_on_heat",
        "meeting": "turn_off",
    }


def test_cooling_demand_turns_on_cool():
    _, intents, _ = simulate(
        {room: "cool" for room in ROOMS}, {room: "off" for room in ROOMS}
    )
    assert {intent.action for intent in intents.values()} == {"turn_on_cool"}


def test_no_demand_means_off():
    coordinations, intents, _ = simulate(
        {room: "neutral" for room in ROOMS},
        {"west": "off", "meeting": "heat", "director": "cool"},
    )
    assert coordinations["outdoor_1"].group_mode == "neutral"
    assert intents["west"].action == "hold" and intents["west"].desired_mode == "off"
    assert intents["meeting"].action == intents["director"].action == "turn_off"


def test_running_unit_stays_on_while_its_room_still_asks():
    """One-sided demand chooses heat, including a small hysteretic request."""
    demand = {"west": "slight_heat", "meeting": "neutral", "director": "neutral"}
    coordinations, intents, _ = simulate(
        demand, {"west": "heat", "meeting": "off", "director": "off"}
    )
    assert coordinations["outdoor_1"].group_mode == "heat"
    assert (intents["west"].action, intents["west"].reason) == (
        "hold",
        "following_group_mode",
    )
    # An off unit is also allowed to follow the room's valid heating request.
    _, intents, _ = simulate(
        demand, {"west": "off", "meeting": "off", "director": "off"}
    )
    assert intents["west"].action == "turn_on_heat"
    assert intents["west"].desired_mode == "heat"


def test_wrong_mode_is_corrected_to_group_mode():
    _, intents, _ = simulate(
        {room: "heat" for room in ROOMS},
        {"west": "auto", "meeting": "heat", "director": "dry"},
    )
    assert intents["west"].action == intents["director"].action == "correct_mode"
    assert intents["west"].desired_mode == "heat"
    assert intents["west"].steps == ("set_mode_heat",)


def test_zone_demand_opposite_to_group_mode_is_off():
    coordinations, intents, _ = simulate(
        {"west": "heat", "meeting": "heat", "director": "cool"},
        {"west": "heat", "meeting": "heat", "director": "off"},
    )
    assert coordinations["outdoor_1"].group_mode == "heat"
    assert intents["director"].reason == "zone_demand_conflicts_group_mode"
    assert intents["director"].desired_mode == "off"


def test_action_and_phase_lists_match_sensor_options():
    tree = ast.parse(
        (Path(__file__).parents[1] / "custom_components/zonentra/sensor.py").read_text()
    )
    options = {
        cls.name: [item.value for item in statement.value.elts]
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for statement in cls.body
        if isinstance(statement, ast.Assign)
        and statement.targets[0].id == "_attr_options"
    }
    assert options["UnitControlSensor"] == list(ACTIONS)
    assert options["GroupCoordinationSensor"] == list(PHASES)


# 5-17: manual override detection.
@pytest.mark.parametrize(
    "mode", ["heat", "cool", "dry", "fan_only", "auto", "heat_cool"]
)
def test_off_to_active_mode_is_manual_on(mode):
    assert classify_transition("off", mode, origin="ha_user") == "manual_on"
    assert classify_transition("off", mode) == "external_on"


@pytest.mark.parametrize(
    ("old", "new", "kwargs", "expected"),
    [
        ("heat", "heat", {}, "attribute_update"),  # 11: attributes only.
        ("unknown", "heat", {}, "from_unavailable"),  # 14
        ("unavailable", "cool", {}, "from_unavailable"),  # 15
        (None, "heat", {"old_present": False}, "initial"),  # 13: startup.
        ("off", "heat", {"zonentra_initiated": True}, "zonentra_initiated"),  # 16
        ("off", "heat", {"trusted": False}, "untrusted"),
        ("heat", "off", {}, "external_off"),
        ("heat", "off", {"origin": "automation"}, "external_off"),
        ("heat", "off", {"origin": "ha_user"}, "manual_off"),
        ("heat", "cool", {}, "mode_change"),
        ("heat", "unavailable", {}, "to_unavailable"),
    ],
)
def test_transitions_that_do_not_start_an_override(old, new, kwargs, expected):
    assert classify_transition(old, new, **kwargs) == expected
    result, event = update_override(
        None, expected, "west", new, NOW, enabled=True, duration_minutes=30
    )
    assert result is None and event is None


@pytest.mark.parametrize(("duration", "expected"), [(30, 30), (5, 5), (120, 120)])
def test_override_duration(duration, expected):
    result, event = update_override(
        None, "manual_on", "west", "cool", NOW, enabled=True, duration_minutes=duration
    )
    assert event == "override_started"
    assert result.expires_at - result.started_at == timedelta(minutes=expected)
    assert result.mode_at_start == "cool"
    assert result.remaining_seconds(NOW) == expected * 60


def test_default_settings_are_30_minutes_and_enabled():
    settings = building().settings
    assert settings.manual_override_enabled is True
    assert settings.manual_override_duration_minutes == 30


def test_override_is_not_extended_and_expires_without_events():
    current, _ = update_override(
        None, "manual_on", "west", "heat", NOW, enabled=True, duration_minutes=30
    )
    later = NOW + timedelta(minutes=20)
    for transition in (None, "attribute_update", "mode_change", "zonentra_initiated"):
        assert update_override(
            current,
            transition,
            "west",
            "heat",
            later,
            enabled=True,
            duration_minutes=30,
        ) == (current, None)
    expired = NOW + timedelta(minutes=30)
    assert update_override(
        current, None, "west", "heat", expired, enabled=True, duration_minutes=30
    ) == (None, "override_expired")


def test_manual_off_ends_override_and_new_on_starts_fresh():
    current, _ = update_override(
        None, "manual_on", "west", "heat", NOW, enabled=True, duration_minutes=30
    )
    later = NOW + timedelta(minutes=10)
    assert update_override(
        current, "manual_off", "west", "off", later, enabled=True, duration_minutes=30
    ) == (None, "override_ended_manual_off")
    fresh, event = update_override(
        None, "manual_on", "west", "heat", later, enabled=True, duration_minutes=30
    )
    assert event == "override_started" and fresh.started_at == later


def test_disabled_override_never_starts():
    assert update_override(
        None, "manual_on", "west", "heat", NOW, enabled=False, duration_minutes=30
    ) == (None, "override_disabled")


def test_command_tracker_is_bounded():
    tracker = CommandTracker(confirmation_seconds=60)
    tracker.register("climate.west", "heat", NOW)
    assert not tracker.confirm("climate.west", "cool", NOW)  # Unexpected state.
    assert tracker.confirm("climate.west", "heat", NOW + timedelta(seconds=30))
    assert not tracker.confirm("climate.west", "heat", NOW + timedelta(seconds=31))
    tracker.register("climate.west", "heat", NOW)
    assert not tracker.confirm("climate.west", "heat", NOW + timedelta(seconds=61))
    assert tracker.pending == ()


# 7, 9: override keeps the unit on without demand; afterwards automatic again.
def test_override_without_demand_is_respected_then_resumes():
    demand = {room: "neutral" for room in ROOMS}
    modes = {"west": "heat", "meeting": "off", "director": "off"}
    _, intents, _ = simulate(demand, modes, overrides=override("west"))
    west = intents["west"]
    assert (west.action, west.reason) == ("hold", "manual_override_neutral")
    assert west.manual_override_active
    assert west.override_remaining_seconds == 29 * 60
    _, intents, _ = simulate(demand, modes, events={"west": "override_expired"})
    assert (intents["west"].action, intents["west"].reason) == (
        "turn_off",
        "override_expired",
    )


def test_override_end_with_demand_follows_automatic_control():
    _, intents, _ = simulate(
        {"west": "heat", "meeting": "neutral", "director": "neutral"},
        {"west": "heat", "meeting": "off", "director": "off"},
        events={"west": "override_expired"},
    )
    assert (intents["west"].action, intents["west"].reason) == (
        "hold",
        "following_group_mode",
    )


# 18-21: group mode stays leading during an override.
@pytest.mark.parametrize(("group", "manual"), [("heat", "cool"), ("cool", "heat")])
def test_manual_opposite_mode_is_corrected(group, manual):
    coordinations, intents, _ = simulate(
        {"west": "neutral", "meeting": group, "director": group},
        {"west": manual, "meeting": group, "director": group},
        overrides=override("west", mode=manual),
    )
    assert coordinations["outdoor_1"].group_mode == group
    west = intents["west"]
    assert (west.action, west.desired_mode) == ("correct_mode", group)
    assert west.reason == "manual_override_mode_corrected"
    assert west.manual_override_active


def test_override_at_neutral_follows_running_units_or_holds():
    demand = {room: "neutral" for room in ROOMS}
    _, intents, _ = simulate(
        demand,
        {"west": "heat", "meeting": "cool", "director": "off"},
        overrides={**override("west"), **override("meeting", mode="cool")},
    )
    # Opposite modes without a leading group mode: no side is picked.
    for room in ("west", "meeting"):
        assert (intents[room].action, intents[room].blocked_reason) == (
            "blocked",
            "group_conflict",
        )
    _, intents, _ = simulate(
        demand,
        {"west": "auto", "meeting": "cool", "director": "off"},
        overrides={**override("west"), **override("meeting", mode="cool")},
    )
    assert (intents["west"].action, intents["west"].desired_mode) == (
        "correct_mode",
        "cool",
    )
    assert intents["meeting"].action == "hold"
    _, intents, _ = simulate(
        demand,
        {"west": "auto", "meeting": "off", "director": "off"},
        overrides=override("west"),
    )
    assert (intents["west"].action, intents["west"].blocked_reason) == (
        "blocked",
        "mode_undetermined",
    )


def test_override_at_unknown_is_blocked():
    _, intents, _ = simulate(
        {"west": "neutral", "meeting": "heat"},  # director temperature missing
        {"west": "heat", "meeting": "heat", "director": "off"},
        overrides=override("west"),
    )
    assert intents["west"].action == "blocked"
    assert intents["west"].reason == "group_mode_unknown"
    assert intents["west"].manual_override_active


# 22, 23: coordinated group switch, observed step by step.
@pytest.mark.parametrize(("old", "new"), [("heat", "cool"), ("cool", "heat")])
def test_group_switch_state_machine(old, new):
    demand = {room: new for room in ROOMS}
    memory = CoordinationMemory(old, LONG_AGO)
    coordinations, intents, memories = simulate(
        demand, {room: old for room in ROOMS}, memory=memory
    )
    memory = memories["outdoor_1"]
    group = coordinations["outdoor_1"]
    assert (group.phase, group.switch_from, group.switch_to) == ("stopping", old, new)
    assert group.switch_allowed_in_simulation and not group.active_switch_permitted
    assert {intent.action for intent in intents.values()} == {"turn_off"}
    assert intents["west"].steps == ("turn_off", "wait_changeover", f"turn_on_{new}")
    # All units observed off: safety changeover wait (no timer extension).
    off = {room: unit("off", NOW - timedelta(hours=1)) for room in ROOMS}
    coordinations, intents, memories = simulate(demand, off, memory=memory)
    memory = memories["outdoor_1"]
    group = coordinations["outdoor_1"]
    assert group.phase == "waiting"
    assert group.waiting_until == NOW + timedelta(seconds=300)
    assert intents["west"].blocked_reason == "changeover_wait"
    later = NOW + timedelta(seconds=300)
    coordinations, intents, memories = simulate(demand, off, memory=memory, now=later)
    memory = memories["outdoor_1"]
    assert coordinations["outdoor_1"].phase == "applying"
    assert {intent.action for intent in intents.values()} == {f"turn_on_{new}"}
    done = {room: unit(new, later) for room in ROOMS}
    coordinations, _, memories = simulate(demand, done, memory=memory, now=later)
    memory = memories["outdoor_1"]
    assert coordinations["outdoor_1"].phase == "completed"
    assert memory.mode == new and memory.since == later


def test_switch_waits_for_minimum_switch_interval():
    coordinations, intents, _ = simulate(
        {room: "cool" for room in ROOMS},
        {room: "heat" for room in ROOMS},
        memory=CoordinationMemory("heat", NOW - timedelta(minutes=5)),
    )
    group = coordinations["outdoor_1"]
    assert (group.phase, group.phase_reason) == ("preparing", "minimum_switch_interval")
    assert group.waiting_until == NOW + timedelta(minutes=10)
    assert {intent.action for intent in intents.values()} == {"blocked"}


def test_override_cannot_bypass_group_switch():
    coordinations, intents, _ = simulate(
        {room: "cool" for room in ROOMS},
        {"west": "heat", "meeting": "off", "director": "off"},
        memory=CoordinationMemory("heat", LONG_AGO),
        overrides=override("west"),
    )
    assert coordinations["outdoor_1"].phase == "stopping"
    assert intents["west"].action == "turn_off"


# 24: anti-cycling.
def test_minimum_off_and_on_times():
    just_off = unit("off", NOW - timedelta(seconds=60))
    just_on = unit("heat", NOW - timedelta(seconds=100))
    _, intents, _ = simulate(
        {"west": "heat", "meeting": "neutral", "director": "heat"},
        {"west": just_off, "meeting": just_on, "director": "heat"},
    )
    west, meeting = intents["west"], intents["meeting"]
    assert (west.action, west.blocked_reason, west.desired_mode) == (
        "blocked",
        "minimum_off_time",
        "heat",
    )
    assert west.blocked_until == NOW + timedelta(seconds=120)
    assert west.seconds_since_transition == 60
    assert (meeting.action, meeting.blocked_reason) == ("blocked", "minimum_on_time")
    assert meeting.blocked_until == NOW + timedelta(seconds=500)


def test_anti_cycling_times_are_configurable():
    model = building(min_off_seconds=30, min_on_seconds=0)
    _, intents, _ = simulate(
        {"west": "heat", "meeting": "neutral", "director": "heat"},
        {
            "west": unit("off", NOW - timedelta(seconds=60)),
            "meeting": unit("heat", NOW - timedelta(seconds=1)),
            "director": "heat",
        },
        model=model,
    )
    assert intents["west"].action == "turn_on_heat"
    assert intents["meeting"].action == "turn_off"


# 25-27: missing / unavailable units and sensor faults block conservatively.
@pytest.mark.parametrize("state", [None, SourceState("unavailable")])
def test_missing_or_unavailable_unit(state):
    coordinations, intents, _ = simulate(
        {room: "heat" for room in ROOMS},
        {"west": state, "meeting": "off", "director": "off"},
    )
    assert intents["west"].action == "blocked"
    assert intents["west"].reason in ("indoor_unit_unavailable", "group_mode_unknown")
    # Equipment unavailable -> insufficient data -> no new actions anywhere.
    assert coordinations["outdoor_1"].group_mode == "unknown"
    assert {intent.action for intent in intents.values()} == {"blocked"}


def test_sensor_fault_blocks_new_actions():
    coordinations, intents, _ = simulate(
        {"west": "heat", "meeting": "heat"},  # director sensor missing
        {room: "off" for room in ROOMS},
    )
    assert coordinations["outdoor_1"].group_mode == "unknown"
    assert coordinations["outdoor_1"].phase == "blocked"
    assert intents["west"].reason == "group_mode_unknown"


def test_unavailable_unit_blocks_group_switch():
    coordinations, _, _ = simulate(
        {room: "cool" for room in ROOMS},
        {"west": "heat", "meeting": "heat", "director": "heat"},
        memory=CoordinationMemory("heat", LONG_AGO),
    )
    assert coordinations["outdoor_1"].phase == "stopping"
    coordinations, _, _ = simulate(
        {room: "cool" for room in ROOMS},
        {"west": "heat", "meeting": "heat", "director": SourceState("unavailable")},
        memory=CoordinationMemory("heat", LONG_AGO),
    )
    assert coordinations["outdoor_1"].phase == "blocked"


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("device", "external_on"),
        ("ha_user", "manual_on"),
        ("automation", "external_on"),
    ],
)
def test_origin_of_a_switch_on(origin, expected):
    """Only a recognized user can start a manual override."""
    assert classify_transition("off", "heat", origin=origin) == expected
    result, _ = update_override(
        None, expected, "west", "heat", NOW, enabled=True, duration_minutes=30
    )
    assert (result is not None) == (expected == "manual_on")
