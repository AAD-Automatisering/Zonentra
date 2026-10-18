"""Pure group mode coordination and per-unit control intents.

This module calculates plans without calling Home Assistant. The runtime uses
unit intents for enabled room regulation and shows coordination phases as
diagnostics. A planned step does not prove execution; the switch state machine
only advances on what is observed.

Two levels:

* group: the outdoor unit group's leading mode (heat / cool / neutral /
  unknown) from the existing weighted demand, plus a heat <-> cool switch state
  machine (idle, preparing, stopping, waiting, applying, verifying, completed,
  blocked, failed);
* unit: the intended action per indoor unit (turn_on_heat, turn_on_cool,
  turn_off, correct_mode, hold, blocked), respecting manual overrides,
  anti-short-cycle times and the group mode, which always stays leading.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping

from .demand import ZoneDemand
from .engine import GroupDecision, select_group_mode
from .indoor import GroupModeObservation, IndoorUnitObservation
from .models import Building, BuildingSettings, EquipmentGroup
from .override import ManualOverride

# Selected indoor modes that do not demand the opposite outdoor mode.
COMPATIBLE = {"heat": ("heat", "fan_only"), "cool": ("cool", "dry", "fan_only")}
OPPOSITE = {"heat": "cool", "cool": "heat"}
DEMAND_MODE = {"heating_demand": "heat", "cooling_demand": "cool"}
PHASES = (
    "idle",
    "preparing",
    "stopping",
    "waiting",
    "applying",
    "verifying",
    "completed",
    "blocked",
    "failed",
)
ACTIONS = (
    "turn_on_heat",
    "turn_on_cool",
    "turn_off",
    "correct_mode",
    "hold",
    "blocked",
)


@dataclass(frozen=True)
class CoordinationMemory:
    """What the coordinator remembers between evaluations (in memory only)."""

    mode: str | None  # heat / cool the group last operated in; None = unknown
    since: datetime  # start of that mode; restart counts as a new start
    phase: str = "idle"
    switch_target: str | None = None
    switch_started_at: datetime | None = None
    stopped_at: datetime | None = None  # first evaluation with nothing to stop


@dataclass(frozen=True)
class GroupCoordination:
    group_id: str
    desired_mode: str  # Decision Engine output (heat / cool / off / unknown)
    group_mode: str  # leading mode for indoor units: heat / cool / neutral / unknown
    group_mode_reason: str
    coordinated_mode: str | None
    derived_mode: str
    member_units: int
    available_units: int
    conflicts: tuple[str, ...]
    switch_needed: bool
    switch_from: str | None
    switch_to: str | None
    switch_allowed_in_simulation: bool
    active_switch_permitted: bool  # Diagnostic plan; runtime authorizes commands.
    phase: str
    phase_reason: str
    waiting_until: datetime | None
    blockers: tuple[str, ...]
    planned_actions: tuple[tuple[str, str], ...] = ()
    simulation: bool = True


@dataclass(frozen=True)
class UnitIntent:
    equipment_id: str
    zone_id: str
    group_id: str | None
    observed_mode: str
    availability: str
    zone_demand: str
    desired_mode: str  # end state: heat / cool / off / unchanged / unknown
    action: str  # next step only; see steps for the full sequence
    reason: str
    blocked_reason: str | None
    blocked_until: datetime | None
    steps: tuple[str, ...]
    manual_override_active: bool
    override_remaining_seconds: int | None
    override_expires_at: datetime | None
    seconds_since_transition: int | None
    coordination_phase: str
    simulation: bool = True


def resolve_group_mode(
    decision: GroupDecision, group: EquipmentGroup, memory_mode: str | None
) -> tuple[str, str]:
    """Leading group mode from the existing weighted demand (never invented)."""
    desired = decision.desired_mode
    if desired in ("heat", "cool"):
        return desired, decision.reason
    if decision.reason in (
        "score_deadband",
        "observed_group_conflict",
        "no_significant_demand",
    ):
        target, reason = select_group_mode(
            group,
            decision.heating_score,
            decision.cooling_score,
            decision.heating_requested or decision.heating_score > 0,
            decision.cooling_requested or decision.cooling_score > 0,
            memory_mode,
        )
        if target in ("heat", "cool"):
            return target, (
                "demand_leads_unit_conflict"
                if decision.reason == "observed_group_conflict"
                else "balanced_demand_keep_coordinated_mode"
            )
        return "neutral", reason
    if desired == "off":
        return "neutral", decision.reason
    return "unknown", decision.blocked_reason or decision.reason


def coordinate(
    group: EquipmentGroup,
    decision: GroupDecision,
    observation: GroupModeObservation,
    units: tuple[IndoorUnitObservation, ...],
    needs: Mapping[str, str | None],
    overrides: frozenset[str],
    memory: CoordinationMemory,
    settings: BuildingSettings,
    now: datetime,
) -> tuple[GroupCoordination, CoordinationMemory]:
    """Group level: leading mode and the observed progress of a mode switch.

    ``needs`` maps equipment_id to the mode its zone demand asks for.
    """
    memory_mode = memory.mode
    if memory_mode is None and observation.derived_mode in ("heat", "cool"):
        memory_mode = observation.derived_mode  # Start from what units show.
    group_mode, mode_reason = resolve_group_mode(decision, group, memory_mode)
    blockers = ["read_only_simulation"]
    if decision.mode_quality != "valid":
        blockers.append("current_mode_unknown")  # No physical outdoor feedback.
    available = [unit for unit in units if unit.available]
    switch_needed = (
        group_mode in OPPOSITE and memory_mode in OPPOSITE and group_mode != memory_mode
    )
    new_memory = replace(memory, mode=memory_mode)
    waiting_until = None
    phase_reason = "no_switch_needed"

    if group_mode == "unknown":
        phase, phase_reason = "blocked", "group_mode_unknown"
    elif not switch_needed:
        if group_mode in OPPOSITE and memory_mode is None:
            new_memory = CoordinationMemory(group_mode, now)
        elif memory.switch_target:
            new_memory = replace(
                new_memory,
                phase="idle",
                switch_target=None,
                switch_started_at=None,
                stopped_at=None,
            )
            phase_reason = "switch_cancelled"
        phase = "completed" if new_memory.phase == "completed" else "idle"
    else:
        target = group_mode
        if memory.switch_target != target:
            new_memory = replace(
                new_memory, switch_target=target, switch_started_at=now, stopped_at=None
            )
        to_stop = [
            unit
            for unit in available
            if unit.selected_mode != "off"
            and unit.selected_mode not in COMPATIBLE[target]
        ]
        required = [
            unit
            for unit in available
            if unit.equipment_id in overrides or needs.get(unit.equipment_id) == target
        ]
        hard_conflicts = [
            code for code in observation.conflicts if code != "selected_heat_and_cool"
        ]
        switch_allowed_at = memory.since + timedelta(seconds=group.min_switch_seconds)
        if len(available) < observation.member_units:
            phase, phase_reason = "blocked", "indoor_units_unavailable"
        elif hard_conflicts:
            phase, phase_reason = "blocked", "indoor_units_conflict"
        elif now < switch_allowed_at:
            # Comfort anti-cycling before a switch may start.
            phase, phase_reason = "preparing", "minimum_switch_interval"
            waiting_until = switch_allowed_at
        elif to_stop:
            phase, phase_reason = "stopping", "stopping_units_in_previous_mode"
            new_memory = replace(new_memory, stopped_at=None)
        else:
            stopped_at = new_memory.stopped_at or now
            new_memory = replace(new_memory, stopped_at=stopped_at)
            waiting_until = stopped_at + timedelta(
                seconds=settings.changeover_wait_seconds
            )
            if now < waiting_until:
                phase, phase_reason = "waiting", "changeover_wait"
            else:
                waiting_until = None
                pending = [unit for unit in required if unit.selected_mode != target]
                if pending:
                    phase, phase_reason = "applying", "applying_new_mode"
                elif any(unit.mismatch for unit in required):
                    phase, phase_reason = "failed", "verification_conflict"
                elif required and observation.derived_mode != target:
                    phase, phase_reason = "verifying", "awaiting_consistent_feedback"
                else:
                    new_memory = CoordinationMemory(target, now, "completed")
                    phase, phase_reason, switch_needed = (
                        "completed",
                        "switch_completed",
                        False,
                    )
        if switch_needed:
            new_memory = replace(new_memory, phase=phase)
    coordination = GroupCoordination(
        group.id,
        decision.desired_mode,
        group_mode,
        mode_reason,
        new_memory.mode,
        observation.derived_mode,
        observation.member_units,
        observation.available_units,
        observation.conflicts,
        switch_needed,
        memory_mode if switch_needed else None,
        group_mode if switch_needed else None,
        switch_needed and phase not in ("blocked", "failed"),
        False,
        phase,
        phase_reason,
        waiting_until,
        tuple(blockers),
    )
    return coordination, new_memory


def unit_intent(
    unit: IndoorUnitObservation,
    changed_at: datetime | None,
    zone_demand: ZoneDemand | None,
    coordination: GroupCoordination,
    override: ManualOverride | None,
    other_active_modes: tuple[str, ...],
    settings: BuildingSettings,
    now: datetime,
    override_event: str | None = None,
    *,
    native_hold: bool = False,
) -> UnitIntent:
    """Unit level: desired end state and the next planned step."""
    mode = unit.selected_mode
    demand = zone_demand.demand if zone_demand else "unknown"
    active_override = override if override and override.active(now) else None
    since = round((now - changed_at).total_seconds()) if changed_at else None

    def result(desired, action, reason, blocked=None, until=None, steps=None):
        return UnitIntent(
            unit.equipment_id,
            unit.zone_id,
            unit.group_id,
            mode,
            unit.availability,
            demand,
            desired,
            action,
            reason,
            blocked,
            until,
            steps
            if steps is not None
            else ((action,) if action in ACTIONS[:4] else ()),
            active_override is not None,
            active_override.remaining_seconds(now) if active_override else None,
            active_override.expires_at if active_override else None,
            since,
            coordination.phase,
        )

    def intend_on(target, reason):
        if since is not None and since < settings.min_off_seconds:
            until = changed_at + timedelta(seconds=settings.min_off_seconds)
            return result(target, "blocked", reason, "minimum_off_time", until)
        return result(target, f"turn_on_{target}", reason)

    def intend_off(reason):
        if since is not None and since < settings.min_on_seconds:
            until = changed_at + timedelta(seconds=settings.min_on_seconds)
            return result("off", "blocked", reason, "minimum_on_time", until)
        return result("off", "turn_off", reason)

    def correct(target, reason):
        return result(target, "correct_mode", reason, steps=(f"set_mode_{target}",))

    if not unit.available:
        return result(
            "unknown", "blocked", "indoor_unit_unavailable", unit.availability
        )
    if active_override and active_override.mode_at_start == "off" and mode == "off":
        # 0.9 room regulation: a person switched the unit off; respect it.
        return result("off", "hold", "manual_override_off")
    if coordination.group_mode == "unknown":
        return result(
            "unchanged", "blocked", "group_mode_unknown", coordination.phase_reason
        )
    target = coordination.group_mode if coordination.group_mode in OPPOSITE else None

    if coordination.switch_needed:
        new = coordination.switch_to
        needs = active_override is not None or DEMAND_MODE.get(demand) == new
        end = new if needs else "off"
        if coordination.phase in ("blocked", "preparing", "failed"):
            if mode == "off" and not needs:
                return result("off", "hold", "already_off")
            return result(
                end,
                "blocked",
                f"group_switch_{coordination.phase}",
                coordination.phase_reason,
                coordination.waiting_until,
            )
        if mode != "off" and mode not in COMPATIBLE[new]:
            # A manual override never bypasses the switch procedure.
            steps = ("turn_off", "wait_changeover", f"turn_on_{new}") if needs else None
            return result(
                end, "turn_off", "group_switch_stop_previous_mode", steps=steps
            )
        if coordination.phase == "waiting":
            if needs and mode != new:
                return result(
                    new,
                    "blocked",
                    "group_switch_waiting",
                    "changeover_wait",
                    coordination.waiting_until,
                )
            return result(end if needs else mode, "hold", "group_switch_waiting")
        target = new  # applying / verifying: normal rules towards the new mode.

    if active_override:
        if target:
            if mode in COMPATIBLE[target]:
                return result(mode, "hold", "manual_override")
            return correct(target, "manual_override_mode_corrected")
        # Neutral group: no new mode is chosen; other running units lead.
        leading = {
            "heat" if other == "heat" else "cool"
            for other in other_active_modes
            if other in ("heat", "cool", "dry")
        }
        own = {"heat": "heat", "cool": "cool", "dry": "cool", "fan_only": None}.get(
            mode, "undetermined"
        )
        if len(leading) > 1:
            return result("unchanged", "blocked", "manual_override", "group_conflict")
        if own == "undetermined":
            if leading:
                return correct(
                    next(iter(leading)), "manual_override_follow_active_units"
                )
            return result(
                "unchanged", "blocked", "manual_override", "mode_undetermined"
            )
        if own is None or not leading or own in leading:
            return result(mode, "hold", "manual_override_neutral")
        # Opposite running units and no leading group mode: never pick a side.
        return result("unchanged", "blocked", "manual_override", "group_conflict")

    if zone_demand is None or demand == "unknown":
        health = zone_demand.sensor_health if zone_demand else "no_zone_demand"
        return result("unchanged", "blocked", "zone_demand_unknown", health)
    if (
        native_hold
        and demand == "neutral"
        and zone_demand.reason == "within_comfort_band"
        and mode in OPPOSITE
        and target in (None, mode)
        and not coordination.switch_needed
        and not coordination.conflicts
        and coordination.phase in ("idle", "completed")
    ):
        return result(mode, "hold", "native_regulation")
    wanted = DEMAND_MODE.get(demand)
    if target and wanted == target:
        if mode == target:
            return result(target, "hold", "following_group_mode")
        if mode == "off":
            return intend_on(target, "zone_demand")
        return correct(target, "mode_differs_from_group_mode")
    if target is None and wanted is not None and mode == wanted:
        # A mixed group can remain neutral while this room still asks. Keep
        # its running unit on rather than interrupting the hysteresis cycle.
        return result(mode, "hold", "room_still_asks")
    reason = (
        "override_expired"
        if override_event == "override_expired"
        else "no_zone_demand"
        if wanted is None
        else "group_mode_neutral"
        if target is None
        else "zone_demand_conflicts_group_mode"
    )
    if mode == "off":
        return result("off", "hold", reason)
    return intend_off(reason)


def standalone_coordination(
    equipment_id: str, zone_demand: ZoneDemand | None
) -> GroupCoordination:
    """Units without an outdoor group follow their own zone demand."""
    demand = zone_demand.demand if zone_demand else "unknown"
    mode = "unknown" if demand == "unknown" else DEMAND_MODE.get(demand, "neutral")
    return GroupCoordination(
        "",
        demand,
        mode,
        "no_group",
        None,
        "unknown",
        1,
        1,
        (),
        False,
        None,
        None,
        False,
        False,
        "idle",
        "no_group",
        None,
        ("read_only_simulation",),
    )


def plan(
    building: Building,
    decisions: Mapping[str, GroupDecision],
    observations: Mapping[str, GroupModeObservation],
    units: Mapping[str, IndoorUnitObservation],
    changed_at: Mapping[str, datetime | None],
    demands: Mapping[str, ZoneDemand],
    overrides: Mapping[str, ManualOverride],
    override_events: Mapping[str, str],
    memories: Mapping[str, CoordinationMemory],
    now: datetime,
    *,
    native_holds: frozenset[str] = frozenset(),
) -> tuple[
    dict[str, GroupCoordination], dict[str, UnitIntent], dict[str, CoordinationMemory]
]:
    """Whole-building plans: coordinations, unit intents and new memories."""
    settings = building.settings
    hvac = [item for item in building.equipment if item.kind == "hvac"]
    needs = {
        item.id: DEMAND_MODE.get(demands[item.zone_id].demand)
        if item.zone_id in demands
        else None
        for item in hvac
    }
    active_overrides = frozenset(
        key
        for key, override in overrides.items()
        if override.active(now) and override.mode_at_start != "off"
    )
    coordinations, intents, new_memories = {}, {}, {}
    for group in building.groups:
        members = tuple(
            units[item.id]
            for item in hvac
            if item.group_id == group.id and item.id in units
        )
        coordination, new_memories[group.id] = coordinate(
            group,
            decisions[group.id],
            observations[group.id],
            members,
            needs,
            active_overrides,
            memories.get(group.id) or CoordinationMemory(None, now),
            settings,
            now,
        )
        coordinations[group.id] = coordination
    for item in hvac:
        if item.id not in units:
            continue
        unit = units[item.id]
        demand = demands.get(item.zone_id)
        coordination = (
            coordinations[item.group_id]
            if item.group_id in coordinations
            else standalone_coordination(item.id, demand)
        )
        others = tuple(
            units[other.id].selected_mode
            for other in hvac
            if other.id != item.id
            and other.group_id == item.group_id
            and other.id in units
            and units[other.id].available
        )
        intents[item.id] = unit_intent(
            unit,
            changed_at.get(item.id),
            demand,
            coordination,
            overrides.get(item.id),
            others,
            settings,
            now,
            override_events.get(item.id),
            native_hold=item.id in native_holds,
        )
    for group_id, coordination in coordinations.items():
        coordinations[group_id] = replace(
            coordination,
            planned_actions=tuple(
                (intent.equipment_id, intent.action)
                for intent in intents.values()
                if intent.group_id == group_id and intent.action in ACTIONS[:4]
            ),
        )
    return coordinations, intents, new_memories
