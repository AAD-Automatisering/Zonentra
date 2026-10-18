"""Independent floor thermostat control with physical feedback and airco priority."""

from dataclasses import replace
from datetime import timedelta

from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.temperature import display_temp
from homeassistant.util import dt as dt_util

from .executor import ModeCommand
from .floor_heating import heating_progress
from .indoor import HEATING_ACTIONS, observe_unit
from .runtime_helpers import _setpoint
from .temperature import convert_temperature


def trustworthy(state):
    return (
        state is not None
        and state.state not in ("unknown", "unavailable")
        and not (
            state.attributes.get("restored") or state.attributes.get("assumed_state")
        )
    )


def floor_off(state):
    """Contradictory heating feedback cannot release an airco cooling start."""
    return (
        trustworthy(state)
        and state.state == "off"
        and state.attributes.get("hvac_action") not in HEATING_ACTIONS
    )


class FloorRuntimeMixin:
    """Floors never participate in outdoor group mode control."""

    def floor_regulated(self, zone_id):
        return self.active_capable and self.thermostat(zone_id).floor_regulation

    def _floor_stop(self, zone_id):
        mode = self.thermostat(zone_id).hvac_mode
        demand = self.zone_demands.get(zone_id)
        return (
            mode not in ("heat", "heat_cool")
            or self.windows.get(zone_id) == "open"
            or (demand is not None and demand.demand == "cooling_demand")
            or any(
                item.kind == "hvac"
                and item.zone_id == zone_id
                and trustworthy(state := self.hass.states.get(item.entity_id))
                and state.state in ("cool", "dry")
                for item in self.building.equipment
            )
        )

    def _floor_cooling_block(self, zone_id):
        # Observing a linked floor does not require permission to control it.
        # Switching regulation off must never release unconfirmed cooling.
        return any(
            item.kind == "floor_heating"
            and item.zone_id == zone_id
            and item.entity_id.startswith("climate.")
            and not floor_off(self.hass.states.get(item.entity_id))
            for item in self.building.equipment
        )

    def _floor_target(self, item):
        """Validate and quantize a room target before dispatch, never clamp it."""
        state = self.hass.states.get(item.entity_id)
        if not item.entity_id.startswith("climate."):
            return None, "unsupported_entity"
        if not trustworthy(state):
            return None, "unavailable"
        attrs = state.attributes
        # KNX heating-only thermostats can accept targets without an off mode.
        # Stopping and cooling interlocks validate off support separately.
        if "heat" not in attrs.get("hvac_modes", ()):
            return None, "unsupported_modes"
        if _setpoint(attrs.get("temperature")) is None:
            return None, "unsupported_setpoint"
        zone = next(zone for zone in self.building.zones if zone.id == item.zone_id)
        # A separate gas-saving room target, independent of comfort and agenda.
        target = zone.floor_heating_limit
        unit = self.hass.config.units.temperature_unit
        native_unit = unit
        entity = self._climate_entity(item.entity_id)
        if entity is not None:
            native_unit = entity.temperature_unit
            low, high, step = (
                entity.min_temp,
                entity.max_temp,
                entity.target_temperature_step,
            )
        else:
            low, high = attrs.get("min_temp"), attrs.get("max_temp")
            step = attrs.get("target_temp_step", 0.5)
        raw = convert_temperature(target, UnitOfTemperature.CELSIUS, native_unit)
        low, high, step = (
            convert_temperature(value, native_unit, native_unit)
            for value in (low, high, step)
        )
        if raw is None or low is None or high is None or not low <= raw <= high:
            return None, "target_out_of_range"
        rounded = round(raw / step) * step if step is not None and step > 0 else raw
        if not low <= rounded <= high:
            return None, "target_out_of_range"
        return convert_temperature(
            rounded, native_unit, UnitOfTemperature.CELSIUS
        ), None

    def _floor_target_matches(self, item, target):
        """Compare with HA's displayed precision, as command receipts do."""
        state = self.hass.states.get(item.entity_id)
        expected = convert_temperature(
            target, UnitOfTemperature.CELSIUS, self.hass.config.units.temperature_unit
        )
        entity = self._climate_entity(item.entity_id)
        if entity is not None:
            expected = display_temp(
                self.hass,
                expected,
                self.hass.config.units.temperature_unit,
                entity.precision,
            )
        return trustworthy(state) and _setpoint(
            state.attributes.get("temperature")
        ) == _setpoint(expected)

    def _floor_prepare(self, sources, now):
        self.floor_plans = {}
        self.floor_observations = {}
        self._floor_checks = []
        items = [
            item for item in self.building.equipment if item.kind == "floor_heating"
        ]
        valid = {item.id for item in items}
        self.floor_overrides = {
            key: until
            for key, until in self.floor_overrides.items()
            if key in valid
            and until > now
            and self.building.settings.manual_override_enabled
        }
        self._floor_checks.extend(self.floor_overrides.values())
        for item in items:
            if item.entity_id.startswith("climate."):
                self.floor_observations[item.id] = observe_unit(
                    item, sources.get(item.entity_id)
                )
            target, error = self._floor_target(item)
            state = self.hass.states.get(item.entity_id)
            plan = {"status": "disabled", "target_temperature": target, "command": None}
            self.floor_plans[item.id] = plan
            if not self.floor_regulated(item.zone_id):
                continue
            # Stopping needs only a trustworthy climate and off support, even
            # when its target/sensor is invalid. No setpoint is sent on a stop.
            stop = self._floor_stop(item.zone_id)
            if (
                stop
                and item.entity_id.startswith("climate.")
                and trustworthy(state)
                and "off" in state.attributes.get("hvac_modes", ())
            ):
                plan["status"] = "off" if floor_off(state) else "stopping"
                if state.state != "off":
                    plan["command"] = self._floor_command(item, "turn_off")
                continue
            if error:
                plan["status"] = error
                continue
            if stop:
                plan["status"] = "cannot_stop"
                continue
            if item.id in self.floor_overrides:
                plan["status"] = "manual_override"
                continue
            if self.zone_snapshots[item.zone_id].temperature.quality != "valid":
                plan["status"] = "room_temperature_unknown"
                continue
            if any(key[1] == item.entity_id for key in self.command_retries):
                plan["status"] = "command_failed"
            else:
                plan["status"] = "in_sync"
            if not self._floor_target_matches(item, target):
                plan["command"] = self._floor_command(item, "set_temperature", target)
                if plan["status"] != "command_failed":
                    plan["status"] = "setting_target"
            elif state.state != "heat":
                plan["command"] = self._floor_command(item, "set_hvac_mode")
                if plan["status"] != "command_failed":
                    plan["status"] = "starting"
            else:
                # A retry is no longer needed when the current goal is met.
                for key in tuple(self.command_retries):
                    if key[1] == item.entity_id:
                        self.command_retries.pop(key)
                plan["status"] = "in_sync"
        # Heating demand is passed straight to the airco. Only cooling waits
        # for trustworthy floor-off feedback; floor failures never delay heat.
        self.hvac_demands = dict(self.zone_demands)
        self.heating_progress = {}
        watches = {}
        for zone in self.building.zones:
            floors = [item for item in items if item.zone_id == zone.id]
            demand = self.zone_demands[zone.id]
            cooling = (
                demand.demand == "cooling_demand"
                or self.thermostat(zone.id).hvac_mode == "dry"
            )
            blocked = cooling and self._floor_cooling_block(zone.id)
            if blocked:
                self.hvac_demands[zone.id] = replace(
                    demand,
                    demand="neutral",
                    heating_score=0.0,
                    cooling_score=0.0,
                    reason="floor_stop_unconfirmed",
                )
            for item in floors:
                self.floor_plans[item.id]["cooling_blocked"] = blocked
            if not floors:
                continue
            units = [
                observe_unit(item, sources.get(item.entity_id))
                for item in self.building.equipment
                if item.kind == "hvac" and item.zone_id == zone.id
            ]
            temperature = self.zone_snapshots[zone.id].temperature
            target = self.zone_snapshots[zone.id].heating_limit
            enabled = self.floor_regulated(zone.id) and self.regulated(zone.id)
            warming = any(
                unit.available and unit.activity == "heating" for unit in units
            )
            watching = enabled and demand.demand == "heating_demand" and warming
            progress = heating_progress(
                self.heating_watches.get(zone.id),
                now,
                temperature=temperature.value
                if temperature.quality == "valid"
                else None,
                target=target.value if target.quality == "valid" else None,
                reported_at=temperature.reported_at,
                watching=watching,
            )
            self.heating_progress[zone.id] = progress
            if progress.memory:
                watches[zone.id] = progress.memory
            if progress.next_check:
                self._floor_checks.append(progress.next_check)
        self.heating_watches = watches

    @staticmethod
    def _floor_command(item, service, temperature=None):
        return ModeCommand(
            item.id,
            item.entity_id,
            service,
            "heat" if service == "set_hvac_mode" else None,
            0,
            "floor_heating",
            temperature=temperature,
        )

    def _floor_control(self, now):
        for plan in self.floor_plans.values():
            if command := plan["command"]:
                self._execute(command, now)

    def _floor_guard(self, item, service, data):
        if item.entity_id != data.get("entity_id") or not item.entity_id.startswith(
            "climate."
        ):
            return "unsupported_floor_entity"
        if not self.floor_regulated(item.zone_id):
            return "floor_disabled"
        if service not in ("set_temperature", "set_hvac_mode", "turn_off"):
            return "unsupported_floor_command"
        if service == "turn_off":
            state = self.hass.states.get(item.entity_id)
            if not trustworthy(state) or "off" not in state.attributes.get(
                "hvac_modes", ()
            ):
                return "floor_stop_unsupported"
            return None if self._floor_stop(item.zone_id) else "floor_stop_cancelled"
        if self._floor_stop(item.zone_id):
            return "floor_heat_blocked"
        if self.floor_overrides.get(item.id, dt_util.utcnow()) > dt_util.utcnow():
            return "manual_override"
        target, error = self._floor_target(item)
        if error:
            return error
        if self.zone_snapshots[item.zone_id].temperature.quality != "valid":
            return "room_temperature_unknown"
        if service == "set_hvac_mode":
            if data.get("hvac_mode") != "heat":
                return "unsupported_floor_mode"
            if not self._floor_target_matches(item, target):
                return "floor_target_unconfirmed"
        else:
            requested = convert_temperature(
                data.get("temperature"),
                self.hass.config.units.temperature_unit,
                UnitOfTemperature.CELSIUS,
            )
            if requested is None or abs(requested - target) > 0.01:
                return "floor_target_changed"
        return None

    def _floor_transition(self, entity_id, old, new, now):
        if not trustworthy(old) or not trustworthy(new):
            return
        for item in self.building.equipment:
            if item.kind != "floor_heating" or item.entity_id != entity_id:
                continue
            changed = False
            if old.state != new.state:
                changed = not self.command_tracker.confirm(entity_id, new.state, now)
            before, after = (
                _setpoint(state.attributes.get("temperature")) for state in (old, new)
            )
            if before != after and after is not None:
                changed |= not self.setpoint_tracker.confirm(entity_id, after, now)
            if (
                changed
                and self.floor_regulated(item.zone_id)
                and self.building.settings.manual_override_enabled
            ):
                self.floor_overrides[item.id] = now + timedelta(
                    minutes=self.building.settings.manual_override_duration_minutes
                )
                # A queued command must not overwrite the new manual target.
                for tracker in (self.command_tracker, self.setpoint_tracker):
                    tracker.cancel(entity_id)
