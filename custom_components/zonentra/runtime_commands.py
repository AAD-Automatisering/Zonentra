"""Home Assistant command dispatch, final guards and command diagnostics."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.const import UnitOfTemperature
from homeassistant.core import Context
from homeassistant.helpers.temperature import display_temp
from homeassistant.util import dt as dt_util

from .executor import ModeCommand, PresetApplication
from .runtime_helpers import _setpoint
from .temperature import convert_temperature

_LOGGER = logging.getLogger(__name__)
RETRY_SECONDS = 120
MAX_RETRY_SECONDS = 1800


@dataclass
class CommandReceipt:
    command: ModeCommand
    entry: dict
    expected: object
    attribute: str | None
    deadline: datetime
    sent: bool = False


def _key(command):
    return command.domain, command.entity_id, command.service


def _signature(command):
    return (
        command.hvac_mode,
        command.temperature,
        command.fan_mode,
        command.value,
        command.reason,
    )


class CommandRuntimeMixin:
    """Dispatch controls through the owning building runtime."""

    def _execute(self, command, now, *, on_result=None) -> bool:
        if self._command_wait(command, now):
            return False
        data = {"entity_id": command.entity_id}
        expected, attribute = None, None
        if command.domain != "climate":
            if command.service == "set_cover_position":
                data["position"] = command.value
            elif command.service == "select_option":
                data["option"] = command.value
                expected = str(command.value)
            elif command.service == "set_value":
                data["value"] = command.value
                expected = command.value
            elif command.service in ("turn_on", "turn_off"):
                expected = command.service.removeprefix("turn_")
        elif command.service == "set_fan_mode":
            data["fan_mode"] = command.fan_mode
            expected, attribute = command.fan_mode, "fan_mode"
        elif command.service == "set_temperature":
            # HA interprets service temperatures in the configured system unit.
            temperature = convert_temperature(
                command.temperature,
                UnitOfTemperature.CELSIUS,
                self.hass.config.units.temperature_unit,
            )
            if temperature is None:
                self._log(command, now, "blocked: invalid_temperature")
                if on_result is not None:
                    self.hass.loop.call_soon(on_result, False)
                return
            entity = self._climate_entity(command.entity_id)
            if entity is not None:
                # Quantize and clamp in native units before the service's own
                # conversion. Rounding a converted boundary can fall just outside
                # the device's strict min/max checks.
                native = convert_temperature(
                    command.temperature,
                    UnitOfTemperature.CELSIUS,
                    entity.temperature_unit,
                )
                if native is not None:
                    step = convert_temperature(
                        entity.target_temperature_step,
                        entity.temperature_unit,
                        entity.temperature_unit,
                        interval=True,
                    )
                    if step is not None and step > 0:
                        native = round(native / step) * step
                    native = min(entity.max_temp, max(entity.min_temp, native))
                    temperature = convert_temperature(
                        native,
                        entity.temperature_unit,
                        self.hass.config.units.temperature_unit,
                    )
            data["temperature"] = (
                temperature if entity is not None else round(temperature, 6)
            )
            shown = (
                display_temp(
                    self.hass,
                    data["temperature"],
                    self.hass.config.units.temperature_unit,
                    entity.precision,
                )
                if entity is not None
                else data["temperature"]
            )
            expected, attribute = _setpoint(shown), "temperature"
            self.setpoint_tracker.register(command.entity_id, expected, now)
        else:
            # A new mode request supersedes an older off/on receipt.
            for key, receipt in tuple(self.command_receipts.items()):
                if (
                    receipt.command.entity_id == command.entity_id
                    and receipt.command.service in ("set_hvac_mode", "turn_off")
                ):
                    receipt.entry["confirmation"] = "superseded"
                    del self.command_receipts[key]
            expected = (
                command.hvac_mode if command.service == "set_hvac_mode" else "off"
            )
            self.command_tracker.register(command.entity_id, expected, now)
        if command.hvac_mode:
            data["hvac_mode"] = command.hvac_mode
        entry = self._log(command, now, "sending")
        receipt = CommandReceipt(
            command, entry, expected, attribute, now + timedelta(seconds=60)
        )
        self.command_receipts[_key(command)] = receipt

        async def send():
            success = await self._call(command.service, data, entry, command.domain)
            runtime = self._current_runtime()
            if runtime is not None:
                runtime._command_result(receipt, success)
            if on_result is not None:
                self.hass.loop.call_soon(on_result, success)
            if runtime is not None and not runtime._closed:
                self.hass.loop.call_soon(runtime.refresh)

        self.hass.async_create_task(send())
        return True

    async def _call(self, service, data, entry, domain="climate") -> bool:
        runtime = self._current_runtime()
        group_ids = {entry.get("group_id")}
        item = next(
            (
                item
                for item in runtime.building.equipment
                if item.id == entry.get("equipment_id")
            ),
            None,
        )
        if item is None:
            entry["result"] = "blocked: equipment_removed"
            return False
        if domain == "cover" and (
            guard := runtime._screen_guard(item, position=data.get("position"))
        ):
            entry["result"] = f"blocked: {guard}"
            return False
        group_ids.add(item.group_id)
        if entry.get("reason", "").startswith("vane_"):
            group_ids.update(
                unit.group_id
                for unit in runtime.building.equipment
                if unit.kind == "hvac" and unit.zone_id == item.zone_id
            )
        blocked = next(
            (
                reason
                for group_id in group_ids
                if (reason := runtime._group_block(group_id))
            ),
            None,
        )
        if blocked or runtime._closed:
            entry["result"] = f"blocked: {blocked or 'runtime_unloaded'}"
            return False
        reason = entry.get("reason", "")
        group_command = reason in (
            "preset_pulse",
            "switch_running_unit",
            "correct_running_unit",
            "group_changeover_stop",
        )
        floor = item.kind == "floor_heating"
        if domain == "climate" and floor:
            if guard := runtime._floor_guard(item, service, data):
                entry["result"] = f"blocked: {guard}"
                return False
        if (
            domain == "climate"
            and not floor
            and service == "set_hvac_mode"
            and data.get("hvac_mode") in ("cool", "dry")
        ):
            if runtime._floor_cooling_block(item.zone_id):
                entry["result"] = "blocked: floor_stop_unconfirmed"
                return False
        if domain == "climate" and not group_command and not floor:
            if not runtime.regulated(item.zone_id):
                entry["result"] = "blocked: room_disabled"
                return False
            thermostat = runtime.thermostat(item.zone_id)
            if service == "set_fan_mode" and not thermostat.fan_control:
                entry["result"] = "blocked: fan_disabled"
                return False
            override = runtime.overrides.get(item.id)
            if (
                override
                and override.active(dt_util.utcnow())
                and reason != "window_open"
            ):
                entry["result"] = "blocked: manual_override"
                return False
            if thermostat.hvac_mode == "off" and service != "turn_off":
                entry["result"] = "blocked: thermostat_off"
                return False
        if domain == "climate" and service == "set_hvac_mode" and not floor:
            block, until = runtime._start_block(
                data["entity_id"], data.get("hvac_mode"), dt_util.utcnow()
            )
            if block:
                entry["result"] = f"blocked: {block}"
                if until:
                    entry["retry_at"] = until.isoformat()
                return False
        try:
            async with asyncio.timeout(30):
                await self.hass.services.async_call(
                    domain, service, data, blocking=True, context=Context()
                )
        except Exception as err:  # noqa: BLE001 - report, never crash the runtime
            entry["result"] = f"failed: {type(err).__name__}"
            _LOGGER.warning("Zonentra %s on %s failed: %s", service, data, err)
            return False
        else:
            entry["result"] = "sent"
            return True

    def _command_wait(self, command, now):
        key = _key(command)
        receipt = self.command_receipts.get(key)
        if receipt:
            if not receipt.sent or _signature(receipt.command) == _signature(command):
                return receipt.deadline
            receipt.entry["confirmation"] = "superseded"
            self.command_receipts.pop(key)
        retry = self.command_retries.get(key)
        if retry:
            previous, until, count = retry
            if _signature(previous) != _signature(command):
                self.command_retries.pop(key)
            elif now < until:
                return until
        return None

    def _command_result(self, receipt, success):
        command = receipt.command
        if self.command_receipts.get(_key(command)) is not receipt:
            return
        receipt.sent = success
        if success:
            receipt.entry["confirmation"] = "pending"
            if command.service == "set_fan_mode":
                self.fan_applied[command.equipment_id] = command.fan_mode
                self.fan_sent_at[command.equipment_id] = dt_util.utcnow()
            elif command.reason.startswith("vane_"):
                item = next(
                    (
                        i
                        for i in self.building.equipment
                        if i.id == command.equipment_id
                    ),
                    None,
                )
                if item:
                    self.vane_applied[item.zone_id] = command.reason.removeprefix(
                        "vane_"
                    )
            elif command.reason == "thermostat_mode":
                self.mode_applied[command.equipment_id] = command.hvac_mode
            elif command.service == "set_temperature":
                self.setpoint_sent[command.equipment_id] = dt_util.utcnow()
        else:
            receipt.entry["confirmation"] = "not_sent"
            self._fail_command(receipt, dt_util.utcnow())

    def _fail_command(self, receipt, now):
        command, key = receipt.command, _key(receipt.command)
        self.command_receipts.pop(key, None)
        # Vane switches also use turn_off; clear their own applied state first.
        if command.reason.startswith("vane_"):
            item = next(
                (i for i in self.building.equipment if i.id == command.equipment_id),
                None,
            )
            if item:
                self.vane_applied.pop(item.zone_id, None)
        elif command.service in ("set_hvac_mode", "turn_off"):
            self.command_tracker.cancel(command.entity_id)
            self.mode_applied.pop(command.equipment_id, None)
        elif command.service == "set_temperature":
            self.setpoint_tracker.cancel(command.entity_id)
        elif command.service == "set_fan_mode":
            self.fan_applied.pop(command.equipment_id, None)
        previous = self.command_retries.get(key)
        count = (
            previous[2] + 1
            if previous and _signature(previous[0]) == _signature(command)
            else 1
        )
        until = now + timedelta(
            seconds=min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(count - 1, 4))
        )
        if raw := receipt.entry.get("retry_at"):
            until = max(until, dt_util.parse_datetime(raw))
        self.command_retries[key] = (command, until, count)
        # Reapply a service-accepted preset that never produced the expected
        # state. Other retries are re-planned by their own current policy.
        if (
            receipt.sent
            and command.reason in ("preset_pulse", "switch_running_unit")
            and command.service == "set_hvac_mode"
        ):
            item = next(
                (i for i in self.building.equipment if i.id == command.equipment_id),
                None,
            )
            if (
                item
                and self.mode_active.get(item.group_id)
                and item.group_id not in self.preset_applications
            ):
                memory = self.preset_memories.get(item.group_id)
                if memory and memory.mode == command.hvac_mode:
                    self.preset_applications[item.group_id] = PresetApplication(
                        memory, {item.id: command}, set(), until
                    )

        if (
            receipt.sent
            and command.reason == "preset_pulse"
            and command.service == "turn_off"
        ):
            item = next(
                (i for i in self.building.equipment if i.id == command.equipment_id),
                None,
            )
            memory = self.preset_memories.get(item.group_id) if item else None
            if memory:
                self.hass.loop.call_soon(
                    self._pulse_result, command, memory.mode, False
                )

    def _command_feedback(self, now):
        for key, receipt in tuple(self.command_receipts.items()):
            if not receipt.sent:
                continue
            state = self.hass.states.get(receipt.command.entity_id)
            trustworthy = (
                state is not None
                and state.state not in ("unknown", "unavailable")
                and not (
                    state.attributes.get("restored")
                    or state.attributes.get("assumed_state")
                )
            )
            value = (
                state.attributes.get(receipt.attribute)
                if trustworthy and receipt.attribute
                else state.state
                if trustworthy
                else None
            )
            if receipt.attribute == "temperature":
                value = _setpoint(value)
            matches = value == receipt.expected
            if isinstance(receipt.expected, (int, float)) and value is not None:
                try:
                    matches = float(value) == receipt.expected
                except TypeError, ValueError:
                    matches = False
            if trustworthy and matches:
                receipt.entry["confirmation"] = "confirmed"
                self.command_receipts.pop(key, None)
                self.command_retries.pop(key, None)
                if receipt.command.service in ("set_hvac_mode", "turn_off"):
                    self.command_tracker.cancel(receipt.command.entity_id)
                elif receipt.command.service == "set_temperature":
                    self.setpoint_tracker.cancel(receipt.command.entity_id)
            elif now > receipt.deadline:
                receipt.entry["confirmation"] = "timeout"
                self._fail_command(receipt, now)

    def _command_deadlines(self, now):
        return (
            [
                receipt.deadline + timedelta(microseconds=1)
                for receipt in self.command_receipts.values()
                if receipt.deadline >= now
            ]
            + [until for _, until, _ in self.command_retries.values() if until > now]
            + [
                pending.deadline + timedelta(microseconds=1)
                for pending in self.cover_tracker.pending
                if pending.deadline >= now
            ]
        )

    def _log(self, command, now, result) -> dict:
        entry = {
            "at": now.isoformat(),
            "group_id": next(
                (
                    item.group_id
                    for item in self.building.equipment
                    if item.id == command.equipment_id
                ),
                None,
            ),
            "equipment_id": command.equipment_id,
            "entity_id": command.entity_id,
            "domain": command.domain,
            "service": command.service,
            "hvac_mode": command.hvac_mode,
            "temperature": command.temperature,
            "fan_mode": command.fan_mode,
            "value": command.value,
            "reason": command.reason,
            "result": result,
        }
        self.command_log = [*self.command_log, entry][-30:]
        _LOGGER.info("Zonentra mode control: %s", entry)
        return entry
