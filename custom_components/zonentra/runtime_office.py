"""Central opening hours prepare ordinary rooms without inventing presence."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .bookings import Booking, BookingPlan, ThermalRates, outdoor_band, plan_booking
from .demand import comfort_limits
from .observations import max_age_for, read
from .presence import read_presence

ARRIVAL_GRACE = timedelta(minutes=15)
TRANSITION_GRACE = timedelta(seconds=30)


@dataclass(frozen=True)
class OfficePlan(BookingPlan):
    """Schedule preparation; an open office alone never requests comfort."""

    @property
    def active(self):
        return self.status in ("preparing", "awaiting_opening", "arrival_grace")


class OfficeRuntimeMixin:
    def _update_room_controller_use(self, transitions, sources):
        """Keep observed manual use until switch-off/arming, across closing time.

        A running unit at startup, an automation or our own command is no proof
        of manual use. Without opening hours the timed override policy remains.
        """
        settings = self.building.settings
        presence = read_presence(
            settings.presence_entity, sources.get(settings.presence_entity)
        )
        selected = settings.workday_presence_zone_id
        self.workday_presence_suppressed.intersection_update({selected})
        if presence == "absent":
            self.workday_presence_suppressed.clear()
        elif (
            presence == "present"
            and selected
            and any(
                item.zone_id == selected and transitions.get(item.id) == "manual_off"
                for item in self.building.equipment
                if item.kind == "hvac"
            )
        ):
            self.workday_presence_suppressed.add(selected)
        enabled = bool(
            settings.office_schedule_entity
            and settings.presence_entity
            and settings.manual_override_enabled
        )
        for item in self.building.equipment:
            if item.kind != "hvac":
                continue
            transition = transitions.get(item.id)
            thermostat = self.thermostat(item.zone_id)
            allowed = (
                enabled
                and thermostat.hvac_mode != "off"
                and thermostat.preset == "comfort"
            )
            if not allowed or presence == "absent" or transition == "manual_off":
                if item.id in self.room_controller_use:
                    self.room_controller_use.discard(item.id)
                    self.overrides.pop(item.id, None)
            elif transition == "manual_on" and presence == "present":
                self.room_controller_use.add(item.id)

    def _office_occupancy(self, zone, occupancy, sources, now):
        entity = self.building.settings.office_schedule_entity
        previous = self.office_plans.get(zone.id)
        plan = OfficePlan("not_configured")
        if entity and zone.calendar_entity:
            plan = OfficePlan("calendar_priority")
        elif entity and (workday_plan := self._office_workday(sources, now)):
            plan = workday_plan
        elif entity:
            source = sources.get(entity)
            state = self.hass.states.get(entity)
            raw = state.attributes.get("next_event") if state else None
            next_event = (
                raw
                if isinstance(raw, datetime)
                else dt_util.parse_datetime(raw)
                if isinstance(raw, str)
                else None
            )
            if (
                source is None
                or not source.trustworthy
                or source.state not in ("on", "off")
            ):
                plan = OfficePlan("unknown")
            elif source.state == "off" and next_event is None:
                # An empty helper has no future opening.
                plan = OfficePlan("closed") if raw is None else OfficePlan("unknown")
            elif (
                source.state == "off"
                and previous is not None
                and (previous.active or previous.status == "awaiting_opening")
                and next_event == previous.start
                and previous.start <= now < previous.start + TRANSITION_GRACE
            ):
                # The deadline can precede HA's on event. Retain the arrival
                # reference briefly to avoid an off/on HVAC cycle at opening.
                plan = replace(
                    previous,
                    status="awaiting_opening",
                    next_check=previous.start + TRANSITION_GRACE,
                )
            elif next_event is None or next_event.tzinfo is None or next_event <= now:
                plan = OfficePlan("unknown")
            elif source.state == "off":
                plan = self._office_preparation(
                    zone, sources, now, next_event, previous
                )
            else:
                plan = OfficePlan("open", end=next_event, next_check=next_event)
                # Only continue preparation across an observed opening. Starting
                # HA in an already open office must not fabricate an arrival.
                if (
                    previous is not None
                    and (previous.active or previous.status == "awaiting_opening")
                    and previous.start is not None
                    and source.changed_at is not None
                    and abs((source.changed_at - previous.start).total_seconds()) <= 60
                    and previous.start <= now < previous.start + ARRIVAL_GRACE
                    and occupancy.presence == "absent"
                ):
                    plan = replace(
                        previous,
                        status="arrival_grace",
                        end=next_event,
                        next_check=min(next_event, previous.start + ARRIVAL_GRACE),
                    )
        self.office_plans[zone.id] = plan
        if plan.next_check:
            self._office_checks.append(plan.next_check)
        if plan.active:
            return replace(occupancy, mode="comfort", reason=f"office_{plan.status}")
        if entity and self.building.settings.presence_entity:
            if occupancy.presence != "absent" and any(
                item.id in self.room_controller_use
                for item in self.building.equipment
                if item.kind == "hvac" and item.zone_id == zone.id
            ):
                return replace(occupancy, mode="comfort", reason="room_controller_use")
            settings = self.building.settings
            workday = sources.get(settings.workday_entity)
            if (
                zone.id == settings.workday_presence_zone_id
                and not zone.calendar_entity
                and occupancy.presence == "present"
                and dt_util.as_local(now).weekday() < 5
                and workday is not None
                and workday.trustworthy
                and workday.state == "on"
                and zone.id not in self.workday_presence_suppressed
            ):
                # Presence only selects this room; ordinary regulation (including
                # setpoint correction) remains active, without a manual override.
                self._office_checks.append(
                    dt_util.start_of_local_day(
                        dt_util.as_local(now) + timedelta(days=1)
                    )
                )
                return replace(occupancy, mode="comfort", reason="workday_presence")
            # Bookings and room motion retain their own occupancy evidence.
            if plan.status != "open" and occupancy.reason in ("present", "default"):
                return replace(occupancy, mode="away", reason="outside_office_hours")
        return occupancy

    def _office_workday(self, sources, now):
        """The optional binary sensor represents today, independently of hours."""
        entity = self.building.settings.workday_entity
        if not entity:
            return None
        source = sources.get(entity)
        midnight = dt_util.start_of_local_day(dt_util.as_local(now) + timedelta(days=1))
        if (
            source is None
            or not source.trustworthy
            or source.state not in ("on", "off")
        ):
            return OfficePlan("workday_unknown", next_check=midnight)
        if source.state == "off":
            return OfficePlan("not_workday", next_check=midnight)
        return None

    def _office_preparation(self, zone, sources, now, opening, previous):
        if (
            self.building.settings.workday_entity
            and dt_util.as_local(opening).date() != dt_util.as_local(now).date()
        ):
            # Today's sensor cannot approve tomorrow's opening. Reassess after
            # local midnight and on sensor changes, including across DST.
            midnight = dt_util.start_of_local_day(
                dt_util.as_local(now) + timedelta(days=1)
            )
            return OfficePlan("waiting_for_workday", start=opening, next_check=midnight)
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
        # The synthetic end is only a planner boundary. Actual schedule state
        # and next_event decide whether the office is open and when it closes.
        plan = plan_booking(
            (Booking(opening, opening + ARRIVAL_GRACE),),
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
            previous=previous,
            outdoor=outside,
        )
        return OfficePlan(**asdict(plan))
