"""Event-driven observations, planning and enabled Home Assistant control."""

import logging
from dataclasses import asdict, replace
from datetime import timedelta

from homeassistant.const import EVENT_STATE_CHANGED, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
)
from homeassistant.helpers.temperature import display_temp
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .control import plan
from .decision import Observation, simulate
from .demand import ZoneDemand, calculate_demand, collect_zone, comfort_limits
from .engine import GroupMemory, evaluate, observe_mode
from .executor import (
    COMPATIBLE,
    PRESET_RETRY_SECONDS,
    PULSE_SECONDS,
    ModeCommand,
    ModeControl,
    PresetApplication,
    PresetMemory,
    next_correction_check,
    plan_mode_commands,
)
from .forecast import (
    REFRESH_MINUTES,
    RETRY_SECONDS,
    Forecast,
    day_type,
    demand_margins,
    summarize_forecast,
)
from .indoor import compare_modes, observe_group, observe_unit
from .models import Building
from .observations import (
    ClimateAttributes,
    Reading,
    SourceState,
    max_age_for,
    read,
)
from .override import CommandTracker, classify_transition, update_override
from .presence import MotionMemory, Occupancy, motion_state, occupancy, read_presence
from .rain import RainMemory, RainProtection
from .roomstate import (
    AUTO_FAN,
    Trend,
    auto_fan,
    fan_speeds,
    safety_state,
    unit_fan,
    update_trend,
    vane_command,
    vane_mode,
    window_state,
)
from .runtime_bookings import BookingRuntimeMixin
from .runtime_commands import CommandRuntimeMixin
from .runtime_floor import FloorRuntimeMixin
from .runtime_helpers import _origin, _setpoint
from .runtime_office import OfficeRuntimeMixin
from .runtime_shading import ShadingRuntimeMixin
from .runtime_timing import TimingRuntimeMixin
from .shading import ScreenDecision, ScreenMove, SunMemory
from .temperature import convert_temperature
from .thermostat import (
    CLEAR_MANUAL,
    UNIT_MODES,
    RoomUnitControl,
    SetpointPlan,
    Thermostat,
    active_band,
    plan_fan,
    plan_room_mode,
    plan_room_unit,
    plan_setpoint,
    room_demand,
    safe_modes,
    update_thermostat,
)
from .wind import WindMemory, WindProtection

_LOGGER = logging.getLogger(__name__)
UNIT_RUNNING = ("heat", "cool", "dry", "fan_only")
ZONE_LIMIT_KEYS = (
    "heating_limit",
    "cooling_limit",
    "eco_heating_limit",
    "eco_cooling_limit",
    "away_heating_limit",
    "away_cooling_limit",
)


class BuildingRuntime(
    FloorRuntimeMixin,
    BookingRuntimeMixin,
    OfficeRuntimeMixin,
    ShadingRuntimeMixin,
    TimingRuntimeMixin,
    CommandRuntimeMixin,
):
    """Observe one building, evaluate its plans and dispatch enabled controls."""

    def __init__(
        self, hass: HomeAssistant, building: Building, *, active: bool = False
    ) -> None:
        self.hass = hass
        # Active outdoor-stand control exists only for the central layout.
        self.active_capable = active
        self.mode_active: dict[str, bool | None] = {}  # None until switch restored
        self.preset_memories: dict[str, PresetMemory] = {}
        self.preset_applications: dict[str, PresetApplication] = {}
        self.mode_controls = {}
        self.group_switches: dict = {}
        self.room_switches: dict = {}
        self.pending_offs = []  # (due, ModeCommand, target mode)
        self.command_log: list[dict] = []
        self.command_receipts: dict = {}
        self.command_retries: dict = {}
        self.mode_store = None
        self.building = building
        self.signal = f"zonentra_{building.id}_updated"
        self.observations: dict[str, Observation] = {}
        self.decisions = ()
        self.outdoor_temperature = Reading(None, "missing")
        self.zone_snapshots = {}
        self.zone_demands = {}
        self.hvac_demands = {}
        self.floor_plans = {}
        self.floor_observations = {}
        self.floor_overrides = {}
        self.heating_watches = {}
        self.heating_progress = {}
        self._floor_checks = []
        self.group_decisions = {}
        # Indoor unit interpretation; diagnostic only, never physical feedback.
        self.indoor_units = {}
        self.group_observations = {}
        self.mode_comparisons = {}
        self.evaluated_at = dt_util.utcnow()
        # 0.8 presence (building) and motion (zone) select comfort/eco/away.
        self.presence = "not_configured"
        self.motion_memories: dict[str, MotionMemory] = {}
        self.zone_occupancy: dict[str, Occupancy] = {}
        self._motion_checks: list = []
        # 0.9 room thermostats (central layout). Persisted by the entry setup.
        self.thermostats: dict[str, Thermostat] = {}
        self.thermostat_store = None
        self.thermostat_cache: dict | None = None  # survives central reloads
        self.zone_bands: dict[str, tuple[str, str]] = {}
        self.comfort: dict[str, tuple[Reading, Reading]] = {}
        self.room_controls: dict[str, RoomUnitControl] = {}
        self.setpoint_plans: dict[str, SetpointPlan] = {}
        self.setpoint_sent: dict = {}
        self.setpoint_tracker = CommandTracker()
        self._setpoint_checks: list = []
        self.mode_applied: dict[str, str] = {}  # dry / fan_only set per unit
        self.fan_applied: dict[str, str] = {}  # fan speed sent per unit
        # 0.10 room state (like VTherm's sensors): window, safety, trend, fan.
        self.windows: dict[str, str] = {}
        self.safety: dict[str, tuple[str, str]] = {}
        self.trends: dict[str, Trend] = {}
        self.auto_fans: dict[str, str | None] = {}
        self.fan_sent_at: dict = {}
        self._window_checks: list = []
        self._manual_checks: list = []
        self.vane_applied: dict[str, str] = {}  # zone -> heat / cool position set
        # 0.11 screens: sunshine memory, decisions, moves and manual overrides.
        self.wind_memory = WindMemory()
        self.wind_policy = None
        self.wind = WindProtection("not_configured", self.wind_memory)
        self.wind_speed = None
        self.wind_gust = None
        self.rain_memory = RainMemory()
        self.rain_source = None
        self.rain = RainProtection("not_configured", self.rain_memory)
        self.sun_memory = SunMemory()
        self.sunny: bool | None = None
        self.sun_power: float | None = None
        self.screen_decisions: dict[str, ScreenDecision] = {}
        self.screen_moves: dict[str, ScreenMove] = {}
        self.screen_overrides: dict = {}  # equipment -> until
        self.screen_last_move: dict = {}
        self.screen_store = None
        self.screen_targets: dict = {}
        self.screen_retry_at: dict = {}
        self.screen_retry_targets: dict = {}
        self.cover_tracker = CommandTracker(confirmation_seconds=240)
        self._screen_checks: list = []
        # 0.12 weather forecast of the coming hours: warm / neutral / cold day.
        self.forecast: Forecast | None = None
        self.forecast_error: str | None = None
        self.day_type = "unknown"
        self._cancel_forecast_retry = None
        self.booking_events: dict = {}
        self.booking_updated: dict = {}
        self.booking_errors: dict = {}
        self.booking_plans: dict = {}
        self.office_plans: dict = {}
        self._office_checks: list = []
        self.booking_results: dict = {}
        self.booking_interrupted: dict = {}
        self._booking_checks: list = []
        self._calendar_refreshing = False
        self.thermal_rates: dict = {}
        self.thermal_samples: dict = {}
        self.thermal_reports: dict = {}
        self.thermal_started: dict = {}
        self.thermal_context: dict = {}
        self.thermal_store = None
        # Coordination and manual overrides survive reloads, not HA restarts.
        self.overrides = {}
        self.last_transitions = {}
        self.coordination_memories = {}
        self.coordinations = {}
        self.unit_intents = {}
        # Distinguish our commands from external device changes.
        self.command_tracker = CommandTracker()
        self.memories = {
            group.id: GroupMemory("unknown", dt_util.utcnow())
            for group in building.groups
        }
        self._cancel_deadline = None
        self._closed = False

    @callback
    def refresh(self, event=None) -> None:
        """Refresh configured state values on an input event, without polling."""
        if self._closed:
            return
        now = dt_util.utcnow()
        if (
            event is not None
            and hasattr(event, "data")
            and event.data.get("entity_id")
            in {
                zone.calendar_entity
                for zone in self.building.zones
                if zone.calendar_entity
            }
        ):
            self.hass.async_create_task(self.async_update_bookings())
        sources = {}
        observations = {}
        for entity_id in self.building.entity_ids:
            state = self.hass.states.get(entity_id)
            value = state.state if state else None
            quality = (
                "missing"
                if state is None
                else value
                if value in ("unknown", "unavailable")
                else "observed"
            )
            observations[entity_id] = Observation(value, quality)
            if state:
                sources[entity_id] = SourceState(
                    value,
                    state.attributes.get("unit_of_measurement"),
                    state.last_reported,
                    not (
                        state.attributes.get("restored")
                        or state.attributes.get("assumed_state")
                    ),
                    state.last_changed,
                    self._climate(state) if entity_id.startswith("climate.") else None,
                )
        transitions = self._transitions(event, now)
        decisions = simulate(self.building, observations)
        if decisions != self.decisions:
            _LOGGER.debug(
                "Read-only simulation updated: %s", [d.reason for d in decisions]
            )
        self.observations = observations
        self.decisions = decisions
        self.evaluated_at = now
        self.outdoor_temperature = read(
            sources.get(self.building.settings.outdoor_temperature_entity),
            "temperature",
            now,
            self.building.settings.outdoor_max_age_seconds,
        )
        self._occupancy(sources, now)
        if self.active_capable:
            self._sync_zone_limits()
        self._remember_unit_options()
        bands, comfort, manual, manual_checks = {}, {}, {}, []
        for zone in self.building.zones:
            thermostat = self.thermostat(zone.id)
            bands[zone.id] = active_band(
                thermostat.preset, self.zone_occupancy[zone.id]
            )
            until = self.manual_until(zone.id)
            if thermostat.manual and (
                thermostat.manual_band != bands[zone.id][0]
                or until is None
                or now >= until
            ):
                # Temporary, like a manual override at the room controller:
                # it ends after the override time, or when the band changes
                # (presence, motion).
                thermostat = replace(thermostat, **CLEAR_MANUAL)
                self.thermostats[zone.id] = thermostat
                self._save_thermostats()
            elif thermostat.manual:
                manual_checks.append(until)
            if thermostat.fan_until:
                fan_until = dt_util.parse_datetime(thermostat.fan_until)
                if fan_until is None or now >= fan_until:
                    thermostat = replace(thermostat, fan_mode=None, fan_until=None)
                    self.thermostats[zone.id] = thermostat
                    self._save_thermostats()
                else:
                    manual_checks.append(fan_until)
            band = (thermostat.heating_limit, thermostat.cooling_limit)
            # "Use central climate limits" (0.13.3): the room's own limits on
            # its device only count when that is off.
            own = thermostat.has_band and not zone.use_building_limits
            comfort[zone.id] = band if own else None
            if thermostat.manual:
                manual[zone.id] = (
                    thermostat.manual_heating_limit,
                    thermostat.manual_cooling_limit,
                )
        self.zone_bands = bands
        self._manual_checks = manual_checks
        self.comfort = {
            zone.id: comfort_limits(zone, sources, now, comfort[zone.id])
            for zone in self.building.zones
        }
        self.zone_snapshots = {
            zone.id: collect_zone(
                self.building,
                zone,
                sources,
                now,
                self.outdoor_temperature,
                "comfort" if zone.id in manual else bands[zone.id][0],
                manual.get(zone.id, comfort[zone.id]),
                None if zone.use_building_limits else self.thermostat(zone.id).bands,
            )
            for zone in self.building.zones
        }
        self._room_state(now)
        settings = self.building.settings
        self.day_type = (
            day_type(
                self.forecast,
                now,
                settings.warm_day_temperature,
                settings.cold_day_temperature,
            )
            if settings.weather_entity
            else "unknown"
        )
        heating_margin, cooling_margin = demand_margins(
            self.day_type, settings.forecast_margin
        )
        demands = {}
        for zone in self.building.zones:
            demand = calculate_demand(
                zone,
                self.zone_snapshots[zone.id],
                self.zone_demands[zone.id].demand
                if zone.id in self.zone_demands
                else "unknown",
                0
                if self.booking_plans[zone.id].active
                or self.office_plans[zone.id].active
                else heating_margin,
                0
                if self.booking_plans[zone.id].active
                or self.office_plans[zone.id].active
                else cooling_margin,
            )
            # Thermostat mode: off asks nothing, heat/cool only one direction.
            demand = room_demand(self.thermostat(zone.id).hvac_mode, demand)
            if self.windows[zone.id] == "open" and demand.demand != "unknown":
                # An open window: the room asks for nothing (like VTherm).
                demand = ZoneDemand(
                    zone.id, "neutral", 0.0, 0.0, "window_open", demand.sensor_health
                )
            demand = self._booking_priority(zone, demand, now)
            demands[zone.id] = demand
        self.zone_demands = demands
        self._command_feedback(now)
        self._floor_prepare(sources, now)
        for group in self.building.groups:
            mode = observe_mode(group, sources, now)
            current = mode.value if mode.quality == "valid" else "unknown"
            if current != self.memories[group.id].current_mode:
                self.memories[group.id] = GroupMemory(current, now)
        previous_group_decisions = self.group_decisions
        self.group_decisions = {
            decision.group_id: decision
            for decision in evaluate(
                self.building, self.hvac_demands, sources, self.memories, now
            )
        }
        indoor_units = {}
        group_observations = {}
        for group in self.building.groups:
            units, observation = observe_group(self.building, group, sources)
            indoor_units.update({unit.equipment_id: unit for unit in units})
            group_observations[group.id] = observation
        for item in self.building.equipment:
            if item.kind == "hvac" and item.id not in indoor_units:
                indoor_units[item.id] = observe_unit(item, sources.get(item.entity_id))
        self.indoor_units = indoor_units
        self._learn_booking_rates(now)
        self.group_observations = group_observations
        self.mode_comparisons = {
            group_id: compare_modes(
                self.group_decisions[group_id].desired_mode, observation
            )
            for group_id, observation in group_observations.items()
        }
        if self.group_decisions != previous_group_decisions:
            _LOGGER.debug(
                "Climate simulation: %s",
                [
                    {
                        "current": d.current_mode,
                        "desired": d.desired_mode,
                        "proposed": d.proposed_mode,
                        "reason": d.reason,
                        "blocked": d.blocked_reason,
                    }
                    for d in self.group_decisions.values()
                ],
            )
        self._preview_auto_fans()
        self._plan(transitions, sources, now)
        if self.active_capable:
            self._floor_control(now)
            self._control(sources, now)
        if self._cancel_deadline:
            self._cancel_deadline()
            self._cancel_deadline = None
        deadlines = self._command_deadlines(now)
        for zone in self.building.zones:
            # Only update_based room measurements can expire.
            if max_age := max_age_for(zone.sensor_validation, zone.max_age_seconds):
                for field in ("temperature_entity", "humidity_entity", "co2_entity"):
                    source = sources.get(getattr(zone, field))
                    if source and source.reported_at:
                        deadlines.append(
                            source.reported_at + timedelta(seconds=max_age)
                        )
        outside = sources.get(self.building.settings.outdoor_temperature_entity)
        if (
            outside
            and outside.reported_at
            and self.building.settings.outdoor_max_age_seconds
        ):
            deadlines.append(
                outside.reported_at
                + timedelta(seconds=self.building.settings.outdoor_max_age_seconds)
            )
        for group in self.building.groups:
            source = sources.get(group.mode_entity)
            if group.max_age_seconds and source and source.reported_at:
                deadlines.append(
                    source.reported_at + timedelta(seconds=group.max_age_seconds)
                )
            if deadline := self.group_decisions[group.id].blocked_until:
                deadlines.append(deadline)
        # Override expiry and anti-cycling ends re-evaluate without new events.
        deadlines.extend(override.expires_at for override in self.overrides.values())
        deadlines.extend(
            intent.blocked_until
            for intent in self.unit_intents.values()
            if intent.blocked_until
        )
        deadlines.extend(
            item.waiting_until
            for item in self.coordinations.values()
            if item.waiting_until
        )
        deadlines.extend(due for due, _, _ in self.pending_offs)
        deadlines.extend(self._booking_checks)
        deadlines.extend(self._office_checks)
        deadlines.extend(self._motion_checks)
        deadlines.extend(self._setpoint_checks)
        deadlines.extend(self._window_checks)
        deadlines.extend(self._manual_checks)
        deadlines.extend(self._screen_checks)
        deadlines.extend(self._floor_checks)
        for group_id, control in self.mode_controls.items():
            if control.blocked_until:
                deadlines.append(control.blocked_until)
            group = next(g for g in self.building.groups if g.id == group_id)
            if check := next_correction_check(
                self._group_units(group),
                self._changed_at(sources),
                self.preset_memories.get(group_id, PresetMemory()),
                control.target_mode,
                now,
            ):
                deadlines.append(check)
        future = [deadline for deadline in deadlines if deadline > now]
        if future:
            self._cancel_deadline = async_track_point_in_utc_time(
                self.hass, self.refresh, min(future)
            )
        async_dispatcher_send(self.hass, self.signal)

    def _occupancy(self, sources, now) -> None:
        """Building presence and zone motion decide each zone's band."""
        settings = self.building.settings
        self.presence = read_presence(
            settings.presence_entity, sources.get(settings.presence_entity)
        )
        checks, result = [], {}
        self._booking_checks = []
        self._office_checks = []
        for zone in self.building.zones:
            entity = zone.occupancy_entity
            motion, memory, check = motion_state(
                entity,
                sources.get(entity) if entity else None,
                self.motion_memories.get(zone.id, MotionMemory()),
                settings.motion_delay_seconds,
                settings.motion_off_delay_minutes,
                now,
            )
            self.motion_memories[zone.id] = memory
            if check:
                checks.append(check)
            booked = self._booking_occupancy(
                zone, occupancy(self.presence, motion), sources, now
            )
            result[zone.id] = self._office_occupancy(zone, booked, sources, now)
        self.zone_occupancy = result
        self._motion_checks = checks

    # --- Room thermostats (0.9) -----------------------------------------------

    def _room_state(self, now) -> None:
        """Window, safety and temperature trend per zone (0.10)."""
        settings = self.building.settings
        checks = []
        for zone in self.building.zones:
            snapshot = self.zone_snapshots[zone.id]
            state, check = window_state(
                snapshot.contacts,
                settings.window_delay_seconds,
                self.thermostat(zone.id).window_bypass,
                now,
            )
            self.windows[zone.id] = state
            if check:
                checks.append(check)
            self.safety[zone.id] = safety_state(snapshot.temperature)
            reading = snapshot.temperature
            if reading.quality == "valid" and isinstance(reading.value, (int, float)):
                trend = update_trend(
                    self.trends.get(zone.id),
                    float(reading.value),
                    reading.reported_at or now,
                    now,
                )
                if trend is not None:
                    self.trends[zone.id] = trend
        self._window_checks = checks

    async def async_update_forecast(self, _now=None) -> None:
        """Fetch the hourly forecast of the coming hours (0.12), then refresh."""
        entity = self.building.settings.weather_entity
        if not entity:
            self.forecast = None
            return
        now = dt_util.utcnow()
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            # Weather not loaded yet or no hourly forecast: keep the last one
            # until it is too old; the next attempt follows on the interval.
            self.forecast_error = type(err).__name__
            _LOGGER.debug("Weather forecast of %s unavailable: %s", entity, err)
            if self.forecast is None and self._cancel_forecast_retry is None:
                # At startup the weather integration may not be loaded yet.
                self._cancel_forecast_retry = async_call_later(
                    self.hass, RETRY_SECONDS, self._retry_forecast
                )
        else:
            entries = ((response or {}).get(entity) or {}).get("forecast") or []
            state = self.hass.states.get(entity)
            unit = state.attributes.get("temperature_unit") if state else None
            unit = unit or self.hass.config.units.temperature_unit
            self.forecast = summarize_forecast(entries, now, temperature_unit=unit)
            self.forecast_error = (
                None
                if self.forecast.maximum is not None
                else "empty_forecast"
                if not entries
                else "invalid_forecast"
            )
        self.refresh()

    async def _retry_forecast(self, _now=None) -> None:
        self._cancel_forecast_retry = None
        await self.async_update_forecast()

    @property
    def forecast_interval(self) -> timedelta:
        return timedelta(minutes=REFRESH_MINUTES)

    # Memory that must survive a reload of the central configuration (0.13.4):
    # without it a reload forgot hysteresis, manual overrides, anti-cycling
    # and pending commands, and running units could be switched off.
    CARRIED = (
        "command_receipts",
        "command_retries",
        "group_switches",
        "room_switches",
        "booking_events",
        "booking_updated",
        "booking_errors",
        "booking_plans",
        "booking_results",
        "booking_interrupted",
        "thermal_rates",
        "zone_demands",
        "hvac_demands",
        "heating_watches",
        "floor_overrides",
        "coordination_memories",
        "overrides",
        "last_transitions",
        "command_tracker",
        "setpoint_tracker",
        "cover_tracker",
        "pending_offs",
        "setpoint_sent",
        "mode_applied",
        "fan_applied",
        "fan_sent_at",
        "vane_applied",
        "motion_memories",
        "trends",
        "sun_memory",
        "wind_memory",
        "wind_policy",
        "rain_memory",
        "rain_source",
        "sunny",
        "screen_overrides",
        "screen_last_move",
        "screen_targets",
        "screen_retry_at",
        "screen_retry_targets",
        "screen_decisions",
        "forecast",
        "forecast_error",
        "command_log",
        "preset_memories",
        "preset_applications",
        "mode_active",
    )

    def adopt(self, previous: "BuildingRuntime") -> None:
        """Take over the in-memory state of the runtime this one replaces."""
        for name in self.CARRIED:
            setattr(self, name, getattr(previous, name))
        calendars = {
            zone.calendar_entity for zone in self.building.zones if zone.calendar_entity
        }
        for name in ("booking_events", "booking_updated", "booking_errors"):
            setattr(
                self,
                name,
                {
                    key: value
                    for key, value in getattr(self, name).items()
                    if key in calendars
                },
            )
        if (
            self.building.settings.office_schedule_entity
            == previous.building.settings.office_schedule_entity
        ):
            self.office_plans = {
                zone.id: previous.office_plans[zone.id]
                for zone in self.building.zones
                if not zone.calendar_entity and zone.id in previous.office_plans
            }
        previous_calendars = {
            zone.id: zone.calendar_entity for zone in previous.building.zones
        }
        self.booking_plans = {
            zone.id: self.booking_plans[zone.id]
            for zone in self.building.zones
            if zone.id in self.booking_plans
            and zone.calendar_entity == previous_calendars.get(zone.id)
        }
        for name in ("booking_results", "booking_interrupted"):
            setattr(
                self,
                name,
                {
                    zone.id: getattr(self, name)[zone.id]
                    for zone in self.building.zones
                    if zone.id in getattr(self, name)
                    and zone.calendar_entity == previous_calendars.get(zone.id)
                    and zone.calendar_entity
                },
            )
        # Removed/disabled equipment must not retain delayed hardware commands.
        equipment = {(item.id, item.entity_id) for item in self.building.equipment}
        self.pending_offs = [
            pending
            for pending in self.pending_offs
            if (pending[1].equipment_id, pending[1].entity_id) in equipment
        ]
        valid_entities = self.building.entity_ids
        valid_ids = {item.id for item in self.building.equipment}
        self.room_switches = {
            key: value for key, value in self.room_switches.items() if key in valid_ids
        }
        self.command_receipts = {
            key: value
            for key, value in self.command_receipts.items()
            if value.command.equipment_id in valid_ids
            and value.command.entity_id in valid_entities
        }
        self.command_retries = {
            key: value
            for key, value in self.command_retries.items()
            if value[0].equipment_id in valid_ids
            and value[0].entity_id in valid_entities
        }
        for tracker in (
            self.command_tracker,
            self.setpoint_tracker,
            self.cover_tracker,
        ):
            for pending in tracker.pending:
                if pending.entity_id not in valid_entities:
                    tracker.cancel(pending.entity_id)
        groups = {group.id for group in self.building.groups}
        self.group_switches = {
            key: value for key, value in self.group_switches.items() if key in groups
        }
        self.preset_applications = {
            group_id: application
            for group_id, application in self.preset_applications.items()
            if group_id in groups
        }
        for group_id, application in self.preset_applications.items():
            members = {
                (item.id, item.entity_id)
                for item in self.building.equipment
                if item.group_id == group_id
            }
            application.commands = {
                key: command
                for key, command in application.commands.items()
                if (command.equipment_id, command.entity_id) in members
            }
            application.in_flight.intersection_update(application.commands)
        self.memories.update(
            {
                group_id: memory
                for group_id, memory in previous.memories.items()
                if group_id in self.memories
            }
        )
        # A service can finish while setup is still loading the new runtime.
        self._save_presets()

    @callback
    def apply_building(self, building: Building) -> None:
        """New limits set on the central device: same inputs, no reload."""
        self.building = building
        self.refresh()

    def _sync_zone_limits(self) -> None:
        """ "Use central climate limits" (0.13.3) decides the room's own limits.

        On: the room has no own limits (earlier ones are cleared). Off: the
        room gets the central limits once, as its own starting values; after
        that they are the room's, changed only on its device.
        """
        changed = False
        for zone in self.building.zones:
            thermostat = self.thermostat(zone.id)
            own = {key: getattr(thermostat, key) for key in ZONE_LIMIT_KEYS}
            if zone.use_building_limits:
                if any(value is not None for value in own.values()):
                    self.thermostats[zone.id] = replace(
                        thermostat, **dict.fromkeys(ZONE_LIMIT_KEYS)
                    )
                    changed = True
            elif all(value is None for value in own.values()):
                # The zone model already holds the central values it follows.
                start = {key: getattr(zone, key) for key in ZONE_LIMIT_KEYS}
                if start["heating_limit"] is None or start["cooling_limit"] is None:
                    start.pop("heating_limit")
                    start.pop("cooling_limit")  # Central limits are entities.
                self.thermostats[zone.id] = replace(thermostat, **start)
                changed = True
        if changed:
            self._save_thermostats()

    def thermostat(self, zone_id: str) -> Thermostat:
        return self.thermostats.get(zone_id) or Thermostat()

    def load_thermostats(self, data: dict | None) -> None:
        for zone_id, item in ((data or {}).get("zones") or {}).items():
            self.thermostats[zone_id] = Thermostat.from_dict(item)

    @callback
    def set_thermostat(self, zone_id: str, **changes) -> Thermostat:
        """Validated thermostat change (ValueError with a code), then refresh."""
        zone = next((z for z in self.building.zones if z.id == zone_id), None)
        if zone is None or not self.active_capable:
            raise ValueError("zone_not_loaded")
        if "fan_mode" in changes:
            # 0.13.5: auto (or none) is the automatic fan; a fixed speed holds
            # for the manual override time, then the fan is automatic again.
            if changes["fan_mode"] in (None, AUTO_FAN):
                changes["fan_mode"], changes["fan_until"] = None, None
            else:
                minutes = self.building.settings.manual_override_duration_minutes
                changes["fan_until"] = (
                    dt_util.utcnow() + timedelta(minutes=minutes)
                ).isoformat()
        if changes.get("manual_band") is not None:
            # A manual target is temporary: as long as a manual override.
            minutes = self.building.settings.manual_override_duration_minutes
            changes["manual_until"] = (
                dt_util.utcnow() + timedelta(minutes=minutes)
            ).isoformat()
        new = update_thermostat(self.thermostat(zone_id), zone.hysteresis, **changes)
        settings = self.building.settings
        for band in ("eco", "away"):
            # The room's eco/away band with the central value for unset limits.
            heat, cool = (
                getattr(new, key)
                if getattr(new, key) is not None
                else getattr(zone, key)
                if getattr(zone, key) is not None
                else getattr(settings, key)
                for key in (f"{band}_heating_limit", f"{band}_cooling_limit")
            )
            if heat + 2 * zone.hysteresis >= cool:
                raise ValueError("invalid_limits")
        self.thermostats[zone_id] = new
        self._save_thermostats()
        self.refresh()
        return new

    def manual_until(self, zone_id: str):
        value = self.thermostat(zone_id).manual_until
        return dt_util.parse_datetime(value) if value else None

    def _save_thermostats(self) -> None:
        if self.thermostat_cache is not None:
            for key, value in self.thermostats.items():
                self.thermostat_cache[key] = value.as_dict()
        if self.thermostat_store is not None:
            self.hass.async_create_task(
                self.thermostat_store.async_save(
                    {
                        "zones": {
                            key: value.as_dict()
                            for key, value in self.thermostats.items()
                        }
                    }
                )
            )

    def regulated(self, zone_id: str | None) -> bool:
        """Room regulation really acts (central layout and switched on)."""
        return self.active_capable and self.thermostat(zone_id).regulation

    def _group_block(self, group_id: str | None) -> str | None:
        """The group's stop switch also gates room and vane commands."""
        if group_id is None or self.mode_active.get(group_id) is True:
            return None
        return (
            "group_disabled"
            if self.mode_active.get(group_id) is False
            else "waiting_for_switch_state"
        )

    def _safe_modes(self, item) -> frozenset[str]:
        if item.group_id is None:
            return frozenset({"heat", "cool"})
        if self._group_block(item.group_id) or item.group_id in self.group_switches:
            return frozenset()
        memory = self.preset_memories.get(item.group_id, PresetMemory())
        return safe_modes((), memory.mode)

    def _regulate(self, sources, now, busy: set[str], available: bool) -> None:
        """Switch regulated rooms' units on/off and steer their setpoints."""
        settings = self.building.settings
        zones = {zone.id: zone for zone in self.building.zones}
        controls, plans, checks = {}, {}, []
        for item in self.building.equipment:
            if item.kind != "hvac" or item.id not in self.unit_intents:
                continue
            if blocked := self._group_block(item.group_id):
                controls[item.id] = RoomUnitControl(item.id, blocked)
                plans[item.id] = SetpointPlan(blocked)
                continue
            intent = self.unit_intents[item.id]
            enabled = self.regulated(item.zone_id)
            thermostat = self.thermostat(item.zone_id)
            unit = self.indoor_units.get(item.id)
            unit_busy = item.id in busy or self.command_tracker.is_pending(
                item.entity_id, now
            )
            override = self.overrides.get(item.id)
            direct = thermostat.hvac_mode in UNIT_MODES
            target = thermostat.hvac_mode if direct else intent.desired_mode
            needs_changeover = (
                enabled
                and unit is not None
                and unit.available
                and not (override and override.active(now))
                and (
                    item.group_id is None
                    or direct
                    and (
                        self.mode_applied.get(item.id) != target
                        or unit.selected_mode == "off"
                    )
                )
            )
            if (
                needs_changeover
                and self.windows.get(item.zone_id) != "open"
                and target in ("heat", "cool", "dry")
                and ("cool" if target == "dry" else target) in self._safe_modes(item)
            ):
                timing, until = self._room_changeover(item, unit, target, now)
                if timing:
                    controls[item.id] = timing
                    plans[item.id] = SetpointPlan(timing.status)
                    if timing.command and available:
                        self._execute(timing.command, now)
                    if until:
                        checks.append(until)
                    continue
            else:
                uncertain = (
                    unit is None
                    or not unit.available
                    or intent.zone_demand == "unknown"
                )
                if enabled and thermostat.hvac_mode != "off" and uncertain:
                    if (
                        unit is None or not unit.available
                    ) and item.id in self.room_switches:
                        self.room_switches[item.id].stopped_at = None
                else:
                    self.room_switches.pop(item.id, None)
            if self.windows.get(item.zone_id) == "open" and enabled:
                # Open window: running units go off at once (no minimum run
                # time), nothing starts until it closes.
                running = unit is not None and unit.selected_mode not in (
                    "off",
                    "unknown",
                )
                if running and not unit_busy:
                    control = RoomUnitControl(
                        item.id,
                        "turn_off",
                        ModeCommand(
                            item.id, item.entity_id, "turn_off", None, 0, "window_open"
                        ),
                    )
                else:
                    control = RoomUnitControl(item.id, "window_open")
            elif thermostat.hvac_mode in UNIT_MODES:
                if enabled and override and override.active(now):
                    control = RoomUnitControl(item.id, "manual_override")
                elif unit is None or not unit.available:
                    control = RoomUnitControl(item.id, "indoor_unit_unavailable")
                elif enabled and (until := self._restart_blocked(item, unit, now)):
                    control = RoomUnitControl(item.id, "minimum_off_time")
                    checks.append(until)
                else:
                    control = plan_room_mode(
                        thermostat.hvac_mode,
                        unit.selected_mode,
                        item.id,
                        item.entity_id,
                        enabled=enabled,
                        safe_modes=self._safe_modes(item),
                        busy=unit_busy,
                        applied=self.mode_applied.get(item.id),
                    )
            else:
                self.mode_applied.pop(item.id, None)
                control = plan_room_unit(
                    intent.action,
                    intent.reason,
                    item.id,
                    item.entity_id,
                    enabled=enabled,
                    safe_modes=self._safe_modes(item),
                    busy=unit_busy,
                )
            if control.command and not available:
                control = RoomUnitControl(item.id, "climate_service_unavailable")
            controls[item.id] = control
            if control.command:
                self._execute(control.command, now)
            elif enabled and unit and unit.available and not unit_busy:
                if check := self._fan(item, unit, thermostat, now, available):
                    checks.append(check)
            plan = self._setpoint_plan(item, zones[item.zone_id], settings, now)
            if (
                plan.send
                and enabled
                and available
                and control.command is None
                # A gateway may still report heat/cool after a mode or off
                # command. Wait for confirmation before steering its target.
                and not unit_busy
                and not self.setpoint_tracker.is_pending(item.entity_id, now)
            ):
                self._execute(
                    ModeCommand(
                        item.id,
                        item.entity_id,
                        "set_temperature",
                        None,
                        0,
                        "self_regulation",
                        plan.setpoint,
                    ),
                    now,
                )
            elif plan.send:
                plan = replace(plan, send=False)
            if plan.next_check and enabled:
                checks.append(plan.next_check)
            plans[item.id] = plan
        self.room_controls = controls
        self.setpoint_plans = plans
        self._setpoint_checks = checks

    def _restart_blocked(self, item, unit, now):
        """Restart protection for a unit that is off (dry / fan_only rooms)."""
        if unit.selected_mode != "off":
            return None
        state = self.hass.states.get(item.entity_id)
        if state is None or state.last_changed is None:
            return None
        until = state.last_changed + timedelta(
            seconds=self.building.settings.min_off_seconds
        )
        return until if now < until else None

    def _vanes(self, now) -> None:
        """Vane up when the room's units cool, down when they heat (0.10).

        Set once per start or mode change; a vane moved by hand afterwards is
        not fought. Works with the outdoor-stand control, also without room
        regulation, because it only follows what the units already do.
        """
        for zone in self.building.zones:
            if not zone.vane_entity:
                continue
            units = [
                item
                for item in self.building.equipment
                if item.kind == "hvac" and item.zone_id == zone.id
            ]
            if any(self._group_block(item.group_id) for item in units):
                self.vane_applied.pop(zone.id, None)
                continue
            modes = {
                unit.selected_mode
                for item in units
                if (unit := self.indoor_units.get(item.id)) and unit.available
            }
            mode = vane_mode(modes)
            if mode is None:
                self.vane_applied.pop(zone.id, None)  # Off: set again on start.
                continue
            if self.vane_applied.get(zone.id) == mode:
                continue
            command = vane_command(
                zone.vane_entity, mode, zone.vane_cool_value, zone.vane_heat_value
            )
            if command is None:
                continue
            domain, service, data = command
            if not self.hass.services.has_service(domain, service):
                continue
            self._execute(
                ModeCommand(
                    units[0].id if units else zone.id,
                    zone.vane_entity,
                    service,
                    None,
                    0,
                    f"vane_{mode}",
                    domain=domain,
                    value=data.get("option", data.get("value", service)),
                ),
                now,
            )

    def _preview_auto_fans(self) -> None:
        """The speed the automatic fan would choose now, for every room with
        indoor units (0.13.3), also in simulation or with another fan mode.

        The mode is that of a running unit, else the room's demand. A running
        automatic fan (``_fan``) overwrites it with the speed it really sends.
        """
        previews = {}
        for zone in self.building.zones:
            units = [
                unit
                for item in self.building.equipment
                if item.zone_id == zone.id
                and item.kind == "hvac"
                and (unit := self.indoor_units.get(item.id))
            ]
            speeds = fan_speeds(self.unit_options(zone.id)[1])
            if not units or not speeds:
                continue
            running = next(
                (u.selected_mode for u in units if u.selected_mode in UNIT_RUNNING),
                None,
            )
            demand = self.zone_demands[zone.id].demand
            mode = running or {"heating_demand": "heat", "cooling_demand": "cool"}.get(
                demand, "off"
            )
            snapshot = self.zone_snapshots[zone.id]
            previews[zone.id] = auto_fan(
                speeds,
                mode,
                snapshot.temperature.value
                if snapshot.temperature.quality == "valid"
                else None,
                snapshot.heating_limit.value,
                snapshot.cooling_limit.value,
                zone.hysteresis,
                self.building.settings.auto_fan_step,
            )
        self.auto_fans = previews

    def fan_choice(self, zone_id: str) -> str:
        """The thermostat's fan mode; none chosen is automatic (0.13.5)."""
        return self.thermostat(zone_id).fan_mode or AUTO_FAN

    def zone_auto_fan(self, zone_id: str) -> bool:
        """The thermostat runs Zonentra's automatic fan (units have no auto)."""
        if not self.thermostat(zone_id).fan_control:
            return False
        return self.fan_choice(zone_id) == AUTO_FAN and (
            AUTO_FAN not in self.unit_options(zone_id)[1]
        )

    def _fan(self, item, unit, thermostat, now, available):
        """Pass the thermostat's fan speed to a running unit, once per change.

        With the automatic fan the speed follows the distance to the target,
        changed at most once per setpoint interval.
        """
        override = self.overrides.get(item.id)
        if override and override.active(now):
            self.fan_applied.pop(item.id, None)
            return override.expires_at
        state = self.hass.states.get(item.entity_id)
        supported = tuple((state.attributes.get("fan_modes") or ()) if state else ())
        if not supported or not thermostat.fan_control:
            # No speeds, or the fan is left to the room (switch off, 0.13.5).
            self.fan_applied.pop(item.id, None)
            return None
        # The thermostat's standard name (0.13.2) -> the unit's own speed.
        desired = unit_fan(self.fan_choice(item.zone_id), supported)
        if self.zone_auto_fan(item.zone_id):
            snapshot = self.zone_snapshots[item.zone_id]
            zone = next(z for z in self.building.zones if z.id == item.zone_id)
            desired = auto_fan(
                fan_speeds(supported),
                unit.selected_mode,
                snapshot.temperature.value
                if snapshot.temperature.quality == "valid"
                else None,
                snapshot.heating_limit.value,
                snapshot.cooling_limit.value,
                zone.hysteresis,
                self.building.settings.auto_fan_step,
            )
            self.auto_fans[item.zone_id] = desired
            last = self.fan_sent_at.get(item.id)
            interval = self.building.settings.setpoint_interval_seconds
            if last and (now - last).total_seconds() < interval:
                if desired != unit.fan_mode:
                    return last + timedelta(seconds=interval)
                return None
        fan = plan_fan(
            desired,
            unit.selected_mode,
            unit.fan_mode,
            self.fan_applied.get(item.id),
            supported,
        )
        if unit.fan_mode == desired:
            self.fan_applied[item.id] = desired
        if fan is None or not available:
            return None
        self._execute(
            ModeCommand(
                item.id,
                item.entity_id,
                "set_fan_mode",
                None,
                0,
                "thermostat_fan_mode",
                fan_mode=fan,
            ),
            now,
        )

    def unit_options(self, zone_id: str) -> tuple[frozenset[str], tuple[str, ...]]:
        """HVAC modes and fan speeds that every indoor unit of a zone supports.

        0.13.6: the last known options of the room (stored with its
        thermostat), so they do not disappear while a unit is unavailable,
        e.g. during a restart; HomeKit fixes an accessory's features when it
        starts.
        """
        thermostat = self.thermostat(zone_id)
        return frozenset(thermostat.unit_hvac_modes), thermostat.unit_fan_modes

    def _observed_unit_options(self, zone_id: str):
        """The options of the units now; None while one is not reporting."""
        states = [
            self.hass.states.get(item.entity_id)
            for item in self.building.equipment
            if item.kind == "hvac" and item.zone_id == zone_id
        ]
        if not states or any(
            state is None
            or state.state in ("unavailable", "unknown")
            or not state.attributes.get("hvac_modes")
            for state in states
        ):
            return None
        modes = tuple(
            mode
            for mode in states[0].attributes.get("hvac_modes") or ()
            if all(mode in state.attributes.get("hvac_modes") for state in states)
        )
        fans = tuple(
            fan
            for fan in states[0].attributes.get("fan_modes") or ()
            if all(fan in (state.attributes.get("fan_modes") or ()) for state in states)
        )
        return tuple(str(mode) for mode in modes), tuple(str(fan) for fan in fans)

    def _remember_unit_options(self) -> None:
        """Store the units' options per room when every unit reports them."""
        changed = False
        for zone in self.building.zones:
            observed = self._observed_unit_options(zone.id)
            if observed is None:
                continue
            thermostat = self.thermostat(zone.id)
            if (thermostat.unit_hvac_modes, thermostat.unit_fan_modes) != observed:
                self.thermostats[zone.id] = replace(
                    thermostat, unit_hvac_modes=observed[0], unit_fan_modes=observed[1]
                )
                changed = True
        if changed:
            self._save_thermostats()

    def _climate_entity(self, entity_id):
        component = self.hass.data.get(DATA_INSTANCES, {}).get("climate")
        return component.get_entity(entity_id) if component else None

    def _setpoint_plan(self, item, zone, settings, now) -> SetpointPlan:
        unit = self.indoor_units.get(item.id)
        override = self.overrides.get(item.id)
        if unit is None or not unit.available:
            return SetpointPlan("unit_unavailable")
        if override and override.active(now):
            return SetpointPlan("manual_override")
        snapshot = self.zone_snapshots[item.zone_id]
        state = self.hass.states.get(item.entity_id)
        attributes = state.attributes if state else {}
        source_unit = self.hass.config.units.temperature_unit
        # HA displays absolute temperatures in system units, but publishes the
        # step in the climate entity's native units without conversion.
        entity = self._climate_entity(item.entity_id)
        step_unit = entity.temperature_unit if entity else source_unit
        step = convert_temperature(
            attributes.get("target_temp_step"), step_unit, interval=True
        )
        plan = plan_setpoint(
            unit.selected_mode,
            snapshot.heating_limit.value,
            snapshot.cooling_limit.value,
            zone.hysteresis,
            snapshot.temperature.value
            if snapshot.temperature.quality == "valid"
            else None,
            convert_temperature(unit.target_temperature, source_unit),
            self.setpoint_sent.get(item.id),
            now,
            max_offset=settings.setpoint_max_offset,
            interval_seconds=settings.setpoint_interval_seconds,
            step=step,
            step_origin=(convert_temperature(0, step_unit) or 0)
            if step is not None and step > 0
            else 0,
            min_temp=convert_temperature(entity.min_temp, step_unit)
            if entity is not None
            else convert_temperature(attributes.get("min_temp"), source_unit),
            max_temp=convert_temperature(entity.max_temp, step_unit)
            if entity is not None
            else convert_temperature(attributes.get("max_temp"), source_unit),
        )
        if entity is not None and plan.setpoint is not None:
            # State attributes can be rounded more coarsely than the device step.
            shown = display_temp(
                self.hass, plan.setpoint, UnitOfTemperature.CELSIUS, entity.precision
            )
            if _setpoint(shown) == _setpoint(unit.target_temperature):
                plan = replace(plan, status="in_sync", send=False, next_check=None)
        return plan

    # --- Active outdoor-stand control (0.7) -----------------------------------

    def load_presets(self, data: dict | None) -> None:
        """Restore the last applied group modes, so a restart does not pulse."""
        for group_id, item in ((data or {}).get("groups") or {}).items():
            changed = dt_util.parse_datetime(item.get("changed_at") or "")
            self.preset_memories[group_id] = PresetMemory(item.get("mode"), changed)

    @callback
    def set_mode_active(self, group_id: str, active: bool) -> None:
        self.mode_active[group_id] = active
        if not active:
            # Emergency stop: nothing more is sent, not even a pending pulse-off.
            group_units = {
                item.id for item in self.building.equipment if item.group_id == group_id
            }
            self.pending_offs = [
                item
                for item in self.pending_offs
                if item[1].equipment_id not in group_units
            ]
            # Late responses from an earlier preset must not schedule a pulse.
            self.preset_applications.pop(group_id, None)
            self.group_switches.pop(group_id, None)
            self.command_receipts = {
                key: receipt
                for key, receipt in self.command_receipts.items()
                if receipt.command.equipment_id not in group_units
            }
            self.command_retries = {
                key: retry
                for key, retry in self.command_retries.items()
                if retry[0].equipment_id not in group_units
            }
            for item in self.building.equipment:
                if item.id not in group_units:
                    continue
                self.mode_applied.pop(item.id, None)
                self.fan_applied.pop(item.id, None)
                self.fan_sent_at.pop(item.id, None)
                self.setpoint_sent.pop(item.id, None)
                self.setpoint_tracker.cancel(item.entity_id)
        self.refresh()

    def _group_units(self, group):
        return tuple(
            self.indoor_units[item.id]
            for item in self.building.equipment
            if item.kind == "hvac"
            and item.group_id == group.id
            and item.id in self.indoor_units
        )

    def _changed_at(self, sources):
        return {
            item.id: sources[item.entity_id].changed_at
            for item in self.building.equipment
            if item.kind == "hvac" and item.entity_id in sources
        }

    def _control(self, sources, now) -> None:
        """Plan and send mode commands; every command is tracked and logged."""
        available = self.hass.services.has_service("climate", "set_hvac_mode")
        changed_at = self._changed_at(sources)
        entity_ids = {item.id: item.entity_id for item in self.building.equipment}
        controls = {}
        for group in self.building.groups:
            previous = self.preset_memories.get(group.id, PresetMemory())
            application = self.preset_applications.get(group.id)
            target = self.coordinations[group.id].group_mode
            enabled = self.mode_active.get(group.id)
            if (
                application
                and not application.in_flight
                and (not enabled or application.memory.mode != target)
            ):
                del self.preset_applications[group.id]
                application = None
            timing = self._group_timing(group, target, previous, now)
            if timing is not None:
                controls[group.id] = timing
                for command in timing.commands:
                    self._execute(command, now)
                continue
            if application:
                commands = ()
                until = application.retry_at
                if enabled and (until is None or now >= until):
                    units = {
                        unit.equipment_id: unit for unit in self._group_units(group)
                    }
                    for key, command in tuple(application.commands.items()):
                        unit = units.get(key)
                        if (
                            key in application.in_flight
                            or unit is None
                            or not unit.available
                        ):
                            continue
                        if unit.selected_mode in COMPATIBLE[application.memory.mode]:
                            del application.commands[key]
                        else:
                            application.commands[key] = replace(
                                command,
                                reason="preset_pulse"
                                if unit.selected_mode == "off"
                                and key
                                not in getattr(
                                    self.group_switches.get(group.id),
                                    "resume_units",
                                    (),
                                )
                                else "switch_running_unit",
                            )
                    commands = tuple(
                        command
                        for key, command in application.commands.items()
                        if key not in application.in_flight
                        and key in units
                        and units[key].available
                    )
                    if (
                        not commands
                        and application.commands
                        and not application.in_flight
                    ):
                        application.retry_at = now + timedelta(
                            seconds=PRESET_RETRY_SECONDS
                        )
                controls[group.id] = self._apply_preset(
                    group.id, application, commands, previous, now, available
                )
                continue
            control, memory = plan_mode_commands(
                group.id,
                target,
                self._group_units(group),
                entity_ids,
                changed_at,
                previous,
                group.min_switch_seconds,
                now,
                enabled=enabled,
            )
            progress = self.group_switches.get(group.id)
            if progress and progress.ready:
                resume = progress.resume_units
                commands = [
                    replace(command, reason="switch_running_unit")
                    if command.equipment_id in resume
                    else command
                    for command in control.commands
                    if not (command.equipment_id in resume and command.delay_seconds)
                ]
                covered = {command.equipment_id for command in commands}
                commands.extend(
                    ModeCommand(
                        unit.equipment_id,
                        entity_ids[unit.equipment_id],
                        "set_hvac_mode",
                        target,
                        0,
                        "switch_running_unit",
                    )
                    for unit in self._group_units(group)
                    if unit.equipment_id in resume
                    and unit.equipment_id not in covered
                    and unit.available
                    and unit.selected_mode == "off"
                )
                if commands:
                    control = replace(
                        control, status="group_mode_applied", commands=tuple(commands)
                    )
                    memory = PresetMemory(target, now, previous.corrected)
            if control.commands and not available:
                control = replace(
                    control, status="climate_service_unavailable", commands=()
                )
                memory = previous
            if control.status == "group_mode_applied" and control.commands:
                application = PresetApplication(
                    memory,
                    {
                        command.equipment_id: command
                        for command in control.commands
                        if not command.delay_seconds
                    },
                    set(),
                )
                self.preset_applications[group.id] = application
                controls[group.id] = self._apply_preset(
                    group.id,
                    application,
                    tuple(application.commands.values()),
                    previous,
                    now,
                    available,
                )
                continue
            controls[group.id] = control
            if progress and progress.ready and not control.commands:
                self.group_switches.pop(group.id, None)
            if memory != previous:
                self.preset_memories[group.id] = memory
                if memory.mode != previous.mode:
                    self._save_presets()
            for command in control.commands:
                self._execute(command, now)
        self.mode_controls = controls
        due = [item for item in self.pending_offs if item[0] <= now]
        self.pending_offs = [item for item in self.pending_offs if item[0] > now]
        busy = {
            command.equipment_id
            for control in controls.values()
            for command in control.commands
        }
        for _, command, target in due:
            source = sources.get(command.entity_id)
            if source is None or source.state != target:
                # Someone changed the unit during the pulse: leave it alone.
                self._log(command, now, "skipped_changed_during_pulse")
                continue
            intent = self.unit_intents.get(command.equipment_id)
            zone_id = intent.zone_id if intent else None
            if self.regulated(zone_id) and intent.desired_mode == target:
                # 0.9: the regulated room needs the unit on in this mode.
                self._log(command, now, "skipped_room_demand")
                continue
            busy.add(command.equipment_id)
            self._execute(
                command,
                now,
                on_result=lambda success, command=command, target=target: (
                    self._pulse_result(command, target, success)
                ),
            )
        busy |= {command.equipment_id for _, command, _ in self.pending_offs}
        busy |= {
            key
            for application in self.preset_applications.values()
            for key in application.commands
        }
        self._regulate(sources, now, busy, available)
        self._vanes(now)
        self._shading(sources, now)

    def _current_runtime(self):
        """A service response may arrive after a central reload."""
        previous = self.hass.data.get(DOMAIN, {}).get("runtimes", {})
        if previous.get(self.building.id) is self:
            return self  # Setup has not adopted this runtime's state yet.
        entry = self.hass.config_entries.async_get_entry(self.building.id)
        runtime = getattr(entry, "runtime_data", self) if entry else self
        return runtime

    def _apply_preset(self, group_id, application, commands, previous, now, available):
        if not application.commands:
            self.preset_memories[group_id] = application.memory
            del self.preset_applications[group_id]
            self.group_switches.pop(group_id, None)
            self._save_presets()
            return ModeControl(
                group_id,
                application.memory.mode,
                application.memory.mode,
                "group_mode_applied",
                None,
                (),
            )
        if not available:
            commands = ()
            application.retry_at = now + timedelta(seconds=PRESET_RETRY_SECONDS)
        waiting = [
            until for command in commands if (until := self._command_wait(command, now))
        ]
        commands = tuple(
            command for command in commands if not self._command_wait(command, now)
        )
        if waiting:
            application.retry_at = min(waiting)
        application.in_flight.update(command.equipment_id for command in commands)
        for command in commands:
            self._execute(
                command,
                now,
                on_result=lambda success, command=command: self._preset_result(
                    group_id, application, command, success
                ),
            )
        return ModeControl(
            group_id,
            application.memory.mode,
            previous.mode,
            "applying_preset" if application.in_flight else "preset_retry",
            application.retry_at,
            tuple(commands),
        )

    def _preset_result(self, group_id, application, command, success) -> None:
        runtime = self._current_runtime()
        if (
            runtime is None
            or runtime.preset_applications.get(group_id) is not application
        ):
            return
        application.in_flight.discard(command.equipment_id)
        if command.equipment_id not in application.commands:
            return
        now = dt_util.utcnow()
        if success:
            application.memory = replace(application.memory, changed_at=now)
            del application.commands[command.equipment_id]
            if command.reason == "switch_running_unit":
                corrected = dict(application.memory.corrected)
                corrected[command.equipment_id] = now
                application.memory = replace(
                    application.memory, corrected=tuple(sorted(corrected.items()))
                )
            if command.reason == "preset_pulse" and runtime.mode_active.get(group_id):
                runtime.pending_offs.append(
                    (
                        now + timedelta(seconds=PULSE_SECONDS),
                        replace(
                            command,
                            service="turn_off",
                            hvac_mode=None,
                            delay_seconds=PULSE_SECONDS,
                        ),
                        application.memory.mode,
                    )
                )
        else:
            runtime.command_tracker.cancel(command.entity_id)
            application.retry_at = now + timedelta(seconds=PRESET_RETRY_SECONDS)
        if not application.commands:
            runtime.preset_memories[group_id] = application.memory
            del runtime.preset_applications[group_id]
            runtime.group_switches.pop(group_id, None)
            if not runtime._closed:
                runtime._save_presets()
        if not runtime._closed:
            runtime.refresh()

    def _pulse_result(self, command, target, success) -> None:
        if success:
            return
        runtime = self._current_runtime()
        if runtime is None:
            return
        item = next(
            (
                item
                for item in runtime.building.equipment
                if item.id == command.equipment_id
                and item.entity_id == command.entity_id
            ),
            None,
        )
        if item is None or not runtime.mode_active.get(item.group_id):
            return
        runtime.command_tracker.cancel(command.entity_id)
        now = dt_util.utcnow()
        retry_at = runtime._command_wait(command, now) or now + timedelta(
            seconds=PRESET_RETRY_SECONDS
        )
        runtime.pending_offs.append((retry_at, command, target))
        if not runtime._closed:
            runtime.refresh()

    def _save_presets(self) -> None:
        if self.mode_store is None:
            return
        data = {
            "groups": {
                group_id: {
                    "mode": memory.mode,
                    "changed_at": memory.changed_at.isoformat()
                    if memory.changed_at
                    else None,
                }
                for group_id, memory in self.preset_memories.items()
            }
        }
        self.hass.async_create_task(self.mode_store.async_save(data))

    def _transitions(self, event, now) -> dict[str, str]:
        """Classify a state_changed event of a linked indoor unit, if any."""
        if getattr(event, "event_type", None) != EVENT_STATE_CHANGED:
            return {}  # Setup, timers and same-value reports are no transitions.
        entity_id = event.data.get("entity_id")
        old, new = event.data.get("old_state"), event.data.get("new_state")
        self._screen_transition(entity_id, old, new, event, now)
        self._floor_transition(entity_id, old, new, now)
        transitions = {}
        for item in self.building.equipment:
            if item.kind != "hvac" or item.entity_id != entity_id:
                continue
            old_value = old.state if old else None
            new_value = new.state if new else None
            initiated = (
                old is not None
                and old_value != new_value
                and self.command_tracker.confirm(entity_id, new_value, now)
            )
            trusted = new is not None and not (
                new.attributes.get("restored") or new.attributes.get("assumed_state")
            )
            transition = classify_transition(
                old_value,
                new_value,
                old_present=old is not None,
                trusted=trusted,
                zonentra_initiated=initiated,
                origin=_origin(event),
            )
            if transition == "attribute_update" and trusted:
                transition = self._setpoint_transition(entity_id, old, new, event, now)
            transitions[item.id] = transition
            self.last_transitions[item.id] = (transitions[item.id], now)
        return transitions

    def _setpoint_transition(self, entity_id, old, new, event, now) -> str:
        """0.9: who changed a running unit's setpoint (attribute only).

        The room controllers of this installation only switch units on and
        off. A setpoint change with a bare context therefore comes from the
        unit or gateway itself (for example its own setpoint after switching
        on), not from a person: only a Home Assistant user counts as manual.
        """
        before = _setpoint(old.attributes.get("temperature"))
        after = _setpoint(new.attributes.get("temperature"))
        if before == after or after is None:
            return "attribute_update"
        if self.setpoint_tracker.confirm(entity_id, after, now):
            return "zonentra_setpoint"
        origin = _origin(event)
        if origin == "automation":
            return "automation_setpoint"
        if origin == "device":
            return "device_setpoint"
        return "manual_setpoint"

    def _plan(self, transitions, sources, now) -> None:
        """Update overrides and calculate group coordination and unit intents."""
        settings = self.building.settings
        overrides, events = {}, {}
        for item in self.building.equipment:
            if item.kind != "hvac":
                continue
            source = sources.get(item.entity_id)
            override, event = update_override(
                self.overrides.get(item.id),
                transitions.get(item.id),
                item.id,
                source.state if source else None,
                now,
                enabled=settings.manual_override_enabled,
                duration_minutes=settings.manual_override_duration_minutes,
                regulated=self.regulated(item.zone_id),
            )
            if override:
                overrides[item.id] = override
            if event:
                events[item.id] = event
                _LOGGER.debug("Manual override %s: %s", item.id, event)
        self.overrides = overrides
        self.command_tracker.expire(now)
        changed_at = {
            item.id: sources[item.entity_id].changed_at
            for item in self.building.equipment
            if item.kind == "hvac" and item.entity_id in sources
        }
        self.coordinations, self.unit_intents, self.coordination_memories = plan(
            self.building,
            self.group_decisions,
            self.group_observations,
            self.indoor_units,
            changed_at,
            self.hvac_demands or self.zone_demands,
            overrides,
            events,
            self.coordination_memories,
            now,
        )

    def _climate(self, state) -> ClimateAttributes:
        """Copy only the read-only climate attributes Zonentra explains."""
        attributes = state.attributes
        fan_mode = attributes.get("fan_mode")
        return ClimateAttributes(
            attributes.get("hvac_action"),
            attributes.get("current_temperature"),
            attributes.get("temperature"),
            str(fan_mode) if fan_mode is not None else None,
            self.hass.config.units.temperature_unit,
            state.last_updated,
        )

    @callback
    def close(self):
        """Cancel the single next-deadline timer on unload."""
        self._closed = True
        self._clear_sun_issue()
        if self._cancel_deadline:
            self._cancel_deadline()
            self._cancel_deadline = None
        if self._cancel_forecast_retry:
            self._cancel_forecast_retry()
            self._cancel_forecast_retry = None

    @property
    def status(self) -> str:
        """Observation health; historical read_only does not disable control."""
        if any(d.reason == "observed_group_conflict" for d in self.decisions):
            return "conflict"
        if any(o.quality != "observed" for o in self.observations.values()):
            return "degraded"
        if any(
            zone.heating_limit is not None
            and self.zone_demands[zone.id].demand == "unknown"
            for zone in self.building.zones
        ):
            return "degraded"
        return "read_only"

    def summary(self) -> dict:
        """Return bounded metadata; do not expose alarm states or raw attributes."""
        return {
            "read_only": True,
            "outdoor_temperature_c": self.outdoor_temperature.value,
            "outdoor_temperature_quality": self.outdoor_temperature.quality,
            "building_presence": self.presence,
            "sunny": self.sunny,
            "day_type": self.day_type,
            "forecast_maximum": self.forecast.maximum if self.forecast else None,
            "forecast_minimum": self.forecast.minimum if self.forecast else None,
            "sun_power": self.sun_power,
            "regulated_zones": sum(
                self.regulated(zone.id) for zone in self.building.zones
            ),
            "outdoor_temperature_validation": self.outdoor_temperature.strategy,
            "outdoor_temperature_freshness_verified": (
                self.outdoor_temperature.freshness_verified
            ),
            "outdoor_temperature_selection_required": bool(
                self.building.settings.outdoor_temperature_candidates
            ),
            "zones": len(self.building.zones),
            "groups": len(self.building.groups),
            "equipment": len(self.building.equipment),
            "unavailable_inputs": sum(
                o.quality != "observed" for o in self.observations.values()
            ),
            "decisions": [asdict(d) for d in self.decisions[:20]],
            "decision_count": len(self.decisions),
            "unknown_zone_demands": sum(
                d.demand == "unknown" for d in self.zone_demands.values()
            ),
        }
