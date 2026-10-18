"""Deterministic zone snapshots and hysteretic temperature demand."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .models import Building, Zone
from .observations import (
    Reading,
    SourceState,
    configured_limit,
    max_age_for,
    read,
)


@dataclass(frozen=True)
class ZoneSnapshot:
    zone_id: str
    temperature: Reading
    heating_limit: Reading
    cooling_limit: Reading
    outdoor_temperature: Reading
    occupancy: Reading
    contacts: tuple[Reading, ...]
    hvac_ids: tuple[str, ...]
    floor_heating_ids: tuple[str, ...]
    shading_ids: tuple[str, ...]


@dataclass(frozen=True)
class ZoneDemand:
    zone_id: str
    demand: str
    heating_score: float | None
    cooling_score: float | None
    reason: str
    sensor_health: str
    priority: float = 1.0


def collect_zone(
    building: Building,
    zone: Zone,
    sources: Mapping[str, SourceState],
    now: datetime,
    outdoor_temperature: Reading | None = None,
    band: str = "comfort",
    comfort: tuple[float, float] | None = None,
    bands: Mapping[str, float] | None = None,
) -> ZoneSnapshot:
    """Snapshot of one zone; ``band`` eco/away replaces the comfort limits.

    ``comfort`` is the room thermostat's own band (0.9); it replaces the
    configured comfort limits, never the central eco/away bands. ``bands`` are
    the room's own eco/away limits set on its device (0.10); they win over the
    zone configuration and the central values.
    """
    # Measurements follow the zone strategy; state_based applies no age limit.
    max_age = max_age_for(zone.sensor_validation, zone.max_age_seconds)

    def numeric(field, kind):
        entity = getattr(zone, field)
        if entity is None:
            return Reading(None, "missing", reason="not_configured")
        return read(sources.get(entity), kind, now, max_age)

    def equipment(kind):
        return tuple(
            item.id
            for item in building.equipment
            if item.zone_id == zone.id and item.kind == kind
        )

    if band in ("eco", "away"):  # Eco/away bands: the zone's, else central.
        settings = building.settings

        def band_limit(direction):
            key = f"{band}_{direction}_limit"
            value = (bands or {}).get(key)
            if value is None:
                value = getattr(zone, key)
            return configured_limit(
                value if value is not None else getattr(settings, key)
            )

        heating, cooling = band_limit("heating"), band_limit("cooling")
    else:
        heating, cooling = comfort_limits(zone, sources, now, comfort)
    return ZoneSnapshot(
        zone.id,
        numeric("temperature_entity", "temperature"),
        heating,
        cooling,
        outdoor_temperature
        if outdoor_temperature is not None
        else read(
            sources.get(building.settings.outdoor_temperature_entity),
            "temperature",
            now,
            building.settings.outdoor_max_age_seconds,
        ),
        read(sources.get(zone.occupancy_entity), "occupancy", now),
        tuple(
            read(sources.get(entity), "contact", now)
            for entity in zone.contact_entities
        ),
        equipment("hvac"),
        equipment("floor_heating"),
        equipment("shading"),
    )


def comfort_limits(
    zone: Zone,
    sources: Mapping[str, SourceState],
    now: datetime,
    comfort: tuple[float, float] | None = None,
) -> tuple[Reading, Reading]:
    """The room's comfort band: thermostat band, else the configured limits."""
    if comfort is not None:
        return configured_limit(comfort[0]), configured_limit(comfort[1])

    return configured_limit(zone.heating_limit), configured_limit(zone.cooling_limit)


def calculate_demand(
    zone: Zone,
    snapshot: ZoneSnapshot,
    previous: str = "unknown",
    heating_margin: float = 0.0,
    cooling_margin: float = 0.0,
) -> ZoneDemand:
    """A configured missing/stale/invalid required input produces unknown, not zero.

    The margins (0.12, from the weather forecast) delay the start of heating on
    a warm day and of cooling on a cold day; a running demand ends as usual.
    """
    required = (snapshot.temperature, snapshot.heating_limit, snapshot.cooling_limit)
    bad = next((reading for reading in required if reading.quality != "valid"), None)
    if bad:
        return ZoneDemand(
            zone.id, "unknown", None, None, "insufficient_data", bad.quality
        )
    temperature = snapshot.temperature.value
    heat, cool, hysteresis = (
        snapshot.heating_limit.value,
        snapshot.cooling_limit.value,
        zone.hysteresis,
    )
    if heat + 2 * hysteresis >= cool:
        return ZoneDemand(
            zone.id, "unknown", None, None, "invalid_limits", "unreliable"
        )
    if previous == "heating_demand" and temperature < heat + hysteresis:
        demand, reason = "heating_demand", "heating_hysteresis"
    elif previous == "cooling_demand" and temperature > cool - hysteresis:
        demand, reason = "cooling_demand", "cooling_hysteresis"
    elif temperature < heat - hysteresis - heating_margin:
        demand, reason = "heating_demand", "below_heating_limit"
    elif temperature > cool + hysteresis + cooling_margin:
        demand, reason = "cooling_demand", "above_cooling_limit"
    elif temperature < heat - hysteresis:
        demand, reason = "neutral", "heating_deferred_warm_day"
    elif temperature > cool + hysteresis:
        demand, reason = "neutral", "cooling_deferred_cold_day"
    else:
        demand, reason = "neutral", "within_comfort_band"
    # Saturate each zone's contribution at 5 K before weighting.
    heating_score = (
        zone.weight * min(5, max(0, heat - temperature))
        if demand == "heating_demand"
        else 0.0
    )
    cooling_score = (
        zone.weight * min(5, max(0, temperature - cool))
        if demand == "cooling_demand"
        else 0.0
    )
    return ZoneDemand(
        zone.id,
        demand,
        round(heating_score, 4),
        round(cooling_score, 4),
        reason,
        "valid",
    )
