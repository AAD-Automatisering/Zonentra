"""Room booking retrieval, adaptive preparation and persisted thermal learning."""

import asyncio
import math
from dataclasses import asdict, replace
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .bookings import (
    BookingPlan,
    ThermalRates,
    assess_booking,
    booking_priority,
    learn_booking_margin,
    learn_rate,
    outdoor_band,
    parse_bookings,
    plan_booking,
)
from .demand import comfort_limits
from .observations import max_age_for, read
from .temperature import convert_temperature

CALENDAR_REFRESH_MINUTES = 5
CALENDAR_MAX_AGE_MINUTES = 10
CALENDAR_HORIZON_HOURS = 24
BOOKING_ARRIVAL_GRACE_MINUTES = 15


class BookingRuntimeMixin:
    """Calendars request comfort; existing room and group controls authorize HVAC."""

    def _booking_priority(self, zone, demand, now):
        plan = (
            self.booking_plans[zone.id]
            if zone.calendar_entity
            else self.office_plans[zone.id]
        )
        if (
            not plan.active
            or self.zone_bands[zone.id][0] != "comfort"
            or not self.regulated(zone.id)
            or demand.demand not in ("heating_demand", "cooling_demand")
            or any(
                item.id in self.overrides
                for item in self.building.equipment
                if item.zone_id == zone.id and item.kind == "hvac"
            )
        ):
            return demand
        if plan.status == "preparing":
            self._booking_checks.append(min(plan.start, now + timedelta(minutes=1)))
        return replace(demand, priority=booking_priority(plan, now))

    def load_thermal_rates(self, data):
        if not isinstance(data, dict):
            return
        for zone in self.building.zones:
            result = (
                data.get("_results", {}).get(zone.id)
                if isinstance(data.get("_results"), dict)
                else None
            )
            if (
                isinstance(result, dict)
                and result.get("status")
                in ("ready", "too_cold", "too_warm", "unknown", "not_evaluated")
                and isinstance(result.get("start"), str)
                and (start := dt_util.parse_datetime(result["start"])) is not None
                and start.tzinfo is not None
            ):
                deficit = result.get("deficit")
                if deficit is None or (
                    isinstance(deficit, (int, float))
                    and not isinstance(deficit, bool)
                    and math.isfinite(deficit)
                    and 0 <= deficit <= 100
                ):
                    self.booking_results[zone.id] = {
                        "status": result["status"],
                        "start": start.isoformat(),
                        "deficit": deficit,
                    }
            stored = data.get(zone.id)
            if isinstance(stored, dict):
                self.thermal_rates[zone.id] = {
                    band: ThermalRates.restore(values)
                    for band, values in stored.items()
                    if band in ("unknown", "freezing", "cold", "mild", "warm", "hot")
                }

    def _save_thermal_rates(self):
        if self.thermal_store is None:
            return
        self.thermal_store.async_delay_save(
            lambda: {
                "_results": dict(self.booking_results),
                **{
                    zone: {band: asdict(rates) for band, rates in values.items()}
                    for zone, values in self.thermal_rates.items()
                },
            },
            5,
        )

    async def async_update_bookings(self, _now=None):
        if self._closed or self._calendar_refreshing:
            return
        entities = {z.calendar_entity for z in self.building.zones if z.calendar_entity}
        if not entities:
            return
        self._calendar_refreshing = True
        now = dt_util.utcnow()

        async def fetch(entity):
            try:
                async with asyncio.timeout(30):
                    response = await self.hass.services.async_call(
                        "calendar",
                        "get_events",
                        {
                            "entity_id": entity,
                            "start_date_time": now.isoformat(),
                            "end_date_time": (
                                now + timedelta(hours=CALENDAR_HORIZON_HOURS)
                            ).isoformat(),
                        },
                        blocking=True,
                        return_response=True,
                    )
            except HomeAssistantError, TimeoutError:
                return entity, None, "service_error"
            record = response.get(entity) if isinstance(response, dict) else None
            events = parse_bookings(
                record.get("events") if isinstance(record, dict) else None,
                dt_util.DEFAULT_TIME_ZONE,
            )
            return entity, events, None if events is not None else "invalid_response"

        try:
            records = await asyncio.gather(*(fetch(entity) for entity in entities))
            if self._closed:
                return  # A response must not revive an unloaded runtime.
            for entity, events, error in records:
                self.booking_events[entity] = events
                self.booking_updated[entity] = now
                self.booking_errors[entity] = error
            self.refresh()
        finally:
            self._calendar_refreshing = False

    def _booking_occupancy(self, zone, occupancy, sources, now):
        if not zone.calendar_entity:
            self.booking_plans[zone.id] = BookingPlan("not_configured")
            return occupancy
        source = sources.get(zone.calendar_entity)
        updated = self.booking_updated.get(zone.calendar_entity)
        fresh = updated is not None and now < updated + timedelta(
            minutes=CALENDAR_MAX_AGE_MINUTES
        )
        events = (
            self.booking_events.get(zone.calendar_entity)
            if fresh
            and source is not None
            and source.trustworthy
            and source.state in ("on", "off")
            else None
        )
        temperature = read(
            sources.get(zone.temperature_entity),
            "temperature",
            now,
            max_age_for(zone.sensor_validation, zone.max_age_seconds),
        )
        thermostat = self.thermostat(zone.id)
        own_band = (
            (thermostat.heating_limit, thermostat.cooling_limit)
            if thermostat.has_band and not zone.use_building_limits
            else None
        )
        heat, cool = comfort_limits(zone, sources, now, own_band)
        outside = self._booking_outdoor_temperature()
        rates = self.thermal_rates.get(zone.id, {}).get(
            outdoor_band(outside), ThermalRates()
        )
        plan = plan_booking(
            events,
            now,
            temperature.value if temperature.quality == "valid" else None,
            heat.value if heat.quality == "valid" else None,
            cool.value if cool.quality == "valid" else None,
            zone.hysteresis,
            rates,
            heating_rate=zone.preconditioning_heating_rate,
            cooling_rate=zone.preconditioning_cooling_rate,
            buffer_minutes=zone.preconditioning_buffer_minutes,
            max_minutes=zone.preconditioning_max_minutes,
            previous=self.booking_plans.get(zone.id),
            outdoor=outside,
        )
        self._booking_result(zone, plan, temperature, heat, cool, sources, now)
        self.booking_plans[zone.id] = plan
        if plan.next_check:
            self._booking_checks.append(plan.next_check)
        if updated and fresh:
            self._booking_checks.append(
                updated + timedelta(minutes=CALENDAR_MAX_AGE_MINUTES)
            )
        if plan.status == "meeting" and occupancy.presence == "absent":
            arrival_deadline = plan.start + timedelta(
                minutes=BOOKING_ARRIVAL_GRACE_MINUTES
            )
            if now >= arrival_deadline:
                return replace(occupancy, reason="booking_no_arrival")
            self._booking_checks.append(min(arrival_deadline, plan.end))
        if plan.active:
            # Prepare before arrival even while the building is absent.
            # Explicit thermostat off/eco/away still takes precedence later.
            return replace(occupancy, mode="comfort", reason=f"booking_{plan.status}")
        if occupancy.presence == "absent":
            return occupancy
        # Calendar rooms use eco unless a usable booking requests comfort.
        # Motion/presence remain observable but do not activate an unbooked room.
        return replace(
            occupancy,
            mode="eco",
            reason="calendar_unknown" if plan.status == "unknown" else "no_booking",
        )

    def _booking_result(self, zone, plan, temperature, heat, cool, sources, now):
        previous = self.booking_plans.get(zone.id)
        thermostat = self.thermostat(zone.id)
        units = [
            item
            for item in self.building.equipment
            if item.zone_id == zone.id and item.kind == "hvac"
        ]
        eligible = (
            self.regulated(zone.id)
            and thermostat.hvac_mode in ("heat", "cool", "heat_cool", "auto")
            and not (plan.direction == "heating" and thermostat.hvac_mode == "cool")
            and not (plan.direction == "cooling" and thermostat.hvac_mode == "heat")
            and thermostat.preset == "comfort"
            and not thermostat.manual
            and bool(units)
            and not any(
                command.equipment_id in {item.id for item in units}
                for command, _, _ in self.command_retries.values()
            )
            and all(
                item.id not in self.overrides
                and not self._group_block(item.group_id)
                and not (
                    item.group_id in self.group_decisions
                    and self.group_decisions[item.group_id].blocked_reason
                )
                and (source := sources.get(item.entity_id)) is not None
                and source.trustworthy
                and source.state not in (None, "unknown", "unavailable")
                for item in units
            )
            and all(
                (source := sources.get(entity)) is not None
                and source.trustworthy
                and source.state == "off"
                for entity in zone.contact_entities
            )
        )
        if plan.status == "preparing":
            start, interrupted, direction = self.booking_interrupted.get(
                zone.id, (None, False, None)
            )
            self.booking_interrupted[zone.id] = (
                plan.start,
                (interrupted if start == plan.start else False) or not eligible,
                direction if start == plan.start and direction else plan.direction,
            )
            return
        if plan.status == "unknown" and zone.id in self.booking_interrupted:
            start, _, direction = self.booking_interrupted[zone.id]
            self.booking_interrupted[zone.id] = (start, True, direction)
            return
        progress = self.booking_interrupted.pop(zone.id, None)
        if (
            plan.status != "meeting"
            or previous is None
            or previous.status != "preparing"
            or previous.start != plan.start
            or now > plan.start + timedelta(minutes=5)
        ):
            return
        if self.booking_results.get(zone.id, {}).get("start") == plan.start.isoformat():
            return
        fresh = (
            temperature.quality == "valid"
            and temperature.reported_at is not None
            and temperature.reported_at >= plan.start - timedelta(minutes=5)
        )
        status, deficit = assess_booking(
            temperature.value if fresh else None,
            heat.value if heat.quality == "valid" else None,
            cool.value if cool.quality == "valid" else None,
            eligible and progress is not None and not progress[1],
        )
        self.booking_results[zone.id] = {
            "start": plan.start.isoformat(),
            "status": status,
            "deficit": deficit,
        }
        band = outdoor_band(self._booking_outdoor_temperature())
        rates = self.thermal_rates.setdefault(zone.id, {}).get(band, ThermalRates())
        self.thermal_rates[zone.id][band] = learn_booking_margin(
            rates, status, progress[2] if progress else None
        )
        self._save_thermal_rates()

    def _booking_outdoor_temperature(self):
        if self.building.settings.outdoor_temperature_entity:
            return (
                self.outdoor_temperature.value
                if self.outdoor_temperature.quality == "valid"
                else None
            )
        # An existing weather entity can provide current outdoor temperature
        # when no dedicated outdoor sensor was configured.
        entity_id = self.building.settings.weather_entity
        state = self.hass.states.get(entity_id) if entity_id else None
        if (
            state is None
            or state.state in ("unknown", "unavailable")
            or state.attributes.get("restored")
            or state.attributes.get("assumed_state")
        ):
            return None
        return convert_temperature(
            state.attributes.get("temperature"),
            state.attributes.get("temperature_unit")
            or self.hass.config.units.temperature_unit,
        )

    def _learn_booking_rates(self, now):
        for zone in self.building.zones:
            if (
                not zone.calendar_entity
                and not self.building.settings.office_schedule_entity
            ):
                continue
            temperature = self.zone_snapshots[zone.id].temperature
            units = [
                u
                for u in self.indoor_units.values()
                if u.zone_id == zone.id and u.available
            ]
            modes = {
                u.activity
                if u.activity is not None
                else {"heat": "heating", "cool": "cooling"}.get(u.selected_mode)
                for u in units
            }
            mode = next(iter(modes)) if len(modes) == 1 else None
            blockers = []
            if not self.regulated(zone.id):
                blockers.append("room_regulation_disabled")
            if self.thermostat(zone.id).hvac_mode == "off":
                blockers.append("thermostat_off")
            if self.windows.get(zone.id) in ("open", "opening", "unknown"):
                blockers.append("window_not_closed")
            if temperature.quality != "valid":
                blockers.append("invalid_room_temperature")
            if mode not in ("heating", "cooling"):
                blockers.append(
                    "no_available_units"
                    if not units
                    else "mixed_unit_activity"
                    if len(modes) > 1
                    else "no_active_heating_or_cooling"
                )
            linked = [
                item
                for item in self.building.equipment
                if item.kind == "hvac" and item.zone_id == zone.id
            ]
            if any(self._group_block(item.group_id) for item in linked):
                blockers.append("group_disabled")
            if any(item.id in self.overrides for item in linked):
                blockers.append("manual_override")
            eligible = not blockers
            outside = self._booking_outdoor_temperature()
            band = outdoor_band(outside)
            by_band = self.thermal_rates.setdefault(zone.id, {})
            rates = by_band.get(band, ThermalRates())
            context = (mode if eligible else None, band)
            if self.thermal_context.get(zone.id) != context:
                self.thermal_context[zone.id] = context
                self.thermal_samples[zone.id] = None
                self.thermal_reports.pop(zone.id, None)
                self.thermal_started[zone.id] = now
            reported = temperature.reported_at
            previous_observation = self.thermal_learning.get(zone.id, {}).get(
                "last_sample"
            )
            explanation = {
                "last_sample": previous_observation,
                "eligible": eligible,
                "reason": blockers[0] if blockers else "waiting_for_temperature_report",
                "blockers": blockers,
                "mode": mode,
                "activity_source": "reported_action"
                if units and all(u.activity is not None for u in units)
                else "selected_mode"
                if units and all(u.activity is None for u in units)
                else "mixed"
                if units
                else "none",
                "available_units": len(units),
                "linked_units": len(linked),
                "reported_inactive_units": sum(u.activity == "inactive" for u in units),
                "temperature_quality": temperature.quality,
                "temperature_reported_at": reported.isoformat() if reported else None,
            }
            self.thermal_learning[zone.id] = explanation
            if not eligible:
                self.thermal_samples[zone.id] = None
                continue
            if reported is None or reported < self.thermal_started.get(zone.id, now):
                explanation["reason"] = "waiting_for_fresh_temperature_report"
                continue
            if reported <= self.thermal_reports.get(
                zone.id, reported - timedelta(microseconds=1)
            ):
                continue
            self.thermal_reports[zone.id] = reported
            previous_sample = self.thermal_samples.get(zone.id)
            learned, sample = learn_rate(
                rates,
                self.thermal_samples.get(zone.id),
                temperature.reported_at or now,
                temperature.value if eligible else None,
                mode,
                outside,
                diagnostics=explanation,
            )
            explanation["last_sample"] = {
                "reason": explanation["reason"],
                "at": reported.isoformat(),
                "mode": mode,
                "outdoor_band": band,
                "window_minutes": (reported - previous_sample.at).total_seconds() / 60
                if previous_sample is not None
                else None,
            }
            self.thermal_samples[zone.id] = sample
            if learned != rates:
                by_band[band] = learned
                self._save_thermal_rates()
