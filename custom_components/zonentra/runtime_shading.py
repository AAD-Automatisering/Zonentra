"""Screen observation, recovery and enabled movement for a building runtime."""

import math
from datetime import timedelta

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .executor import ModeCommand
from .observations import cover_position
from .rain import RainMemory, RainProtection, current_rain, rain_protection
from .runtime_helpers import _origin
from .shading import (
    MIN_STEP,
    ScreenDecision,
    ScreenMove,
    decide_screen,
    plan_screen_move,
    stable_screen_target,
    sun_on_facade,
    sunny_state,
)
from .wind import WindMemory, WindProtection, speed_kmh, wind_protection

SCREEN_RETRY_SECONDS = 120


def _power(source) -> float | None:
    """Solar power in W, or None when not usable."""
    if source is None or not source.trustworthy:
        return None
    try:
        value = float(source.state)
    except TypeError, ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value * 1000 if source.unit == "kW" else value


class ShadingRuntimeMixin:
    """Plan screens independently, sharing lifecycle and dispatch with the runtime."""

    # --- Screens (0.11) ---------------------------------------------------------

    def load_screen_state(self, data):
        """Restore finite expiry times only for the same physical screen."""
        if not isinstance(data, dict):
            return
        now = dt_util.utcnow()
        rain = data.get("__rain__", {})
        settings = self.building.settings
        source = settings.screen_rain_entity or settings.weather_entity
        if (
            isinstance(rain, dict)
            and rain.get("wet") is True
            and rain.get("entity_id") == source
        ):
            # Time offline is not evidence of continuous dry weather.
            self.rain_source = source
            self.rain_memory = RainMemory(True)
        wind = data.get("__wind__", {})
        if (
            isinstance(wind, dict)
            and wind.get("active") is True
            and wind.get("entity_id") == settings.weather_entity
        ):
            self.wind_policy = (
                settings.weather_entity,
                settings.screen_wind_speed_limit,
                settings.screen_wind_gust_limit,
            )
            self.wind_memory = WindMemory(True)
        for item in self.building.equipment:
            record = data.get(item.id)
            if item.kind != "shading" or not isinstance(record, dict):
                continue
            if record.get("entity_id") != item.entity_id:
                continue
            for key, destination in (
                ("last_move", self.screen_last_move),
                ("override_until", self.screen_overrides),
            ):
                raw = record.get(key)
                at = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
                if at is None or at.tzinfo is None:
                    continue
                if key == "last_move" and now - timedelta(hours=4) <= at <= now:
                    destination[item.id] = at
                elif key == "override_until" and now < at <= now + timedelta(hours=12):
                    destination[item.id] = at

    def _save_screen_state(self):
        if self.screen_store is None:
            return
        self.screen_store.async_delay_save(
            lambda: {
                **(
                    {"__rain__": {"entity_id": self.rain_source, "wet": True}}
                    if self.rain_memory.wet
                    else {}
                ),
                **(
                    {
                        "__wind__": {
                            "entity_id": self.building.settings.weather_entity,
                            "active": True,
                        }
                    }
                    if self.wind_memory.active
                    else {}
                ),
                **{
                    item.id: {
                        "entity_id": item.entity_id,
                        "last_move": self.screen_last_move[item.id].isoformat()
                        if item.id in self.screen_last_move
                        else None,
                        "override_until": self.screen_overrides[item.id].isoformat()
                        if item.id in self.screen_overrides
                        else None,
                    }
                    for item in self.building.equipment
                    if item.kind == "shading"
                },
            },
            1,
        )

    def _screen_transition(self, entity_id, old, new, event, now) -> None:
        """A screen moved by a person (HA or room controller) is respected."""
        for item in self.building.equipment:
            if item.kind != "shading" or item.entity_id != entity_id:
                continue
            if new is None or new.state in (
                "unknown",
                "unavailable",
            ):
                return
            if new.attributes.get("restored") or new.attributes.get("assumed_state"):
                return
            if (
                old is None
                or old.state in ("unknown", "unavailable")
                or (
                    old.attributes.get("restored")
                    or old.attributes.get("assumed_state")
                )
            ):
                # Recovery is a fresh observation, not evidence of a person.
                self.cover_tracker.cancel(entity_id)
                return
            position = cover_position(new)
            pending = next(
                (p for p in self.cover_tracker.pending if p.entity_id == entity_id),
                None,
            )
            if pending is not None and self.cover_tracker.is_pending(entity_id, now):
                expected = float(pending.expected_state)
                if new.state in ("opening", "closing"):
                    # Our own travel is not manual operation. A reversal is:
                    # respect the wall button before it reaches an end stop.
                    if position is None or (
                        new.state == "opening"
                        and expected >= position
                        or new.state == "closing"
                        and expected <= position
                    ):
                        return
                elif position is not None and abs(position - expected) < MIN_STEP:
                    self.cover_tracker.confirm(entity_id, pending.expected_state, now)
                    self._cover_confirmation(
                        entity_id, pending.expected_state, "confirmed"
                    )
                    self.last_transitions[item.id] = ("zonentra_screen", now)
                    return
                if position is None or (
                    old is not None
                    and cover_position(old) == position
                    and old.state == new.state
                ):
                    return
                # 0.13.4: it came to rest away from our position (stopped or
                # sent elsewhere): a person took over, room controller or HA.
                self._cover_confirmation(
                    entity_id, pending.expected_state, "superseded"
                )
                self.cover_tracker.cancel(entity_id)
            elif old.state == new.state and (
                new.state in ("opening", "closing") or cover_position(old) == position
            ):
                return
            if _origin(event) == "automation":
                self.last_transitions[item.id] = ("automation_screen", now)
                return
            minutes = self.building.settings.screen_override_minutes
            self.screen_overrides[item.id] = now + timedelta(minutes=minutes)
            self._save_screen_state()
            self.last_transitions[item.id] = ("manual_screen", now)

    def _shading(self, sources, now) -> None:
        """Decide every screen; move it only with the room's screen regulation."""
        settings = self.building.settings
        for pending in self.cover_tracker.pending:
            if now > pending.deadline:
                self._cover_confirmation(
                    pending.entity_id, pending.expected_state, "timeout"
                )
        checks = []
        self._update_wind(now)
        if self.wind.next_check:
            checks.append(self.wind.next_check)
        if expiry := self._wind_expiry(now):
            checks.append(expiry)
        self._update_rain(now)
        if self.rain.next_check:
            checks.append(self.rain.next_check)
        if expiry := self._rain_expiry(now):
            checks.append(expiry)
        self.sun_power = _power(sources.get(settings.sun_power_entity))
        if settings.sun_power_entity:
            self.sunny, self.sun_memory, check = sunny_state(
                self.sun_power, settings.sun_power_threshold, self.sun_memory, now
            )
            if check:
                checks.append(check)
        else:
            self.sunny = None
        azimuth, elevation = self._sun_position()
        block = self._screen_block()
        blocked = block is not None
        available = self.hass.services.has_service("cover", "set_cover_position")
        zones = {zone.id: zone for zone in self.building.zones}
        screens = {
            item.id for item in self.building.equipment if item.kind == "shading"
        }
        self.screen_targets = {
            key: memory for key, memory in self.screen_targets.items() if key in screens
        }
        decisions, moves = {}, {}
        for item in self.building.equipment:
            if item.kind != "shading":
                continue
            zone = zones[item.zone_id]
            snapshot = self.zone_snapshots[zone.id]
            previous = self.screen_decisions.get(item.id)
            decision = decide_screen(
                elevation=elevation,
                sunny=self.sunny,
                on_facade=sun_on_facade(azimuth, elevation, zone.facade),
                demand=self.zone_demands[zone.id].demand,
                room_temperature=snapshot.temperature.value
                if snapshot.temperature.quality == "valid"
                else None,
                heating_limit=snapshot.heating_limit.value,
                cooling_limit=snapshot.cooling_limit.value,
                shade_position=round(settings.screen_shade_position),
                previous=previous.target if previous else None,
                day=self.day_type,
                occupied=self.zone_bands[zone.id][0] == "comfort"
                or self.thermostat(zone.id).manual,
            )
            protective = self.wind.open_required or self.rain.open_required
            if self.wind.open_required:
                decision = ScreenDecision(
                    "open",
                    100,
                    "wind"
                    if self.wind.status == "wind"
                    else "wind_calming"
                    if self.wind.status == "calming"
                    else "wind_unknown",
                )
            elif self.rain.open_required:
                decision = ScreenDecision(
                    "open",
                    100,
                    "rain"
                    if self.rain.status == "rain"
                    else "rain_drying"
                    if self.rain.status == "drying"
                    else "rain_unknown",
                )
            elif (
                (self.rain.block_closing or self.wind.block_closing)
                and decision.position is not None
                and decision.position < 100
            ):
                decision = ScreenDecision(
                    "hold",
                    None,
                    "wind_unknown" if self.wind.block_closing else "rain_unknown",
                )
            override = self.screen_overrides.get(item.id)
            if override is not None and now >= override:
                del self.screen_overrides[item.id]  # Manual time is over.
                self._save_screen_state()
                override = None
            state = self.hass.states.get(item.entity_id)
            pending = next(
                (
                    pending
                    for pending in self.cover_tracker.pending
                    if pending.entity_id == item.entity_id
                ),
                None,
            )
            if protective and pending and pending.expected_state != "100":
                self._cover_confirmation(
                    item.entity_id, pending.expected_state, "superseded"
                )
                self.cover_tracker.cancel(item.entity_id)
            current = cover_position(state) if state else None
            if (
                protective
                and state is not None
                and (state.state == "closing" or current is not None and current < 100)
            ):
                # The ordinary 10% position tolerance must not leave wet
                # fabric partially extended or allow a closing move to finish.
                current = None
            move = plan_screen_move(
                decision,
                current,
                None if protective else self.screen_last_move.get(item.id),
                now,
                enabled=self.active_capable
                and self.thermostat(zone.id).screen_regulation,
                blocked=blocked,
                override_until=None if protective else override,
                busy=self.cover_tracker.is_pending(item.entity_id, now),
                interval_seconds=settings.screen_min_move_minutes * 60,
            )
            retry_at = (
                self.screen_retry_at.get(item.id)
                if not protective or self.screen_retry_targets.get(item.id) == 100
                else None
            )
            if move.status == "move" and retry_at and now < retry_at:
                move = ScreenMove("retry", next_check=retry_at)
            if move.status == "move" and (guard := self._screen_guard(item)):
                move = ScreenMove(guard)
            if move.status == "blocked" and block:
                move = ScreenMove(block)
            if move.status == "move" and not available:
                move = ScreenMove("cover_service_unavailable")
            if (
                move.status in ("move", "interval", "retry")
                and self._screen_guard(item) is None
                and available
                and not protective
            ):
                delay = (
                    settings.screen_open_delay_minutes
                    if decision.target == "open"
                    else settings.screen_shade_delay_minutes
                )
                memory, stable_at = stable_screen_target(
                    decision, self.screen_targets.get(item.id), now, delay * 60
                )
                self.screen_targets[item.id] = memory
                if stable_at:
                    checks.append(stable_at)
                    if move.status == "move":
                        move = ScreenMove("stabilizing", next_check=stable_at)
            else:
                self.screen_targets.pop(item.id, None)
            if move.status == "move":
                self._move_screen(item, move.position, decision.reason, now)
            if move.next_check:
                checks.append(move.next_check)
            decisions[item.id], moves[item.id] = decision, move
        self.screen_decisions, self.screen_moves = decisions, moves
        self._screen_checks = checks

    def _move_screen(self, item, position, reason, now) -> None:
        entry = self._log(
            ModeCommand(
                item.id,
                item.entity_id,
                "set_cover_position",
                None,
                0,
                f"screen_{reason}",
                domain="cover",
                value=position,
            ),
            now,
            "sending",
        )

        async def send():
            # Register before the service can publish its confirmation state.
            self.cover_tracker.register(item.entity_id, str(position), now)
            registered = next(
                pending
                for pending in self.cover_tracker.pending
                if pending.entity_id == item.entity_id
            )
            success = await self._call(
                "set_cover_position",
                {"entity_id": item.entity_id, "position": position},
                entry,
                "cover",
            )
            runtime = self._current_runtime()
            if runtime is None or runtime._closed:
                return
            if success:
                entry.setdefault("confirmation", "pending")
                runtime.screen_last_move[item.id] = now
                runtime._save_screen_state()
                runtime.screen_retry_at.pop(item.id, None)
                runtime.screen_retry_targets.pop(item.id, None)
            else:
                entry["confirmation"] = "not_sent"
                if registered in runtime.cover_tracker.pending:
                    runtime.cover_tracker.cancel(item.entity_id)
                runtime.screen_retry_targets[item.id] = position
                runtime.screen_retry_at[item.id] = dt_util.utcnow() + timedelta(
                    seconds=SCREEN_RETRY_SECONDS
                )
            self.hass.loop.call_soon(runtime.refresh)

        self.hass.async_create_task(send())

    def _cover_confirmation(self, entity_id, expected, status):
        for entry in reversed(self.command_log):
            if (
                entry["entity_id"] == entity_id
                and entry["service"] == "set_cover_position"
                and str(entry["value"]) == expected
            ):
                entry["confirmation"] = status
                return

    def _wind_configured(self):
        settings = self.building.settings
        return settings.screen_wind_protection and (
            settings.weather_entity is not None
            or settings.screen_wind_speed_limit is not None
            or settings.screen_wind_gust_limit is not None
        )

    @property
    def wind_mode(self):
        settings = self.building.settings
        return (
            "limits_and_weather"
            if settings.screen_wind_speed_limit is not None
            or settings.screen_wind_gust_limit is not None
            else "weather_condition"
        )

    def _wind_state(self, now):
        settings = self.building.settings
        state = (
            self.hass.states.get(settings.weather_entity)
            if settings.weather_entity
            else None
        )
        if (
            state is None
            or state.state in ("unknown", "unavailable")
            or state.attributes.get("restored")
            or state.attributes.get("assumed_state")
            or not -5
            <= (now - state.last_reported).total_seconds()
            < settings.screen_wind_max_age_minutes * 60
        ):
            return None
        return state

    def _wind_readings(self, now):
        state = self._wind_state(now)
        if state is None:
            return None, None
        unit = state.attributes.get("wind_speed_unit")
        return tuple(
            speed_kmh(state.attributes.get(key), unit)
            for key in ("wind_speed", "wind_gust_speed")
        )

    def _wind_expiry(self, now):
        settings = self.building.settings
        state = (
            self.hass.states.get(settings.weather_entity)
            if settings.weather_entity and self._wind_configured()
            else None
        )
        if state is not None:
            until = state.last_reported + timedelta(
                minutes=settings.screen_wind_max_age_minutes
            )
            if until > now:
                return until
        return None

    def _update_wind(self, now):
        settings = self.building.settings
        policy = (
            settings.weather_entity,
            settings.screen_wind_speed_limit,
            settings.screen_wind_gust_limit,
        )
        memory = self.wind_memory
        if policy != self.wind_policy:
            memory = WindMemory(memory.active)
        self.wind_policy = policy
        self.wind_speed, self.wind_gust = self._wind_readings(now)
        if not self._wind_configured():
            status = (
                "disabled" if not settings.screen_wind_protection else "not_configured"
            )
            self.wind = WindProtection(status, WindMemory())
        else:
            self.wind = wind_protection(
                self.wind_speed,
                self.wind_gust,
                settings.screen_wind_speed_limit,
                settings.screen_wind_gust_limit,
                memory,
                now,
                settings.screen_wind_calm_minutes,
                condition=state.state
                if (state := self._wind_state(now)) is not None
                else None,
            )
        self.wind_memory = self.wind.memory
        if self.wind_memory != memory:
            self._save_screen_state()

    def _wind_blocks_closing(self, now):
        if not self._wind_configured():
            return False
        settings = self.building.settings
        speed, gust = self._wind_readings(now)
        return wind_protection(
            speed,
            gust,
            settings.screen_wind_speed_limit,
            settings.screen_wind_gust_limit,
            self.wind_memory,
            now,
            settings.screen_wind_calm_minutes,
            condition=state.state
            if (state := self._wind_state(now)) is not None
            else None,
        ).block_closing

    def _rain_source(self):
        settings = self.building.settings
        return (
            (settings.screen_rain_entity or settings.weather_entity)
            if settings.screen_rain_protection
            else None
        )

    def _rain_reading(self, now):
        entity_id = self._rain_source()
        state = self.hass.states.get(entity_id) if entity_id else None
        if (
            state is None
            or state.attributes.get("restored")
            or state.attributes.get("assumed_state")
        ):
            return None
        max_age = self.building.settings.screen_rain_max_age_minutes * 60
        if (
            entity_id.startswith("weather.")
            and not -5 <= (now - state.last_reported).total_seconds() < max_age
        ):
            return None
        return current_rain(state.state, entity_id.split(".", 1)[0])

    def _rain_expiry(self, now):
        entity_id = self._rain_source()
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is not None and entity_id.startswith("weather."):
            expiry = state.last_reported + timedelta(
                minutes=self.building.settings.screen_rain_max_age_minutes
            )
            if expiry > now:
                return expiry
        return None

    def _update_rain(self, now):
        source = self._rain_source()
        memory = self.rain_memory
        if source != self.rain_source:
            memory = RainMemory(memory.wet)
        self.rain_source = source
        if source is None:
            status = (
                "disabled"
                if not self.building.settings.screen_rain_protection
                else "not_configured"
            )
            self.rain = RainProtection(status, RainMemory())
        else:
            self.rain = rain_protection(
                self._rain_reading(now),
                memory,
                now,
                self.building.settings.screen_rain_dry_minutes,
            )
        self.rain_memory = self.rain.memory
        if self.rain_memory != memory:
            self._save_screen_state()

    def _rain_blocks_closing(self, now):
        if self._rain_source() is None:
            return False
        # Re-read live inputs: a rain update may arrive after planning a close.
        return rain_protection(
            self._rain_reading(now),
            self.rain_memory,
            now,
            self.building.settings.screen_rain_dry_minutes,
        ).block_closing

    def _screen_block(self) -> str | None:
        """Only a trustworthy off state releases a configured block."""
        entity_id = self.building.settings.screen_block_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state not in ("on", "off")
            or (
                state.attributes.get("restored")
                or state.attributes.get("assumed_state")
            )
        ):
            return "block_unknown"
        return "blocked" if state.state == "on" else None

    def _screen_guard(self, item, position=None) -> str | None:
        """Recheck live inputs immediately before dispatching a movement."""
        if (
            not self.active_capable
            or not self.thermostat(item.zone_id).screen_regulation
        ):
            return "screen_disabled"
        if block := self._screen_block():
            return block
        if (
            position is not None
            and position < 100
            and self._wind_blocks_closing(dt_util.utcnow())
        ):
            return "wind_blocked"
        if (
            position is not None
            and position < 100
            and self._rain_blocks_closing(dt_util.utcnow())
        ):
            return "rain_blocked"
        state = self.hass.states.get(item.entity_id)
        if (
            state is None
            or state.state not in ("open", "closed", "opening", "closing")
            or (
                state.attributes.get("restored")
                or state.attributes.get("assumed_state")
            )
        ):
            return "cover_unavailable"
        return None

    def _clear_sun_issue(self):
        ir.async_delete_issue(self.hass, DOMAIN, f"sun_unavailable_{self.building.id}")

    def _sun_position(self):
        """Expose missing solar input through Repairs, and hold until recovery."""
        settings = self.building.settings
        sun = self.hass.states.get(settings.sun_entity)
        values = tuple(
            sun.attributes.get(key) if sun else None for key in ("azimuth", "elevation")
        )
        valid = (
            sun is not None
            and sun.state not in ("unknown", "unavailable")
            and not (
                sun.attributes.get("restored") or sun.attributes.get("assumed_state")
            )
            and all(
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                for v in values
            )
        )
        valid = valid and 0 <= values[0] <= 360 and -90 <= values[1] <= 90
        if valid or not any(item.kind == "shading" for item in self.building.equipment):
            self._clear_sun_issue()
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"sun_unavailable_{self.building.id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="sun_unavailable",
                translation_placeholders={
                    "building": self.building.name,
                    "entity": settings.sun_entity,
                },
            )
        return values if valid else (None, None)
