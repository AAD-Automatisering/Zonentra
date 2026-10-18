"""Compact read-only diagnostics for the building, zones, indoor units and groups."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from . import ZonentraConfigEntry, central_presence, entry_type
from .bookings import ThermalRates, outdoor_band, rate_quality
from .const import DOMAIN
from .entity import zone_context
from .floor_heating import MINIMUM_DEFICIT, MINIMUM_RISE, WATCH_MINUTES
from .models import Equipment, Zone
from .observations import cover_position, describe
from .topology import ZONE, zone_equipment


async def async_setup_entry(
    hass, entry: ZonentraConfigEntry, async_add_entities
) -> None:
    if entry_type(entry) == ZONE:
        entities = _zone_entities(hass, entry)
    else:
        entities = _building_entities(entry)
    # Options reload removes obsolete diagnostics; renames retain stable IDs.
    expected = {entity.unique_id for entity in entities}
    registry = er.async_get(hass)
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            record.platform == DOMAIN
            and record.domain == "sensor"
            and record.unique_id not in expected
        ):
            registry.async_remove(record.entity_id)
    async_add_entities(entities)


def _building_entities(entry) -> list:
    """Legacy entries keep every entity; central entries keep building/groups."""
    runtime = entry.runtime_data
    legacy = entry_type(entry) == "legacy"
    hvac = [item for item in runtime.building.equipment if item.kind == "hvac"]
    groups = runtime.building.groups
    entities = [ZonentraStatusSensor(entry)]
    if legacy:
        entities.extend(
            ZoneDemandSensor(entry, zone) for zone in runtime.building.zones
        )
    entities.extend(GroupModeSensor(entry, group) for group in groups)
    if legacy:
        entities.extend(IndoorUnitSensor(entry, item) for item in hvac)
    entities.extend(DerivedGroupModeSensor(entry, group) for group in groups)
    if legacy:
        entities.extend(UnitControlSensor(entry, item) for item in hvac)
    entities.extend(GroupCoordinationSensor(entry, group) for group in groups)
    if not legacy and runtime.building.settings.weather_entity:
        entities.extend(
            [
                ForecastTemperatureSensor(entry, "maximum"),
                ForecastTemperatureSensor(entry, "minimum"),
                DayTypeSensor(entry),
            ]
        )
    return entities


def _zone_entities(hass, entry) -> list:
    """A zone device with its demand and indoor-unit sensors (central runtime)."""
    options = dict(entry.options)
    shared = zone_context(hass, entry)
    zone = Zone(options["id"], options.get("name", entry.title))
    entities = [
        ZoneDemandSensor(
            entry,
            zone,
            unique_id=f"{entry.entry_id}_demand",
            translation_key="room_demand",
            **shared,
        ),
        ActiveBandSensor(entry, zone, **shared),
        SafetySensor(entry, zone, **shared),
        EmaTemperatureSensor(entry, zone, **shared),
        TemperatureSlopeSensor(entry, zone, **shared),
    ]
    # 0.13.3: only what is configured. Presence needs a motion sensor in the
    # zone or building presence; setpoint and fan need indoor units.
    if options.get("occupancy_entity") or central_presence(hass):
        entities.append(PresenceSensor(entry, zone, **shared))
    if options.get("calendar_entity"):
        entities.append(BookingSensor(entry, zone, **shared))
    if options.get("floor_heating_entities") and options.get("indoor_units"):
        entities.append(HeatingProgressSensor(entry, zone, **shared))
    if options.get("indoor_units"):
        entities.append(RegulatedSetpointSensor(entry, zone, **shared))
        entities.append(AutoFanSensor(entry, zone, **shared))
    if options.get("contact_entities"):  # 0.13.3: only with window contacts.
        entities.append(WindowSensor(entry, zone, **shared))
    for record in zone_equipment(options):
        if record["kind"] == "shading":
            state = hass.states.get(record["entity_id"])
            item = Equipment(
                **{**record, "name": state.name if state else record["name"]}
            )
            entities.append(
                ScreenSensor(
                    entry,
                    item,
                    unique_id=f"{entry.entry_id}_{item.id}_screen",
                    **shared,
                )
            )
            continue
        if record["kind"] == "floor_heating":
            state = hass.states.get(record["entity_id"])
            item = Equipment(
                **{**record, "name": state.name if state else record["name"]}
            )
            entities.append(FloorHeatingSensor(entry, item, **shared))
            continue
        if record["kind"] != "hvac":
            continue
        state = hass.states.get(record["entity_id"])
        item = Equipment(**{**record, "name": state.name if state else record["name"]})
        entities.append(
            IndoorUnitSensor(
                entry,
                item,
                unique_id=f"{entry.entry_id}_{item.id}_indoor_unit",
                **shared,
            )
        )
        entities.append(
            UnitControlSensor(
                entry, item, unique_id=f"{entry.entry_id}_{item.id}_control", **shared
            )
        )
    return entities


class ZonentraDiagnosticSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, provider=None, signal=None, device=None):
        self._provider = provider or (lambda: entry.runtime_data)
        self._signal = signal or f"{DOMAIN}_{entry.entry_id}_updated"
        self._attr_device_info = device or DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data["name"],
            manufacturer="Zonentra",
        )

    @property
    def runtime(self):
        """The building runtime; a zone reads the central one (None if unloaded)."""
        return self._provider()

    def _present(self, runtime) -> bool:
        return True

    @property
    def available(self) -> bool:
        runtime = self.runtime
        return runtime is not None and self._present(runtime)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._signal, self._updated)
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()


class ZonentraStatusSensor(ZonentraDiagnosticSensor):
    _attr_name = "Status"
    _attr_icon = "mdi:office-building-cog"

    def __init__(self, entry):
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self):
        return self.runtime.status

    @property
    def extra_state_attributes(self):
        return self.runtime.summary()


class ZoneDemandSensor(ZonentraDiagnosticSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["heating_demand", "cooling_demand", "neutral", "unknown"]
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_translation_key = "zone_demand"

    def __init__(self, entry, zone, unique_id=None, translation_key=None, **shared):
        super().__init__(entry, **shared)
        self._zone = zone
        self._attr_translation_placeholders = {"zone": zone.name}
        self._attr_unique_id = unique_id or f"{entry.entry_id}_zone_{zone.id}_demand"
        if translation_key:
            self._attr_translation_key = translation_key

    @property
    def zone(self) -> Zone:
        """The effective zone from the current building (after reloads)."""
        runtime = self.runtime
        zones = runtime.building.zones if runtime else ()
        return next((zone for zone in zones if zone.id == self._zone.id), self._zone)

    def _present(self, runtime) -> bool:
        return self._zone.id in runtime.zone_demands

    def _occupancy(self) -> dict:
        item = self.runtime.zone_occupancy.get(self._zone.id)
        if item is None:
            return {}
        return {
            "occupancy_mode": item.mode,
            "occupancy_reason": item.reason,
            "building_presence": item.presence,
            "motion": item.motion,
            **self._office_schedule(),
        }

    def _office_schedule(self) -> dict:
        plan = self.runtime.office_plans.get(self._zone.id)
        if plan is None or plan.status == "not_configured":
            return {}
        return {
            "office_schedule_status": plan.status,
            "office_opening": plan.start.isoformat() if plan.start else None,
            "office_prepare_at": plan.prepare_at.isoformat()
            if plan.prepare_at
            else None,
            "office_lead_minutes": plan.lead_minutes,
            "office_learned_rate": plan.learned,
        }

    def _thermostat(self) -> dict:
        """0.9 room thermostat; the band may differ from motion by preset."""
        runtime = self.runtime
        if not runtime.active_capable:
            return {}
        thermostat = runtime.thermostat(self._zone.id)
        band, reason = runtime.zone_bands[self._zone.id]
        return {
            "read_only": not runtime.regulated(self._zone.id),
            "thermostat_mode": thermostat.hvac_mode,
            "thermostat_preset": thermostat.preset,
            "active_band": band,
            "active_band_reason": reason,
            "room_regulation": runtime.regulated(self._zone.id),
            "airco_control_strategy": "native_trial"
            if self.zone.native_regulation
            else "zonentra",
            "native_regulation_active": runtime.regulated(self.zone.id)
            and runtime.native_regulation(self.zone),
        }

    @property
    def native_value(self):
        return self.runtime.zone_demands[self.zone.id].demand

    @property
    def extra_state_attributes(self):
        demand = self.runtime.zone_demands[self.zone.id]
        snapshot = self.runtime.zone_snapshots[self.zone.id]
        health = {
            field: getattr(snapshot, field).quality
            for field in (
                "temperature",
                "heating_limit",
                "cooling_limit",
                "outdoor_temperature",
                "occupancy",
            )
        }
        return {
            "read_only": True,
            "limits_source": "building" if self.zone.use_building_limits else "zone",
            "parameters_source": "building"
            if self.zone.use_building_parameters
            else "zone",
            "sensor_validation": self.zone.sensor_validation,
            "heating_score": demand.heating_score,
            "cooling_score": demand.cooling_score,
            "temperature_c": snapshot.temperature.value,
            "heating_limit_c": snapshot.heating_limit.value,
            "cooling_limit_c": snapshot.cooling_limit.value,
            "weight": self.zone.weight,
            "hysteresis_c": self.zone.hysteresis,
            "reason": demand.reason,
            "sensor_health": demand.sensor_health,
            "source_health": health,
            # Health, strategy, ages and whether freshness is actually verifiable.
            "temperature_validation": describe(
                snapshot.temperature, self.runtime.evaluated_at
            ),
            "occupancy": snapshot.occupancy.value or "unknown",
            # 0.8: which band applies and why (presence wins over motion).
            **self._occupancy(),
            **self._thermostat(),
            "open_contacts": sum(
                contact.value == "open" for contact in snapshot.contacts
            ),
            "unknown_contacts": sum(
                contact.quality != "valid" for contact in snapshot.contacts
            ),
        }


class GroupModeSensor(ZonentraDiagnosticSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["heat", "cool", "off", "unknown"]
    _attr_icon = "mdi:heat-pump-outline"
    _attr_translation_key = "group_mode"

    def __init__(self, entry, group):
        super().__init__(entry)
        self._group = group  # Not "group": HA reserves that entity attribute.
        self._attr_translation_placeholders = {"group": group.name}
        self._attr_unique_id = f"{entry.entry_id}_group_{group.id}_mode"

    @property
    def native_value(self):
        return self.runtime.group_decisions[self._group.id].proposed_mode

    @property
    def extra_state_attributes(self):
        decision = self.runtime.group_decisions[self._group.id]
        return {
            "read_only": True,
            "current_mode": decision.current_mode,
            "desired_mode": decision.desired_mode,
            "heating_score": decision.heating_score,
            "cooling_score": decision.cooling_score,
            "reason": decision.reason,
            "blocked_reason": decision.blocked_reason,
            "mode_change_proposed": decision.mode_change_proposed,
            "blocked_until": decision.blocked_until.isoformat()
            if decision.blocked_until
            else None,
            "member_zones": len(decision.member_zone_ids),
            "reliable_zones": decision.reliable_zones,
            "mode_quality": decision.mode_quality,
            # Physical mode only comes from the configured, trusted feedback.
            "physical_mode": decision.current_mode,
            "mode_feedback_quality": decision.mode_quality,
            "physical_mode_feedback_available": decision.mode_quality == "valid",
            "derived_group_mode": self.runtime.group_observations[
                self._group.id
            ].derived_mode,
            "observation_reason": self.runtime.mode_comparisons[self._group.id],
        }


class IndoorUnitSensor(ZonentraDiagnosticSensor):
    """Selected mode of one linked indoor unit, as reported by Home Assistant."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "heat",
        "cool",
        "heat_cool",
        "auto",
        "dry",
        "fan_only",
        "off",
        "unknown",
    ]
    _attr_icon = "mdi:air-conditioner"
    _attr_translation_key = "indoor_unit"

    def __init__(self, entry, item, unique_id=None, **shared):
        super().__init__(entry, **shared)
        self.item = item
        self._attr_translation_placeholders = {"unit": item.name}
        self._attr_unique_id = (
            unique_id or f"{entry.entry_id}_equipment_{item.id}_indoor_unit"
        )

    def _present(self, runtime) -> bool:
        return self.item.id in runtime.indoor_units

    @property
    def native_value(self):
        unit = self.runtime.indoor_units[self.item.id]
        return (
            unit.selected_mode
            if unit.selected_mode in self._attr_options
            else "unknown"
        )

    @property
    def extra_state_attributes(self):
        unit = self.runtime.indoor_units[self.item.id]
        return {
            "read_only": True,
            "indoor_unit_entity": self.item.entity_id,
            "selected_mode": unit.selected_mode,
            "reported_action": unit.reported_action,
            "current_temperature": unit.current_temperature,
            "target_temperature": unit.target_temperature,
            "temperature_unit": unit.temperature_unit,
            "fan_mode": unit.fan_mode,
            "availability": unit.availability,
            "observation_quality": unit.quality,
            "selected_action_mismatch": unit.mismatch,
            "observed_at": unit.observed_at,
        }


class DerivedGroupModeSensor(ZonentraDiagnosticSensor):
    """Group mode interpreted from indoor units — never physical outdoor feedback."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["heat", "cool", "inactive", "mixed", "unknown"]
    _attr_icon = "mdi:home-search-outline"
    _attr_translation_key = "derived_group_mode"

    def __init__(self, entry, group):
        super().__init__(entry)
        self._group = group  # Not "group": HA reserves that entity attribute.
        self._attr_translation_placeholders = {"group": group.name}
        self._attr_unique_id = f"{entry.entry_id}_group_{group.id}_derived_mode"

    @property
    def native_value(self):
        return self.runtime.group_observations[self._group.id].derived_mode

    @property
    def extra_state_attributes(self):
        observation = self.runtime.group_observations[self._group.id]
        decision = self.runtime.group_decisions[self._group.id]
        return {
            "read_only": True,
            "derived_from": "indoor_units",
            "member_indoor_units": observation.member_units,
            "available_indoor_units": observation.available_units,
            "usable_selected_modes": observation.usable_modes,
            "usable_hvac_actions": observation.usable_actions,
            "selected_modes": dict(observation.selected_modes),
            "selected_mode_consensus": observation.selected_consensus,
            "selected_modes_agree": observation.modes_agree,
            "reported_activity": observation.reported_activity,
            "reported_actions_agree": observation.actions_agree,
            "conflict_detected": observation.conflict,
            "conflicts": list(observation.conflicts),
            "completeness": observation.completeness,
            "quality": observation.quality,
            "reason": observation.reason,
            "notes": list(observation.notes),
            "desired_mode": decision.desired_mode,
            "physical_mode": decision.current_mode,
            "physical_mode_feedback_available": decision.mode_quality == "valid",
            "observation_reason": self.runtime.mode_comparisons[self._group.id],
        }


class ActiveBandSensor(ZonentraDiagnosticSensor):
    """The band the zone thermostat applies now: comfort, eco or away."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["comfort", "eco", "away"]
    _attr_icon = "mdi:account-clock-outline"
    _attr_translation_key = "active_band"
    _attr_entity_category = None  # Shown with the controls, like VTherm presets.

    def __init__(self, entry, zone, **shared):
        super().__init__(entry, **shared)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_active_band"

    def _present(self, runtime) -> bool:
        return self._zone.id in runtime.zone_bands

    @property
    def native_value(self):
        return self.runtime.zone_bands[self._zone.id][0]

    @property
    def extra_state_attributes(self):
        runtime = self.runtime
        snapshot = runtime.zone_snapshots[self._zone.id]
        occupancy = runtime.zone_occupancy.get(self._zone.id)
        return {
            "reason": runtime.zone_bands[self._zone.id][1],
            "preset": runtime.thermostat(self._zone.id).preset,
            "heating_limit_c": snapshot.heating_limit.value,
            "cooling_limit_c": snapshot.cooling_limit.value,
            "building_presence": occupancy.presence if occupancy else None,
            "motion": occupancy.motion if occupancy else None,
        }


class ZoneStateSensor(ZonentraDiagnosticSensor):
    """A room-state sensor on the zone device, like Versatile Thermostat's."""

    _attr_entity_category = None
    key = ""

    def __init__(self, entry, zone, **shared):
        super().__init__(entry, **shared)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_{self.key}"

    def _present(self, runtime) -> bool:
        return self._zone.id in runtime.zone_demands and runtime.active_capable

    def _units(self):
        runtime = self.runtime
        return [
            item
            for item in runtime.building.equipment
            if item.zone_id == self._zone.id and item.kind == "hvac"
        ]


class BookingSensor(ZoneStateSensor):
    """Booking preparation and its temperature/outdoor-dependent estimate."""

    key = "booking"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "not_configured",
        "unknown",
        "idle",
        "scheduled",
        "preparing",
        "meeting",
    ]
    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "booking"

    @property
    def native_value(self):
        plan = self.runtime.booking_plans.get(self._zone.id)
        return plan.status if plan else "unknown"

    @property
    def extra_state_attributes(self):
        runtime = self.runtime
        plan = runtime.booking_plans.get(self._zone.id)
        outside = runtime._booking_outdoor_temperature()
        values = {
            "calendar_error": runtime.booking_errors.get(
                next(
                    (
                        zone.calendar_entity
                        for zone in runtime.building.zones
                        if zone.id == self._zone.id
                    ),
                    None,
                )
            ),
            "outdoor_temperature": outside,
            "outdoor_band": outdoor_band(outside),
            "regulation_enabled": runtime.regulated(self._zone.id),
            "priority": runtime.zone_demands[self._zone.id].priority,
            "last_result": runtime.booking_results.get(self._zone.id),
            "learning_quality": {
                mode: rate_quality(
                    mode,
                    runtime.thermal_rates.get(self._zone.id, {}).get(
                        outdoor_band(outside), ThermalRates()
                    ),
                    runtime.evaluated_at,
                )
                for mode in ("heating", "cooling")
            },
        }
        if plan:
            values.update(
                {
                    "meeting_start": _iso(plan.start),
                    "meeting_end": _iso(plan.end),
                    "preparation_start": _iso(plan.prepare_at),
                    "lead_minutes": round(plan.lead_minutes, 1),
                    "direction": plan.direction,
                    "estimated_rate_c_per_minute": plan.rate,
                    "learned_rate": plan.learned,
                }
            )
        return values


class PresenceSensor(ZoneStateSensor):
    """Building presence as it applies to this room; without building
    presence (0.13.3) the room's own motion sensor."""

    key = "presence"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["present", "absent", "unknown", "not_configured"]
    _attr_icon = "mdi:home-account"
    _attr_translation_key = "presence"

    @property
    def native_value(self):
        occupancy = self.runtime.zone_occupancy.get(self._zone.id)
        if occupancy is None:
            return "unknown"
        if occupancy.presence == "not_configured":
            return {"active": "present", "inactive": "absent"}.get(
                occupancy.motion, "unknown"
            )
        return occupancy.presence

    @property
    def extra_state_attributes(self):
        occupancy = self.runtime.zone_occupancy.get(self._zone.id)
        return {
            "motion": occupancy.motion if occupancy else None,
            "occupancy_mode": occupancy.mode if occupancy else None,
        }


class SafetySensor(ZoneStateSensor):
    """Safe while the room temperature can be trusted; else no regulation."""

    key = "safety"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["safe", "sensor_fault"]
    _attr_icon = "mdi:shield-check-outline"
    _attr_translation_key = "safety"

    @property
    def native_value(self):
        return self.runtime.safety[self._zone.id][0]

    @property
    def extra_state_attributes(self):
        return {"temperature_quality": self.runtime.safety[self._zone.id][1]}


class WindowSensor(ZoneStateSensor):
    """Open window: after the delay the room stops (unless bypassed)."""

    key = "window"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "closed",
        "opening",
        "open",
        "bypass",
        "not_configured",
        "unknown",
    ]
    _attr_icon = "mdi:window-open-variant"
    _attr_translation_key = "window"

    @property
    def native_value(self):
        return self.runtime.windows[self._zone.id]

    @property
    def extra_state_attributes(self):
        snapshot = self.runtime.zone_snapshots[self._zone.id]
        return {
            "open_contacts": sum(c.value == "open" for c in snapshot.contacts),
            "contacts": len(snapshot.contacts),
            "delay_seconds": self.runtime.building.settings.window_delay_seconds,
        }


class RegulatedSetpointSensor(ZoneStateSensor):
    """The setpoint Zonentra steers the indoor unit to (or would)."""

    key = "regulated_setpoint"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-auto"
    _attr_translation_key = "regulated_setpoint"

    def _plan(self):
        plans = self.runtime.setpoint_plans
        return next(
            (plans[item.id] for item in self._units() if item.id in plans), None
        )

    @property
    def native_value(self):
        plan = self._plan()
        return plan.setpoint if plan else None

    @property
    def extra_state_attributes(self):
        plan = self._plan()
        return {
            "status": plan.status if plan else None,
            "base": plan.base if plan else None,
            "offset": plan.offset if plan else None,
            "room_regulation": self.runtime.regulated(self._zone.id),
        }


class EmaTemperatureSensor(ZoneStateSensor):
    """Exponentially smoothed room temperature (5 min time constant)."""

    key = "ema_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 2
    _attr_translation_key = "ema_temperature"

    @property
    def native_value(self):
        trend = self.runtime.trends.get(self._zone.id)
        return trend.ema if trend else None


class TemperatureSlopeSensor(ZoneStateSensor):
    """How fast the room warms (+) or cools (-), in °C per hour."""

    key = "temperature_slope"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "°C/h"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:thermometer-chevron-up"
    _attr_translation_key = "temperature_slope"

    @property
    def native_value(self):
        trend = self.runtime.trends.get(self._zone.id)
        return trend.slope if trend else None


class AutoFanSensor(ZoneStateSensor):
    """Fan speed the automatic fan chooses now (0.13.3: always shown, also
    when the thermostat has another fan mode or in simulation)."""

    key = "auto_fan"
    _attr_icon = "mdi:fan-auto"
    _attr_translation_key = "auto_fan"

    @property
    def native_value(self):
        return self.runtime.auto_fans.get(self._zone.id)

    @property
    def extra_state_attributes(self):
        runtime = self.runtime
        return {
            # The thermostat runs Zonentra's automatic fan (fan mode auto).
            "in_use": runtime.zone_auto_fan(self._zone.id),
            "room_regulation": runtime.regulated(self._zone.id),
            "step": runtime.building.settings.auto_fan_step,
        }


class ScreenSensor(ZonentraDiagnosticSensor):
    """What Zonentra wants a screen to do (0.11), and why. Not the screen's
    own position (attribute ``current_position``): with Screen regulation off
    it is only advice (0.13.3: named so)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["open", "shade", "hold"]
    _attr_icon = "mdi:roller-shade"
    _attr_translation_key = "screen"
    _attr_entity_category = None

    def __init__(self, entry, item, unique_id=None, **shared):
        super().__init__(entry, **shared)
        self.item = item
        self._attr_translation_placeholders = {"screen": item.name}
        self._attr_unique_id = unique_id

    def _present(self, runtime) -> bool:
        return self.item.id in runtime.screen_decisions

    @property
    def native_value(self):
        return self.runtime.screen_decisions[self.item.id].target

    @property
    def extra_state_attributes(self):
        runtime = self.runtime
        decision = runtime.screen_decisions[self.item.id]
        move = runtime.screen_moves.get(self.item.id)
        zone = next(z for z in runtime.building.zones if z.id == self.item.zone_id)
        active = runtime.thermostat(zone.id).screen_regulation
        return {
            "simulation": not active,
            "screen_regulation": active,
            "reason": decision.reason,
            "target_position": decision.position,
            "current_position": cover_position(
                self.hass.states.get(self.item.entity_id)
            ),
            "status": move.status if move else None,
            "next_check": _iso(move.next_check) if move else None,
            "wind_status": runtime.wind.status,
            "wind_mode": runtime.wind_mode,
            "wind_speed_kmh": runtime.wind_speed,
            "wind_gust_kmh": runtime.wind_gust,
            "wind_speed_limit_kmh": runtime.building.settings.screen_wind_speed_limit,
            "wind_gust_limit_kmh": runtime.building.settings.screen_wind_gust_limit,
            "wind_open_required": runtime.wind.open_required,
            "wind_blocks_closing": runtime.wind.block_closing,
            "wind_calm_since": _iso(runtime.wind.memory.calm_since),
            "wind_release_at": _iso(runtime.wind.next_check),
            "rain_status": runtime.rain.status,
            "rain_open_required": runtime.rain.open_required,
            "rain_blocks_closing": runtime.rain.block_closing,
            "rain_dry_since": _iso(runtime.rain.memory.dry_since),
            "rain_release_at": _iso(runtime.rain.next_check),
            "facade": zone.facade,
            "sunny": runtime.sunny,
            "sun_power": runtime.sun_power,
            "day_type": runtime.day_type,
            "manual_until": _iso(runtime.screen_overrides.get(self.item.id)),
            "last_commands": [
                {key: entry[key] for key in ("at", "value", "reason", "result")}
                | {"confirmation": entry.get("confirmation")}
                for entry in runtime.command_log
                if entry["equipment_id"] == self.item.id
            ][-5:],
        }


class ForecastTemperatureSensor(ZonentraDiagnosticSensor):
    """Highest or lowest temperature expected in the coming hours (0.12)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = None

    def __init__(self, entry, kind):
        super().__init__(entry)
        self._kind = kind
        self._attr_translation_key = f"forecast_{kind}"
        self._attr_unique_id = f"{entry.entry_id}_forecast_{kind}"

    @property
    def native_value(self):
        forecast = self.runtime.forecast
        if self.runtime.day_type == "unknown" or forecast is None:
            return None
        return getattr(forecast, self._kind)

    @property
    def extra_state_attributes(self):
        forecast = self.runtime.forecast
        return {
            "hours": forecast.hours if forecast else 0,
            "fetched_at": _iso(forecast.fetched_at) if forecast else None,
            "error": self.runtime.forecast_error,
        }


class DayTypeSensor(ZonentraDiagnosticSensor):
    """Warm, neutral or cold day by the forecast; drives screens and margins."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["warm", "neutral", "cold"]
    _attr_icon = "mdi:weather-partly-cloudy"
    _attr_translation_key = "day_type"
    _attr_entity_category = None

    def __init__(self, entry):
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_day_type"

    @property
    def native_value(self):
        kind = self.runtime.day_type
        return None if kind == "unknown" else kind

    @property
    def extra_state_attributes(self):
        settings = self.runtime.building.settings
        return {
            "warm_from": settings.warm_day_temperature,
            "cold_to": settings.cold_day_temperature,
            "heating_start_margin": settings.forecast_margin
            if self.runtime.day_type == "warm"
            else 0.0,
            "cooling_start_margin": settings.forecast_margin
            if self.runtime.day_type == "cold"
            else 0.0,
        }


def _iso(value):
    return value.isoformat() if value else None


class UnitControlSensor(ZonentraDiagnosticSensor):
    """Planned unit action, with the runtime's room-control status and commands."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "turn_on_heat",
        "turn_on_cool",
        "turn_off",
        "correct_mode",
        "hold",
        "blocked",
    ]
    _attr_icon = "mdi:play-box-outline"
    _attr_translation_key = "unit_control"

    def __init__(self, entry, item, unique_id=None, **shared):
        super().__init__(entry, **shared)
        self.item = item
        self._attr_translation_placeholders = {"unit": item.name}
        self._attr_unique_id = (
            unique_id or f"{entry.entry_id}_equipment_{item.id}_control"
        )

    def _present(self, runtime) -> bool:
        return self.item.id in runtime.unit_intents

    @property
    def native_value(self):
        return self.runtime.unit_intents[self.item.id].action

    @property
    def extra_state_attributes(self):
        intent = self.runtime.unit_intents[self.item.id]
        return {
            "simulation": True,
            "read_only": True,
            "zone": next(
                (z.name for z in self.runtime.building.zones if z.id == intent.zone_id),
                None,
            ),
            "observed_mode": intent.observed_mode,
            "desired_mode": intent.desired_mode,
            "zone_demand": intent.zone_demand,
            "intended_action": intent.action,
            "steps": list(intent.steps),
            "reason": intent.reason,
            "blocked_reason": intent.blocked_reason,
            "blocked_until": _iso(intent.blocked_until),
            "manual_override_active": intent.manual_override_active,
            "override_remaining_seconds": intent.override_remaining_seconds,
            "override_expires_at": _iso(intent.override_expires_at),
            "seconds_since_transition": intent.seconds_since_transition,
            "group_coordination_phase": intent.coordination_phase,
            "availability": intent.availability,
            **self._room_regulation(),
            "evaluated_at": _iso(self.runtime.evaluated_at),
        }

    def _room_regulation(self) -> dict:
        """0.9: what room regulation does with this unit (or would do)."""
        runtime = self.runtime
        if not runtime.active_capable:
            return {}
        active = runtime.regulated(self.item.zone_id)
        control = runtime.room_controls.get(self.item.id)
        plan = runtime.setpoint_plans.get(self.item.id)
        return {
            "simulation": not active,
            "read_only": not active,
            "room_regulation": active,
            "room_regulation_status": control.status if control else None,
            "setpoint_status": plan.status if plan else None,
            "setpoint_target": plan.setpoint if plan else None,
            "setpoint_base": plan.base if plan else None,
            "setpoint_offset": plan.offset if plan else None,
            "last_commands": [
                {
                    key: entry[key]
                    for key in ("at", "service", "hvac_mode", "temperature", "reason")
                }
                | {"result": entry["result"], "confirmation": entry.get("confirmation")}
                for entry in runtime.command_log
                if entry["equipment_id"] == self.item.id
            ][-5:],
        }


class GroupCoordinationSensor(ZonentraDiagnosticSensor):
    """Observed coordination phase alongside active group-control results."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "idle",
        "preparing",
        "stopping",
        "waiting",
        "applying",
        "verifying",
        "completed",
        "blocked",
        "failed",
    ]
    _attr_icon = "mdi:swap-horizontal-circle-outline"
    _attr_translation_key = "group_coordination"

    def __init__(self, entry, group):
        super().__init__(entry)
        self._group = group  # Not "group": HA reserves that entity attribute.
        self._attr_translation_placeholders = {"group": group.name}
        self._attr_unique_id = f"{entry.entry_id}_group_{group.id}_coordination"

    @property
    def native_value(self):
        return self.runtime.coordinations[self._group.id].phase

    @property
    def extra_state_attributes(self):
        item = self.runtime.coordinations[self._group.id]
        names = {e.id: e.name for e in self.runtime.building.equipment}
        return {
            "simulation": True,
            "read_only": True,
            "desired_mode": item.desired_mode,
            "group_mode": item.group_mode,
            "group_mode_reason": item.group_mode_reason,
            "coordinated_mode": item.coordinated_mode,
            "derived_mode": item.derived_mode,
            "member_indoor_units": item.member_units,
            "available_indoor_units": item.available_units,
            "conflicts": list(item.conflicts),
            "switch_needed": item.switch_needed,
            "switch_from": item.switch_from,
            "switch_to": item.switch_to,
            "switch_allowed_in_simulation": item.switch_allowed_in_simulation,
            "active_switch_permitted": item.active_switch_permitted,
            "phase_reason": item.phase_reason,
            "waiting_until": _iso(item.waiting_until),
            "blockers": list(item.blockers),
            "planned_actions": {
                names.get(unit, unit): action for unit, action in item.planned_actions
            },
            # 0.7 active outdoor-stand control (central layout only).
            **self._mode_control(),
            "evaluated_at": _iso(self.runtime.evaluated_at),
        }

    def _mode_control(self) -> dict:
        runtime = self.runtime
        if not runtime.active_capable:
            return {"active_mode_control": False}
        control = runtime.mode_controls.get(self._group.id)
        memory = runtime.preset_memories.get(self._group.id)
        active = runtime.mode_active.get(self._group.id)
        return {
            # Honest labelling: this group's stand is really controlled.
            "read_only": not active,
            "active_mode_control": active,
            "mode_control_status": control.status if control else None,
            "mode_control_target": control.target_mode if control else None,
            "preset_mode": memory.mode if memory else None,
            "preset_changed_at": _iso(memory.changed_at) if memory else None,
            "mode_control_blocked_until": _iso(control.blocked_until)
            if control
            else None,
            "last_commands": [
                {
                    key: entry[key]
                    for key in (
                        "at",
                        "entity_id",
                        "service",
                        "hvac_mode",
                        "reason",
                        "result",
                    )
                }
                | {"confirmation": entry.get("confirmation")}
                for entry in runtime.command_log
                if entry["group_id"] == self._group.id
            ][-10:],
        }


class FloorHeatingSensor(ZonentraDiagnosticSensor):
    """Observed floor thermostat and the reason for floor/airco decisions."""

    _attr_icon = "mdi:heating-coil"
    _attr_translation_key = "floor_heating"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "disabled",
        "off",
        "stopping",
        "setting_target",
        "starting",
        "in_sync",
        "command_failed",
        "manual_override",
        "unavailable",
        "unsupported_entity",
        "unsupported_modes",
        "unsupported_setpoint",
        "target_out_of_range",
        "cannot_stop",
        "room_temperature_unknown",
    ]
    _attr_entity_category = None

    def __init__(self, entry, item, **shared):
        super().__init__(entry, **shared)
        self.item = item
        self._attr_unique_id = f"{entry.entry_id}_{item.id}_floor"
        self._attr_translation_placeholders = {"floor": item.name}

    def _present(self, runtime):
        return self.item.id in runtime.floor_plans

    @property
    def native_value(self):
        return self.runtime.floor_plans[self.item.id]["status"]

    @property
    def extra_state_attributes(self):
        runtime = self.runtime
        plan = runtime.floor_plans[self.item.id]
        state = self.hass.states.get(self.item.entity_id)
        observation = runtime.floor_observations.get(self.item.id)
        return {
            **{key: value for key, value in plan.items() if key != "command"},
            "target_temperature_unit": "°C",
            "current_target_temperature": state.attributes.get("temperature")
            if state
            else None,
            "current_target_temperature_unit": self.hass.config.units.temperature_unit,
            "selected_mode": observation.selected_mode if observation else None,
            "reported_action": observation.reported_action if observation else None,
            "floor_regulation": runtime.floor_regulated(self.item.zone_id),
            "manual_until": _iso(runtime.floor_overrides.get(self.item.id)),
            "last_commands": [
                {
                    key: record.get(key)
                    for key in (
                        "at",
                        "service",
                        "temperature",
                        "result",
                        "confirmation",
                    )
                }
                for record in runtime.command_log
                if record["equipment_id"] == self.item.id
            ][-5:],
        }


class HeatingProgressSensor(ZoneStateSensor):
    """Visible warning only: never automatically request additional gas heat."""

    key = "heating_progress"
    _attr_translation_key = "heating_progress"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "monitoring", "adequate", "insufficient", "unknown"]
    _attr_icon = "mdi:thermometer-alert"

    @property
    def native_value(self):
        progress = self.runtime.heating_progress.get(self._zone.id)
        return progress.status if progress else "idle"

    @property
    def extra_state_attributes(self):
        progress = self.runtime.heating_progress.get(self._zone.id)
        memory = progress.memory if progress else None
        return {
            "since": _iso(memory.since) if memory else None,
            "initial_temperature_c": memory.temperature if memory else None,
            "temperature_rise_c": progress.rise if progress else None,
            "next_check": _iso(progress.next_check) if progress else None,
            "observation_minutes": WATCH_MINUTES,
            "minimum_rise_c": MINIMUM_RISE,
            "minimum_deficit_c": MINIMUM_DEFICIT,
            "automatic_gas_boost": False,
        }
