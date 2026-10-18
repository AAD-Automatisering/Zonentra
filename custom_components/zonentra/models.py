"""Immutable building topology and additive 0.1-compatible configuration."""

import math
import re
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any

from .const import KINDS
from .migration import (
    BAND_FIELDS,
    FLOOR_FIELDS,
    LIMIT_FIELDS,
    PARAMETER_FIELDS,
    PRECONDITIONING_FIELDS,
    migrate_options,
)
from .observations import STATE_BASED, UPDATE_BASED, VALIDATION_STRATEGIES
from .presence import PRESENCE_DOMAINS
from .shading import FACADES

ZONE_ENTITY_FIELDS = (
    "temperature_entity",
    "occupancy_entity",
    "calendar_entity",
    "humidity_entity",
    "co2_entity",
    "vane_entity",
)
VANE_DOMAINS = (
    "select",
    "input_select",
    "number",
    "input_number",
    "switch",
    "input_boolean",
)


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    temperature_entity: str | None = None
    occupancy_entity: str | None = None
    calendar_entity: str | None = None
    floor_heating_limit: float = 16.0
    preconditioning_max_minutes: float = 180.0
    preconditioning_buffer_minutes: float = 10.0
    preconditioning_heating_rate: float = 0.05  # °C/min baseline before learning
    preconditioning_cooling_rate: float = 0.05
    heating_limit: float | None = None
    cooling_limit: float | None = None
    humidity_entity: str | None = None
    co2_entity: str | None = None
    contact_entities: tuple[str, ...] = ()
    group_id: str | None = None
    weight: float = 1.0
    hysteresis: float = 0.3
    # Only enforced with update_based; state_based never ages out a valid value.
    max_age_seconds: float = 1800.0
    sensor_validation: str = STATE_BASED
    use_building_limits: bool = False
    use_building_parameters: bool = False
    use_building_preconditioning: bool = True
    use_building_floor_heating: bool = True
    # 0.10: own eco/away bands when the zone does not use the central limits;
    # None takes the central value.
    eco_heating_limit: float | None = None
    eco_cooling_limit: float | None = None
    away_heating_limit: float | None = None
    away_cooling_limit: float | None = None
    # 0.10 vane: position for cooling (up) and heating (down) of the room's
    # indoor units, set through a separate entity (select or number).
    vane_entity: str | None = None
    vane_cool_value: str | None = None
    vane_heat_value: str | None = None
    # 0.11: facade of the room's screens (north ... northwest).
    facade: str | None = None


@dataclass(frozen=True)
class EquipmentGroup:
    id: str
    name: str
    single_hvac_mode: bool = True
    mode_entity: str | None = None
    min_switch_seconds: float = 900.0
    min_demand_score: float = 0.5
    score_deadband: float = 0.5
    max_age_seconds: float = 1800.0


@dataclass(frozen=True)
class Equipment:
    id: str
    name: str
    kind: str
    entity_id: str
    zone_id: str
    group_id: str | None = None


@dataclass(frozen=True)
class BuildingSettings:
    floor_heating_limit: float = 16.0
    preconditioning_max_minutes: float = 180.0
    preconditioning_buffer_minutes: float = 10.0
    preconditioning_heating_rate: float = 0.05
    preconditioning_cooling_rate: float = 0.05
    outdoor_temperature_entity: str | None = None
    outdoor_max_age_seconds: float = 1800.0
    heating_limit: float | None = None
    cooling_limit: float | None = None
    hysteresis: float = 0.3
    max_age_seconds: float = 1800.0
    sensor_validation: str = STATE_BASED
    outdoor_temperature_candidates: tuple[str, ...] = ()
    # Building-wide manual override and HVAC timing settings.
    manual_override_enabled: bool = True
    manual_override_duration_minutes: float = 30.0
    min_on_seconds: float = 600.0  # comfort: avoid short runs
    min_off_seconds: float = 180.0  # safety: restart protection (verify per make)
    changeover_wait_seconds: float = 300.0  # safety: stop -> other mode delay
    # 0.8 presence and motion (like Versatile Thermostat).
    presence_entity: str | None = None
    office_schedule_entity: str | None = None
    eco_heating_limit: float = 19.0
    eco_cooling_limit: float = 26.0
    away_heating_limit: float = 16.0
    away_cooling_limit: float = 30.0
    motion_delay_seconds: float = 30.0
    motion_off_delay_minutes: float = 30.0
    # 0.9 room regulation: self-regulating setpoint of running indoor units.
    setpoint_max_offset: float = 2.0
    setpoint_interval_seconds: float = 300.0
    # 0.10 open window: the room stops after this delay (like VTherm).
    window_delay_seconds: float = 30.0
    # 0.13.1 automatic fan: °C of distance to the target per fan speed step.
    auto_fan_step: float = 0.5
    # 0.11 screens: solar power says whether the sun shines; sun.sun where.
    sun_power_entity: str | None = None
    sun_power_threshold: float = 1000.0  # W
    sun_entity: str = "sun.sun"
    screen_rain_protection: bool = True
    screen_rain_entity: str | None = None
    screen_rain_dry_minutes: float = 30.0
    screen_rain_max_age_minutes: float = 60.0
    screen_wind_protection: bool = True
    screen_wind_speed_limit: float | None = None
    screen_wind_gust_limit: float | None = None
    screen_wind_calm_minutes: float = 30.0
    screen_wind_max_age_minutes: float = 60.0
    screen_block_entity: str | None = None  # configured: only valid off allows moves
    screen_shade_position: float = 0.0  # % (100 = open)
    screen_override_minutes: float = 120.0
    screen_min_move_minutes: float = 30.0
    screen_open_delay_minutes: float = 15.0
    screen_shade_delay_minutes: float = 5.0
    # 0.12 weather forecast: the coming hours make a day warm/neutral/cold.
    weather_entity: str | None = None
    warm_day_temperature: float = 22.0  # expected maximum from (°C)
    cold_day_temperature: float = 12.0  # expected maximum up to (°C)
    forecast_margin: float = 1.0  # °C later start of heating/cooling


def effective_zone(zone: Zone, settings: BuildingSettings) -> Zone:
    values = {}
    own_limits = any(getattr(zone, key) is not None for key in LIMIT_FIELDS)
    if zone.use_building_limits or not own_limits:
        # Static limits are set on the device; rooms without a local band
        # inherit the central limits.
        values.update({key: getattr(settings, key) for key in LIMIT_FIELDS})
    values.update(
        {
            key: getattr(settings, key)
            for key in BAND_FIELDS
            if zone.use_building_limits or getattr(zone, key) is None
        }
    )
    if zone.use_building_parameters:
        values.update({key: getattr(settings, key) for key in PARAMETER_FIELDS})
    if zone.use_building_preconditioning:
        values.update({key: getattr(settings, key) for key in PRECONDITIONING_FIELDS})
    if zone.use_building_floor_heating:
        values.update({key: getattr(settings, key) for key in FLOOR_FIELDS})
    return replace(zone, **values)


@dataclass(frozen=True)
class Building:
    id: str
    name: str
    zones: tuple[Zone, ...]
    groups: tuple[EquipmentGroup, ...]
    equipment: tuple[Equipment, ...]
    settings: BuildingSettings = dataclass_field(default_factory=BuildingSettings)

    @property
    def entity_ids(self) -> frozenset[str]:
        """Return configured inputs only, including optional future context."""
        return frozenset(
            [item.entity_id for item in self.equipment]
            + [
                entity
                for zone in self.zones
                for field in ZONE_ENTITY_FIELDS
                if (entity := getattr(zone, field))
            ]
            + [entity for zone in self.zones for entity in zone.contact_entities]
            + [group.mode_entity for group in self.groups if group.mode_entity]
            + [
                entity
                for key in (
                    "outdoor_temperature_entity",
                    "presence_entity",
                    "office_schedule_entity",
                    "sun_power_entity",
                    "screen_block_entity",
                    "screen_rain_entity",
                )
                if (entity := getattr(self.settings, key))
            ]
            # The sun position only matters with screens (0.11).
            + (
                [self.settings.sun_entity]
                + (
                    [self.settings.weather_entity]
                    if self.settings.weather_entity
                    and (
                        self.settings.screen_rain_protection
                        or self.settings.screen_wind_protection
                    )
                    else []
                )
                if any(item.kind == "shading" for item in self.equipment)
                else []
            )
        )


def _records(records: list[dict[str, Any]]) -> None:
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_id")
    for record in records:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", record["id"]):
            raise ValueError("invalid_id")
        if not record["name"].strip():
            raise ValueError("invalid_name")


def _entity(value: str | None, domains: tuple[str, ...] | None = None) -> None:
    if value is None:
        return
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value):
        raise ValueError("invalid_entity")
    if value.startswith("zonentra.") or (
        domains and value.split(".")[0] not in domains
    ):
        raise ValueError("invalid_entity")


def _range(value, low, high) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("invalid_parameters")


def build_model(building_id: str, name: str, topology: dict[str, Any]) -> Building:
    """Validate references, parameters and exclusive physical equipment ownership."""
    if not name.strip():
        raise ValueError("invalid_name")
    topology = migrate_options(topology)
    settings_data = topology.get("building", {})
    settings = BuildingSettings(
        **{
            **settings_data,
            "outdoor_temperature_candidates": tuple(
                settings_data.get("outdoor_temperature_candidates", ())
            ),
        }
    )
    _entity(settings.outdoor_temperature_entity, ("sensor",))
    _range(settings.hysteresis, 0.1, 2)
    _range(settings.max_age_seconds, 0, 86400)
    _range(settings.outdoor_max_age_seconds, 0, 86400)
    if not isinstance(settings.manual_override_enabled, bool):
        raise ValueError("invalid_parameters")
    _range(settings.manual_override_duration_minutes, 5, 120)
    for key in ("min_on_seconds", "min_off_seconds", "changeover_wait_seconds"):
        _range(getattr(settings, key), 0, 3600)
    _entity(settings.presence_entity, PRESENCE_DOMAINS)
    _entity(settings.office_schedule_entity, ("schedule",))
    _validate_bands(settings, settings.hysteresis)
    _range(settings.motion_delay_seconds, 0, 600)
    _range(settings.motion_off_delay_minutes, 1, 240)
    _range(settings.setpoint_max_offset, 0, 5)
    _range(settings.setpoint_interval_seconds, 60, 3600)
    _range(settings.window_delay_seconds, 0, 3600)
    _range(settings.auto_fan_step, 0.2, 2)
    _entity(settings.sun_power_entity, ("sensor",))
    _entity(settings.sun_entity, ("sun",))
    _entity(settings.screen_block_entity, ("binary_sensor", "input_boolean"))
    if not isinstance(settings.screen_rain_protection, bool):
        raise ValueError("invalid_parameters")
    _entity(settings.screen_rain_entity, ("weather", "binary_sensor"))
    _range(settings.screen_rain_dry_minutes, 5, 240)
    _range(settings.screen_rain_max_age_minutes, 5, 180)
    if not isinstance(settings.screen_wind_protection, bool):
        raise ValueError("invalid_parameters")
    for key in ("screen_wind_speed_limit", "screen_wind_gust_limit"):
        if getattr(settings, key) is not None:
            _range(getattr(settings, key), 1, 200)
    _range(settings.screen_wind_calm_minutes, 5, 240)
    _range(settings.screen_wind_max_age_minutes, 5, 180)
    _range(settings.sun_power_threshold, 0, 1_000_000)
    _range(settings.screen_shade_position, 0, 100)
    _range(settings.screen_override_minutes, 5, 720)
    _range(settings.screen_min_move_minutes, 15, 240)
    _range(settings.screen_open_delay_minutes, 0, 60)
    _range(settings.screen_shade_delay_minutes, 0, 60)
    _entity(settings.weather_entity, ("weather",))
    _range(settings.warm_day_temperature, -20, 45)
    _range(settings.cold_day_temperature, -20, 45)
    _range(settings.forecast_margin, 0, 3)
    if settings.cold_day_temperature >= settings.warm_day_temperature:
        raise ValueError("invalid_parameters")
    _validate_sensor_validation(settings)
    _validate_limits(settings)
    _validate_preconditioning(settings)
    _validate_floor_heating(settings)
    zones, groups, equipment = (
        topology.get(key, []) for key in ("zones", "groups", "equipment")
    )
    for records in (zones, groups, equipment):
        _records(records)
    zone_models = tuple(
        Zone(**{**item, "contact_entities": tuple(item.get("contact_entities", ()))})
        for item in zones
    )
    for zone in zone_models:
        if any(
            not isinstance(getattr(zone, flag), bool)
            for flag in (
                "use_building_limits",
                "use_building_parameters",
                "use_building_preconditioning",
                "use_building_floor_heating",
            )
        ):
            raise ValueError("invalid_parameters")
    zone_models = tuple(effective_zone(zone, settings) for zone in zone_models)
    group_models = tuple(EquipmentGroup(**item) for item in groups)
    zone_by_id = {zone.id: zone for zone in zone_models}
    group_ids = {group.id for group in group_models}
    for zone in zone_models:
        for field in ZONE_ENTITY_FIELDS:
            domains = (
                ("calendar",)
                if field == "calendar_entity"
                else VANE_DOMAINS
                if field == "vane_entity"
                else ("binary_sensor",)
                if field == "occupancy_entity"
                else ("sensor",)
            )
            _entity(getattr(zone, field), domains)
        for entity in zone.contact_entities:
            _entity(entity, ("binary_sensor",))
        if zone.group_id and zone.group_id not in group_ids:
            raise ValueError("invalid_reference")
        _validate_preconditioning(zone)
        _validate_floor_heating(zone)
        _range(zone.weight, 0.1, 10)
        _range(zone.hysteresis, 0.1, 2)
        _range(zone.max_age_seconds, 0, 86400)
        _validate_sensor_validation(zone)
        _validate_limits(zone)
        _validate_bands(zone, zone.hysteresis)
        if zone.facade is not None and zone.facade not in FACADES:
            raise ValueError("invalid_parameters")
    for group in group_models:
        _entity(group.mode_entity, ("sensor", "select", "climate"))
        _range(group.min_switch_seconds, 0, 86400)
        _range(group.min_demand_score, 0.1, 100)
        _range(group.score_deadband, 0.1, 100)
        _range(group.max_age_seconds, 0, 86400)
    physical_entities: set[str] = set()
    domains = {
        "hvac": ("climate",),
        "floor_heating": ("climate", "number"),
        "shading": ("cover",),
        "ventilation": ("fan", "switch"),
    }
    equipment_models = []
    for item in equipment:
        if item["kind"] not in KINDS:
            raise ValueError("invalid_kind")
        if item["zone_id"] not in zone_by_id or (
            item.get("group_id") and item["group_id"] not in group_ids
        ):
            raise ValueError("invalid_reference")
        if item.get("group_id") and item["kind"] != "hvac":
            raise ValueError("invalid_group")
        zone_group = (
            zone_by_id[item["zone_id"]].group_id if item["kind"] == "hvac" else None
        )
        if zone_group and item.get("group_id") and zone_group != item["group_id"]:
            raise ValueError("conflicting_group")
        _entity(item["entity_id"], domains.get(item["kind"]))
        if item["entity_id"] in physical_entities:
            raise ValueError("duplicate_equipment_entity")
        physical_entities.add(item["entity_id"])
        equipment_models.append(
            Equipment(**{**item, "group_id": item.get("group_id") or zone_group})
        )
    return Building(
        building_id, name, zone_models, group_models, tuple(equipment_models), settings
    )


def _validate_floor_heating(config) -> None:
    _range(config.floor_heating_limit, 5, 25)


def _validate_preconditioning(config) -> None:
    _range(config.preconditioning_max_minutes, 0, 360)
    _range(config.preconditioning_buffer_minutes, 0, 60)
    _range(config.preconditioning_heating_rate, 0.005, 0.5)
    _range(config.preconditioning_cooling_rate, 0.005, 0.5)


def _validate_bands(config, hysteresis) -> None:
    for band in ("eco", "away"):
        heat = getattr(config, f"{band}_heating_limit")
        cool = getattr(config, f"{band}_cooling_limit")
        _range(heat, 5, 35)
        _range(cool, 5, 35)
        if heat + 2 * hysteresis >= cool:
            raise ValueError("invalid_limits")


def _validate_sensor_validation(config) -> None:
    if config.sensor_validation not in VALIDATION_STRATEGIES:
        raise ValueError("invalid_parameters")
    if config.sensor_validation == UPDATE_BASED and not config.max_age_seconds:
        raise ValueError("invalid_parameters")  # update_based needs a maximum age


def _validate_limits(config) -> None:
    for limit in (config.heating_limit, config.cooling_limit):
        if limit is not None:
            _range(limit, 5, 35)
    heat = config.heating_limit is not None
    cool = config.cooling_limit is not None
    if heat != cool:
        raise ValueError("invalid_limits")
    if (
        config.heating_limit is not None
        and config.cooling_limit is not None
        and config.heating_limit + 2 * config.hysteresis >= config.cooling_limit
    ):
        raise ValueError("invalid_limits")
