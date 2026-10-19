"""Privacy-conscious diagnostics, with no raw source states."""

from dataclasses import asdict, fields

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntryState

from . import ZonentraConfigEntry, central_entry, entry_type
from .bookings import (
    MAX_SAMPLE_MINUTES,
    MIN_LEARNED_SAMPLES,
    MIN_SAMPLE_MINUTES,
    ThermalRates,
    outdoor_band,
    rate_quality,
)
from .models import ZONE_ENTITY_FIELDS, BuildingSettings
from .observations import describe
from .topology import EQUIPMENT_FIELDS, ZONE

REDACT = {
    "name",
    "id",
    "entity_id",
    "zone_id",
    "group_id",
    "equipment_id",
    "temperature_entity",
    "controller_entity",
    "occupancy_entity",
    "mode_entity",
    "outdoor_temperature_entity",
    "outdoor_temperature_candidates",
    "contact_entities",
    "member_zone_ids",
    *ZONE_ENTITY_FIELDS,
    *(
        field.name
        for field in fields(BuildingSettings)
        if field.name.endswith(("_entity", "_entities", "_candidates"))
    ),
    *(field for field, _ in EQUIPMENT_FIELDS),
}
# Indoor observations export interpretation only; raw temperatures and fan modes
# are omitted, and the equipment_id is redacted by the enclosing export.
UNIT_FIELDS = (
    "equipment_id",
    "availability",
    "selected_mode",
    "reported_action",
    "quality",
    "mismatch",
)
SOURCE_FIELDS = (
    "temperature",
    "heating_limit",
    "cooling_limit",
    "outdoor_temperature",
)


def _iso(value):
    return value.isoformat() if value else None


def _learning_context(runtime, zone):
    """Identical learning evidence for office rooms and calendar rooms."""
    band = outdoor_band(runtime._booking_outdoor_temperature())
    profiles = runtime.thermal_rates.get(zone.id, {})
    current = profiles.get(band, ThermalRates())
    return {
        "uses_central_settings": zone.use_building_preconditioning,
        "preconditioning_settings": {
            "heating_rate_c_per_minute": zone.preconditioning_heating_rate,
            "cooling_rate_c_per_minute": zone.preconditioning_cooling_rate,
            "buffer_minutes": zone.preconditioning_buffer_minutes,
            "max_minutes": zone.preconditioning_max_minutes,
        },
        "learning": {
            **runtime.thermal_learning.get(zone.id, {}),
            "outdoor_band": band,
            "minimum_samples": MIN_LEARNED_SAMPLES,
            "minimum_sample_minutes": MIN_SAMPLE_MINUTES,
            "maximum_sample_minutes": MAX_SAMPLE_MINUTES,
            "profile_quality": {
                mode: rate_quality(mode, current, runtime.evaluated_at)
                for mode in ("heating", "cooling")
            },
        },
        "learned_profiles": {
            band: {
                **asdict(rates),
                "quality": {
                    mode: rate_quality(mode, rates, runtime.evaluated_at)
                    for mode in ("heating", "cooling")
                },
            }
            for band, rates in profiles.items()
        },
    }


def _source_validation(runtime) -> list[dict]:
    """Per-zone validation explanations without values, entity IDs or names."""
    return [
        {
            "zone_id": zone_id,
            **{
                field: {
                    key: value
                    for key, value in describe(
                        getattr(snapshot, field), runtime.evaluated_at
                    ).items()
                    if key != "value"
                }
                for field in SOURCE_FIELDS
            },
        }
        for zone_id, snapshot in runtime.zone_snapshots.items()
    ]


def _room_context(runtime, zone_ids):
    """Explain screen and booking decisions without raw sensor/calendar content."""
    return {
        "heating_progress": [
            {
                "zone_id": zone_id,
                "status": progress.status,
                "since": _iso(progress.memory.since) if progress.memory else None,
                "temperature_rise_c": progress.rise,
                "next_check": _iso(progress.next_check),
            }
            for zone_id, progress in runtime.heating_progress.items()
            if zone_id in zone_ids
        ],
        "floor_heating": [
            {
                "equipment_id": item.id,
                "zone_id": item.zone_id,
                **{
                    key: value
                    for key, value in runtime.floor_plans.get(item.id, {}).items()
                    if key != "command"
                },
                "manual_until": _iso(runtime.floor_overrides.get(item.id)),
            }
            for item in runtime.building.equipment
            if item.kind == "floor_heating" and item.zone_id in zone_ids
        ],
        "screen_wind": {
            "status": runtime.wind.status,
            "mode": runtime.wind_mode,
            "entity_id": runtime.building.settings.weather_entity,
            "speed_kmh": runtime.wind_speed,
            "gust_kmh": runtime.wind_gust,
            "speed_limit_kmh": runtime.building.settings.screen_wind_speed_limit,
            "gust_limit_kmh": runtime.building.settings.screen_wind_gust_limit,
            "open_required": runtime.wind.open_required,
            "closing_blocked": runtime.wind.block_closing,
            "calm_since": _iso(runtime.wind.memory.calm_since),
            "next_check": _iso(runtime.wind.next_check),
            "calm_minutes": runtime.building.settings.screen_wind_calm_minutes,
        },
        "screen_rain": {
            "status": runtime.rain.status,
            "entity_id": runtime.rain_source,
            "open_required": runtime.rain.open_required,
            "closing_blocked": runtime.rain.block_closing,
            "dry_since": _iso(runtime.rain.memory.dry_since),
            "next_check": _iso(runtime.rain.next_check),
            "dry_minutes": runtime.building.settings.screen_rain_dry_minutes,
            "max_age_minutes": runtime.building.settings.screen_rain_max_age_minutes,
        },
        "screen_timing": {
            "minimum_move_minutes": runtime.building.settings.screen_min_move_minutes,
            "open_delay_minutes": runtime.building.settings.screen_open_delay_minutes,
            "shade_delay_minutes": runtime.building.settings.screen_shade_delay_minutes,
        },
        "screens": [
            {
                "equipment_id": item.id,
                "zone_id": item.zone_id,
                "decision": asdict(runtime.screen_decisions[item.id])
                if item.id in runtime.screen_decisions
                else None,
                "movement": {**asdict(move), "next_check": _iso(move.next_check)}
                if (move := runtime.screen_moves.get(item.id))
                else None,
                "target_since": _iso(memory.since)
                if (memory := runtime.screen_targets.get(item.id))
                else None,
                "last_move": _iso(runtime.screen_last_move.get(item.id)),
                "manual_until": _iso(runtime.screen_overrides.get(item.id)),
                "command_log": [
                    entry
                    for entry in runtime.command_log
                    if entry["equipment_id"] == item.id
                ][-5:],
            }
            for item in runtime.building.equipment
            if item.kind == "shading" and item.zone_id in zone_ids
        ],
        "office_schedule": [
            {
                "zone_id": zone.id,
                "plan": {
                    **asdict(plan),
                    "start": _iso(plan.start),
                    "end": _iso(plan.end),
                    "prepare_at": _iso(plan.prepare_at),
                    "next_check": _iso(plan.next_check),
                },
                "active_band": runtime.zone_bands.get(zone.id),
                **_learning_context(runtime, zone),
            }
            for zone in runtime.building.zones
            if zone.id in zone_ids
            and (plan := runtime.office_plans.get(zone.id))
            and plan.status != "not_configured"
        ],
        "bookings": [
            {
                "zone_id": zone.id,
                "plan": {
                    **asdict(plan),
                    "start": _iso(plan.start),
                    "end": _iso(plan.end),
                    "prepare_at": _iso(plan.prepare_at),
                    "next_check": _iso(plan.next_check),
                }
                if (plan := runtime.booking_plans.get(zone.id))
                else None,
                "calendar_error": runtime.booking_errors.get(zone.calendar_entity),
                "calendar_updated": _iso(
                    runtime.booking_updated.get(zone.calendar_entity)
                ),
                "active_band": runtime.zone_bands.get(zone.id),
                "priority": runtime.zone_demands[zone.id].priority,
                "last_result": runtime.booking_results.get(zone.id),
                **_learning_context(runtime, zone),
            }
            for zone in runtime.building.zones
            if zone.id in zone_ids and zone.calendar_entity
        ],
    }


async def async_get_config_entry_diagnostics(hass, entry: ZonentraConfigEntry) -> dict:
    if entry_type(entry) == ZONE:
        return _zone_diagnostics(hass, entry)
    runtime = entry.runtime_data
    return async_redact_data(
        {
            "configuration": {"data": dict(entry.data), "options": dict(entry.options)},
            "status": entry.runtime_data.status,
            "summary": {
                key: value
                for key, value in entry.runtime_data.summary().items()
                if key != "outdoor_temperature_c"
            },
            "zone_demands": [
                asdict(item) for item in entry.runtime_data.zone_demands.values()
            ],
            "source_validation": _source_validation(entry.runtime_data),
            **_room_context(runtime, {zone.id for zone in runtime.building.zones}),
            "group_observations": [
                {
                    **asdict(observation),
                    "desired_mode": runtime.group_decisions[group_id].desired_mode,
                    "physical_mode": runtime.group_decisions[group_id].current_mode,
                    "physical_mode_feedback_available": (
                        runtime.group_decisions[group_id].mode_quality == "valid"
                    ),
                    "observation_reason": runtime.mode_comparisons[group_id],
                    "units": [
                        {
                            key: value
                            for key, value in asdict(unit).items()
                            if key in UNIT_FIELDS
                        }
                        for unit in runtime.indoor_units.values()
                        if unit.group_id == group_id
                    ],
                }
                for group_id, observation in runtime.group_observations.items()
            ],
            "mode_control": {
                "active_capable": runtime.active_capable,
                "groups": [
                    {
                        "group_id": group_id,
                        "active": runtime.mode_active.get(group_id),
                        "status": control.status,
                        "target_mode": control.target_mode,
                        "preset_mode": control.preset_mode,
                        "blocked_until": _iso(control.blocked_until),
                    }
                    for group_id, control in runtime.mode_controls.items()
                ],
                "command_log": list(runtime.command_log),
            },
            "control_simulation": {
                "simulation": True,
                "coordinations": [
                    {
                        **{
                            key: value
                            for key, value in asdict(item).items()
                            if key not in ("planned_actions", "waiting_until")
                        },
                        "waiting_until": _iso(item.waiting_until),
                        "planned_actions": [
                            action for _, action in item.planned_actions
                        ],
                    }
                    for item in runtime.coordinations.values()
                ],
                "unit_intents": [
                    {
                        **{
                            key: value
                            for key, value in asdict(intent).items()
                            if key not in ("blocked_until", "override_expires_at")
                        },
                        "blocked_until": _iso(intent.blocked_until),
                        "override_expires_at": _iso(intent.override_expires_at),
                    }
                    for intent in runtime.unit_intents.values()
                ],
            },
            "group_decisions": [
                {
                    **asdict(item),
                    "blocked_until": item.blocked_until.isoformat()
                    if item.blocked_until
                    else None,
                }
                for item in entry.runtime_data.group_decisions.values()
            ],
        },
        REDACT,
    )


def _zone_diagnostics(hass, entry) -> dict:
    """A zone entry explains its observations, plans and enabled room control."""
    central = central_entry(hass)
    runtime = (
        central.runtime_data
        if central and central.state is ConfigEntryState.LOADED
        else None
    )
    zone_id = entry.options.get("id")
    data = {
        "entry_type": ZONE,
        "configuration": {"options": dict(entry.options)},
        "central_loaded": runtime is not None,
    }
    if runtime and zone_id in runtime.zone_demands:
        data["zone_demand"] = asdict(runtime.zone_demands[zone_id])
        data["unit_intents"] = [
            {
                **{
                    key: value
                    for key, value in asdict(intent).items()
                    if key not in ("blocked_until", "override_expires_at")
                },
                "blocked_until": _iso(intent.blocked_until),
                "override_expires_at": _iso(intent.override_expires_at),
            }
            for intent in runtime.unit_intents.values()
            if intent.zone_id == zone_id
        ]
        # 0.9 room thermostat and what room regulation does per unit.
        data["thermostat"] = runtime.thermostat(zone_id).as_dict()
        data["active_band"] = list(runtime.zone_bands.get(zone_id, ()))
        data.update(_room_context(runtime, {zone_id}))
        data["room_regulation"] = [
            {
                "equipment_id": item.id,
                "status": control.status,
                "setpoint": {
                    **asdict(plan),
                    "next_check": _iso(plan.next_check),
                }
                if (plan := runtime.setpoint_plans.get(item.id))
                else None,
            }
            for item in runtime.building.equipment
            if item.zone_id == zone_id
            and (control := runtime.room_controls.get(item.id))
        ]
    return async_redact_data(data, REDACT)
