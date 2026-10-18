"""UI setup and transactional topology editing for one building."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from . import central_entry
from . import zone_entries as config_zone_entries
from .const import DOMAIN, KINDS, TOPOLOGY_KEYS
from .migration import (
    BAND_FIELDS,
    DEVICE_LIMITS,
    FLOOR_FIELDS,
    LIMIT_FIELDS,
    NEW_BUILDING_DEFAULTS,
    PARAMETER_FIELDS,
    PRECONDITIONING_FIELDS,
    migrate_options,
)
from .models import (
    VANE_DOMAINS,
    ZONE_ENTITY_FIELDS,
    BuildingSettings,
    EquipmentGroup,
    Zone,
    build_model,
)
from .observations import VALIDATION_STRATEGIES
from .presence import PRESENCE_DOMAINS
from .shading import FACADES
from .topology import (
    CENTRAL,
    CENTRAL_UNIQUE_ID,
    ENTRY_TYPE,
    EQUIPMENT_FIELDS,
    ZONE,
    assemble,
)


def entity_selector(domain=None):
    config = {"filter": {"domain": domain}} if domain else {}
    return selector.EntitySelector(config)


def numeric(low, high, step=0.1, unit=None):
    config = {"min": low, "max": high, "step": step, "mode": "box"}
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def choice(options):
    return selector.SelectSelector({"options": options, "mode": "dropdown"})


def floor_fields() -> dict:
    return {
        vol.Optional("floor_heating_limit"): numeric(5, 25, unit="°C"),
    }


def preconditioning_fields() -> dict:
    return {
        vol.Optional("preconditioning_max_minutes"): numeric(0, 360, 1, "min"),
        vol.Optional("preconditioning_buffer_minutes"): numeric(0, 60, 1, "min"),
        vol.Optional("preconditioning_heating_rate"): numeric(
            0.005, 0.5, 0.005, "°C/min"
        ),
        vol.Optional("preconditioning_cooling_rate"): numeric(
            0.005, 0.5, 0.005, "°C/min"
        ),
    }


def building_fields(limits: bool = True) -> dict:
    """Central building settings, shared by the central and legacy flows.

    The central configuration (0.10) sets its static limits on its device;
    only the legacy entry keeps them in the form (``limits``).
    """
    fields = {
        **preconditioning_fields(),
        **floor_fields(),
        vol.Optional("outdoor_temperature_entity"): entity_selector("sensor"),
        vol.Optional("outdoor_max_age_seconds"): numeric(0, 86400, 1, "s"),
        vol.Optional("heating_limit"): numeric(5, 35, unit="°C"),
        vol.Optional("cooling_limit"): numeric(5, 35, unit="°C"),
        vol.Optional("hysteresis"): numeric(0.1, 2, unit="°C"),
        vol.Optional("sensor_validation"): choice(list(VALIDATION_STRATEGIES)),
        vol.Optional("max_age_seconds"): numeric(0, 86400, 1, "s"),
        # Building-wide manual overrides and timing for planning and control.
        vol.Optional("manual_override_enabled"): selector.BooleanSelector(),
        vol.Optional("manual_override_duration_minutes"): numeric(5, 120, 1, "min"),
        vol.Optional("min_on_seconds"): numeric(0, 3600, 1, "s"),
        vol.Optional("min_off_seconds"): numeric(0, 3600, 1, "s"),
        vol.Optional("changeover_wait_seconds"): numeric(0, 3600, 1, "s"),
        # Presence and motion (0.8), like Versatile Thermostat.
        vol.Optional("presence_entity"): entity_selector(list(PRESENCE_DOMAINS)),
        vol.Optional("office_schedule_entity"): entity_selector("schedule"),
        vol.Optional("eco_heating_limit"): numeric(5, 35, unit="°C"),
        vol.Optional("eco_cooling_limit"): numeric(5, 35, unit="°C"),
        vol.Optional("away_heating_limit"): numeric(5, 35, unit="°C"),
        vol.Optional("away_cooling_limit"): numeric(5, 35, unit="°C"),
        vol.Optional("motion_delay_seconds"): numeric(0, 600, 1, "s"),
        vol.Optional("motion_off_delay_minutes"): numeric(1, 240, 1, "min"),
        # Room regulation (0.9): self-regulating setpoint of running units.
        vol.Optional("setpoint_max_offset"): numeric(0, 5, unit="°C"),
        vol.Optional("setpoint_interval_seconds"): numeric(60, 3600, 1, "s"),
        vol.Optional("window_delay_seconds"): numeric(0, 3600, 1, "s"),
        vol.Optional("auto_fan_step"): numeric(0.2, 2, unit="°C"),
        # Screens (0.11): sunshine from solar power; wind alarm blocks.
        vol.Optional("sun_entity"): entity_selector("sun"),
        vol.Optional("sun_power_entity"): entity_selector("sensor"),
        vol.Optional("sun_power_threshold"): numeric(0, 1_000_000, 1, "W"),
        vol.Optional("screen_block_entity"): entity_selector(
            ["binary_sensor", "input_boolean"]
        ),
        vol.Optional("screen_rain_protection"): selector.BooleanSelector(),
        vol.Optional("screen_rain_entity"): entity_selector(
            ["weather", "binary_sensor"]
        ),
        vol.Optional("screen_rain_dry_minutes"): numeric(5, 240, 1, "min"),
        vol.Optional("screen_rain_max_age_minutes"): numeric(5, 180, 1, "min"),
        vol.Optional("screen_wind_protection"): selector.BooleanSelector(),
        vol.Optional("screen_wind_speed_limit"): numeric(1, 200, 1, "km/h"),
        vol.Optional("screen_wind_gust_limit"): numeric(1, 200, 1, "km/h"),
        vol.Optional("screen_wind_calm_minutes"): numeric(5, 240, 1, "min"),
        vol.Optional("screen_wind_max_age_minutes"): numeric(5, 180, 1, "min"),
        vol.Optional("screen_shade_position"): numeric(0, 100, 1, "%"),
        vol.Optional("screen_override_minutes"): numeric(5, 720, 1, "min"),
        vol.Optional("screen_min_move_minutes"): numeric(15, 240, 1, "min"),
        vol.Optional("screen_open_delay_minutes"): numeric(0, 60, 1, "min"),
        vol.Optional("screen_shade_delay_minutes"): numeric(0, 60, 1, "min"),
        # Weather forecast (0.12): warm/cold days save gas and electricity.
        vol.Optional("weather_entity"): entity_selector("weather"),
    }
    if not limits:
        fields = {
            marker: value
            for marker, value in fields.items()
            if marker.schema not in DEVICE_LIMITS
        }
    return fields


# 0.13: the central configuration is edited per subject (menu), like
# Versatile Thermostat. Every central field belongs to exactly one section.
CENTRAL_SECTIONS = {
    "general": (
        "outdoor_temperature_entity",
        "outdoor_max_age_seconds",
        "hysteresis",
        "sensor_validation",
        "max_age_seconds",
    ),
    "presence": (
        "presence_entity",
        "office_schedule_entity",
        "motion_delay_seconds",
        "motion_off_delay_minutes",
    ),
    "regulation": (
        *FLOOR_FIELDS,
        "manual_override_enabled",
        "manual_override_duration_minutes",
        "min_on_seconds",
        "min_off_seconds",
        "changeover_wait_seconds",
        "setpoint_max_offset",
        "setpoint_interval_seconds",
        "window_delay_seconds",
        "auto_fan_step",
    ),
    "screens": (
        "sun_entity",
        "sun_power_entity",
        "sun_power_threshold",
        "screen_block_entity",
        "screen_rain_protection",
        "screen_rain_entity",
        "screen_rain_dry_minutes",
        "screen_rain_max_age_minutes",
        "screen_wind_protection",
        "screen_wind_speed_limit",
        "screen_wind_gust_limit",
        "screen_wind_calm_minutes",
        "screen_wind_max_age_minutes",
        "screen_shade_position",
        "screen_override_minutes",
        "screen_min_move_minutes",
        "screen_open_delay_minutes",
        "screen_shade_delay_minutes",
    ),
    "forecast": ("weather_entity",),
    "preconditioning": PRECONDITIONING_FIELDS,
}
NEW_CENTRAL_FIELDS = ("outdoor_temperature_entity",)


def section_fields(keys) -> dict:
    return {
        marker: value
        for marker, value in building_fields(limits=False).items()
        if marker.schema in keys
    }


def building_defaults() -> dict:
    default_settings = BuildingSettings()
    return {
        **NEW_BUILDING_DEFAULTS,
        **{
            key: getattr(default_settings, key)
            for key in (
                *PRECONDITIONING_FIELDS,
                *FLOOR_FIELDS,
                "outdoor_max_age_seconds",
                "manual_override_enabled",
                "manual_override_duration_minutes",
                "min_on_seconds",
                "min_off_seconds",
                "changeover_wait_seconds",
                "eco_heating_limit",
                "eco_cooling_limit",
                "away_heating_limit",
                "away_cooling_limit",
                "motion_delay_seconds",
                "motion_off_delay_minutes",
                "setpoint_max_offset",
                "setpoint_interval_seconds",
                "window_delay_seconds",
                "auto_fan_step",
                "sun_power_threshold",
                "screen_rain_protection",
                "screen_rain_dry_minutes",
                "screen_rain_max_age_minutes",
                "screen_wind_protection",
                "screen_wind_calm_minutes",
                "screen_wind_max_age_minutes",
                "screen_shade_position",
                "screen_override_minutes",
                "screen_min_move_minutes",
                "screen_open_delay_minutes",
                "screen_shade_delay_minutes",
            )
        },
    }


def multi(domain):
    return selector.EntitySelector({"filter": {"domain": domain}, "multiple": True})


ZONE_DEFAULTS = {
    "weight": 1.0,
    "use_building_limits": True,
    "use_building_parameters": True,
    "use_building_preconditioning": True,
    "use_building_floor_heating": True,
}
ZONE_FLAGS = (
    "use_building_limits",
    "use_building_parameters",
    "use_building_preconditioning",
    "use_building_floor_heating",
)


def zone_fields(groups: list[dict]) -> dict:
    """Zone settings, shared by the zone config flow and its options flow."""
    return {
        vol.Required("name"): selector.TextSelector(),
        vol.Optional("temperature_entity"): entity_selector("sensor"),
        vol.Optional("indoor_units"): multi("climate"),
        vol.Optional("group_id"): choice(
            [
                {"value": "", "label": "No group"},
                *[{"value": group["id"], "label": group["name"]} for group in groups],
            ]
        ),
        vol.Optional("weight"): numeric(0.1, 10),
        vol.Optional("use_building_limits"): selector.BooleanSelector(),
        vol.Optional("use_building_parameters"): selector.BooleanSelector(),
        vol.Optional("occupancy_entity"): entity_selector("binary_sensor"),
        vol.Optional("calendar_entity"): entity_selector("calendar"),
        vol.Optional("use_building_preconditioning"): selector.BooleanSelector(),
        vol.Optional("use_building_floor_heating"): selector.BooleanSelector(),
        vol.Optional("humidity_entity"): entity_selector("sensor"),
        vol.Optional("co2_entity"): entity_selector("sensor"),
        vol.Optional("contact_entities"): multi("binary_sensor"),
        vol.Optional("floor_heating_entities"): multi(["climate", "number"]),
        vol.Optional("shading_entities"): multi("cover"),
        vol.Optional("facade"): selector.SelectSelector(
            {
                "options": list(FACADES),
                "mode": "dropdown",
                "translation_key": "facade",
            }
        ),
        vol.Optional("ventilation_entities"): multi(["fan", "switch"]),
        # Vane (0.10): up when cooling, down when heating.
        vol.Optional("vane_entity"): entity_selector(list(VANE_DOMAINS)),
        vol.Optional("vane_cool_value"): selector.TextSelector(),
        vol.Optional("vane_heat_value"): selector.TextSelector(),
    }


# Each settings page owns only these keys; clearing a field never removes
# settings on another page. Local values stay dormant while central is on.
ZONE_OPTION_SECTIONS = {
    "zone_general": ("name", "weight", "use_building_limits"),
    "zone_sensors": (
        "temperature_entity",
        "occupancy_entity",
        "humidity_entity",
        "co2_entity",
        "contact_entities",
    ),
    "zone_hvac": (
        "indoor_units",
        "group_id",
        "vane_entity",
        "vane_cool_value",
        "vane_heat_value",
    ),
    "zone_regulation": (
        "use_building_parameters",
        "hysteresis",
        "sensor_validation",
        "max_age_seconds",
    ),
    "zone_floor_heating": (
        "floor_heating_entities",
        "use_building_floor_heating",
        *FLOOR_FIELDS,
    ),
    "zone_screens": ("shading_entities", "facade"),
    "zone_ventilation": ("ventilation_entities",),
    "zone_preconditioning": (
        "calendar_entity",
        "use_building_preconditioning",
        *PRECONDITIONING_FIELDS,
    ),
}


def zone_section_fields(section: str, groups: list[dict]) -> dict:
    """Reuse the existing selectors, displaying just one room subject."""
    fields = {
        **zone_fields(groups),
        **zone_parameter_fields(dict.fromkeys(ZONE_FLAGS, False)),
    }
    keys = ZONE_OPTION_SECTIONS[section]
    return {marker: value for marker, value in fields.items() if marker.schema in keys}


def zone_parameter_fields(record: dict) -> dict:
    """Local parameters, only when not taken from the building."""
    fields = {}
    if not record.get("use_building_parameters", True):
        fields.update(
            {
                vol.Optional("hysteresis"): numeric(0.1, 2, unit="°C"),
                vol.Optional("sensor_validation"): choice(list(VALIDATION_STRATEGIES)),
                vol.Optional("max_age_seconds"): numeric(0, 86400, 1, "s"),
            }
        )
    if not record.get("use_building_preconditioning", True):
        fields.update(preconditioning_fields())
    if not record.get("use_building_floor_heating", True):
        fields.update(floor_fields())
    return fields


def clean(user_input: dict) -> dict:
    return {
        key: value for key, value in user_input.items() if value not in ("", None, [])
    }


def merge_zone(previous: dict, submitted: dict) -> dict:
    """New zone settings; earlier local limits/parameters stay dormant."""
    dormant = {
        key: previous[key]
        for key in (
            *LIMIT_FIELDS,
            *BAND_FIELDS,
            *PARAMETER_FIELDS,
            *PRECONDITIONING_FIELDS,
            *FLOOR_FIELDS,
        )
        if key in previous
    }
    record = {"id": previous["id"], **dormant, **clean(submitted)}
    for flag in ZONE_FLAGS:
        record.setdefault(flag, previous.get(flag, True))
    return record


def merge_zone_parameters(previous: dict, submitted: dict) -> dict:
    """Replace the visible parameter fields; retain hidden local settings."""
    visible = {marker.schema for marker in zone_parameter_fields(previous)}
    return {
        **{key: value for key, value in previous.items() if key not in visible},
        **clean(submitted),
    }


def zone_entities(record: dict) -> set[str]:
    values = [record.get(field) for field in ZONE_ENTITY_FIELDS]
    values += list(record.get("contact_entities", ()))
    values += [
        entity for field, _ in EQUIPMENT_FIELDS for entity in record.get(field, ())
    ]
    return {value for value in values if value}


def check_zone(hass, record: dict, entry_id: str | None = None, partial=False) -> dict:
    """Validate a zone against the central configuration and all other zones."""
    central = central_entry(hass)
    if central is None:
        return {"base": "central_required"}
    entries = config_zone_entries(hass)
    previous = next((dict(e.options) for e in entries if e.entry_id == entry_id), {})
    others = [dict(e.options) for e in entries if e.entry_id != entry_id]
    candidate = dict(record)
    if partial:  # Local sensor parameters follow in the next step.
        candidate.update({flag: True for flag in ZONE_FLAGS})
    try:
        build_model(
            central.entry_id,
            central.data["name"],
            assemble(dict(central.options), [*others, candidate]),
        )
        registry = er.async_get(hass)
        known = zone_entities(previous)
        for entity_id in zone_entities(record):
            registered = registry.async_get(entity_id)
            if registered and registered.platform == DOMAIN:
                raise ValueError("invalid_entity")
            if (
                entity_id not in known
                and not registered
                and hass.states.get(entity_id) is None
            ):
                raise ValueError("entity_not_found")
    except ValueError as err:
        return {"base": str(err)}
    return {}


class ZoneFormMixin:
    """Zone settings, followed by local sensor parameters when needed."""

    zone: dict

    def _groups(self) -> list[dict]:
        central = central_entry(self.hass)
        return list(central.options.get("groups", [])) if central else []

    def _zone_form(self, step_id, values, errors):
        schema = self.add_suggested_values_to_schema(
            vol.Schema(zone_fields(self._groups())), {**ZONE_DEFAULTS, **values}
        )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    def _parameters_form(self, values, errors):
        central = central_entry(self.hass)
        building = dict(central.options.get("building", {})) if central else {}
        schema = self.add_suggested_values_to_schema(
            vol.Schema(zone_parameter_fields(self.zone)),
            {**building_defaults(), **building, **values},
        )
        return self.async_show_form(
            step_id="zone_parameters", data_schema=schema, errors=errors
        )


class ZonentraConfigFlow(ZoneFormMixin, ConfigFlow, domain=DOMAIN):
    """Like Versatile Thermostat: central configuration first, then zones."""

    VERSION = 3

    def __init__(self):
        self.zone: dict = {}

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["central", "zone"])

    async def async_step_central(self, user_input=None) -> ConfigFlowResult:
        if central_entry(self.hass):
            return self.async_abort(reason="central_already_configured")
        errors = {}
        if user_input is not None:
            name = str(user_input.get("name", "")).strip()
            settings = {
                # Everything starts at the defaults: limits are set on the
                # device, the other subjects later via Configure.
                **building_defaults(),
                **{
                    key: value
                    for key, value in user_input.items()
                    if key != "name" and value != ""
                },
            }
            if not name:
                errors["name"] = "invalid_name"
            else:
                try:
                    build_model("central", name, {"building": settings})
                except ValueError as err:
                    errors["base"] = str(err)
                else:
                    await self.async_set_unique_id(CENTRAL_UNIQUE_ID)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=name,
                        data={ENTRY_TYPE: CENTRAL, "name": name},
                        options={"building": settings, "groups": []},
                    )
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required("name"): selector.TextSelector(),
                    **section_fields(NEW_CENTRAL_FIELDS),
                }
            ),
            user_input or {},
        )
        return self.async_show_form(
            step_id="central", data_schema=schema, errors=errors
        )

    async def async_step_zone(self, user_input=None) -> ConfigFlowResult:
        if central_entry(self.hass) is None:
            return self.async_abort(reason="central_required")
        errors = {}
        if user_input is not None:
            self.zone = merge_zone(
                {"id": self.zone.get("id") or uuid4().hex}, user_input
            )
            errors = check_zone(self.hass, self.zone, partial=True)
            if not errors:
                if zone_parameter_fields(self.zone):
                    return await self.async_step_zone_parameters()
                return await self._create_zone()
        return self._zone_form("zone", user_input or self.zone, errors)

    async def async_step_zone_parameters(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self.zone = merge_zone_parameters(self.zone, user_input)
            errors = check_zone(self.hass, self.zone)
            if not errors:
                return await self._create_zone()
        return self._parameters_form(user_input or self.zone, errors)

    async def _create_zone(self) -> ConfigFlowResult:
        await self.async_set_unique_id(self.zone["id"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self.zone["name"], data={ENTRY_TYPE: ZONE}, options=self.zone
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        kind = config_entry.data.get(ENTRY_TYPE)
        if kind == ZONE:
            return ZoneOptionsFlow()
        if kind == CENTRAL:
            return CentralOptionsFlow()
        return ZonentraOptionsFlow()


class ZoneOptionsFlow(ZoneFormMixin, OptionsFlow):
    """Edit room subjects in a draft, then save and reload the room once."""

    def __init__(self):
        self.zone: dict | None = None

    async def async_step_init(self, user_input=None):
        return await self.async_step_zone_menu()

    async def async_step_zone_menu(self, user_input=None):
        if self.zone is None:
            self.zone = deepcopy(dict(self.config_entry.options))
        return self.async_show_menu(
            step_id="zone_menu",
            menu_options=[
                "zone_general",
                "zone_sensors",
                "zone_hvac",
                "zone_regulation",
                "zone_floor_heating",
                "zone_screens",
                "zone_ventilation",
                "zone_preconditioning",
                "finish",
            ],
        )

    async def _section(self, section, user_input):
        keys = ZONE_OPTION_SECTIONS[section]
        errors = {}
        if user_input is not None:
            candidate = {
                **{key: value for key, value in self.zone.items() if key not in keys},
                **clean(user_input),
            }
            for flag in ZONE_FLAGS:
                if flag in keys:
                    candidate.setdefault(flag, self.zone.get(flag, True))
            for flag, local_keys in (
                (
                    "use_building_parameters",
                    ("hysteresis", "sensor_validation", "max_age_seconds"),
                ),
                ("use_building_preconditioning", PRECONDITIONING_FIELDS),
                ("use_building_floor_heating", FLOOR_FIELDS),
            ):
                if flag in keys and candidate.get(flag, True):
                    for key in local_keys:
                        candidate.pop(key, None)
                        if key in self.zone:
                            candidate[key] = self.zone[key]
            errors = check_zone(self.hass, candidate, self.config_entry.entry_id)
            if not errors:
                self.zone = candidate
                return await self.async_step_init()
        central = central_entry(self.hass)
        building = dict(central.options.get("building", {})) if central else {}
        schema = self.add_suggested_values_to_schema(
            vol.Schema(zone_section_fields(section, self._groups())),
            {
                **building_defaults(),
                **building,
                **ZONE_DEFAULTS,
                **self.zone,
                **(user_input or {}),
            },
        )
        return self.async_show_form(step_id=section, data_schema=schema, errors=errors)

    async def async_step_zone_general(self, user_input=None):
        return await self._section("zone_general", user_input)

    async def async_step_zone_sensors(self, user_input=None):
        return await self._section("zone_sensors", user_input)

    async def async_step_zone_hvac(self, user_input=None):
        return await self._section("zone_hvac", user_input)

    async def async_step_zone_regulation(self, user_input=None):
        return await self._section("zone_regulation", user_input)

    async def async_step_zone_floor_heating(self, user_input=None):
        return await self._section("zone_floor_heating", user_input)

    async def async_step_zone_screens(self, user_input=None):
        return await self._section("zone_screens", user_input)

    async def async_step_zone_ventilation(self, user_input=None):
        return await self._section("zone_ventilation", user_input)

    async def async_step_zone_preconditioning(self, user_input=None):
        return await self._section("zone_preconditioning", user_input)

    async def async_step_finish(self, user_input=None):
        # Revalidate in case another room changed while the draft was open.
        errors = check_zone(self.hass, self.zone, self.config_entry.entry_id)
        if errors:
            result = await self._section("zone_hvac", None)
            result["errors"] = errors
            return result
        self.hass.config_entries.async_update_entry(
            self.config_entry, title=self.zone["name"], options=self.zone
        )
        return self.async_create_entry(title="", data=self.zone)


class ZonentraOptionsFlow(OptionsFlowWithReload):
    """Legacy (pre-0.6) single entry: collect edits and save only via Finish."""

    _limits_in_form = True

    def __init__(self):
        self.topology: dict[str, Any] | None = None
        self.selected: str | None = None
        self.new_id: str | None = None
        self._visible_zone_blocks = {"limits": False, "parameters": False}

    async def async_step_init(self, user_input=None):
        if self.topology is None:
            self.topology = migrate_options(dict(self.config_entry.options))
            for key in TOPOLOGY_KEYS:
                self.topology.setdefault(key, [])
        return self.async_show_menu(
            step_id="init",
            menu_options=["building", "zone", "group", "equipment", "finish"],
        )

    async def async_step_building(self, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = deepcopy(self.topology)
            settings = {key: value for key, value in user_input.items() if value != ""}
            if not self._limits_in_form:
                # Set on the device: keep them when the form is saved.
                current = self.topology.get("building", {})
                settings = {
                    **{key: current[key] for key in DEVICE_LIMITS if key in current},
                    **settings,
                }
            candidates = self.topology.get("building", {}).get(
                "outdoor_temperature_candidates", []
            )
            if candidates and not settings.get("outdoor_temperature_entity"):
                settings["outdoor_temperature_candidates"] = candidates
            candidate["building"] = settings
            try:
                self._validate(candidate)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                self.topology = candidate
                return await self.async_step_init()
        fields = building_fields(limits=self._limits_in_form)
        defaults = building_defaults()
        current = self.topology.get("building", {})
        schema = self.add_suggested_values_to_schema(
            vol.Schema(fields), {**defaults, **current, **(user_input or {})}
        )
        return self.async_show_form(
            step_id="building",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "outdoor_candidates": ", ".join(
                    current.get("outdoor_temperature_candidates", [])
                )
                or "—"
            },
        )

    async def async_step_finish(self, user_input=None):
        # Revalidate at the transaction boundary.
        self._model(self.topology)
        return self.async_create_entry(title="", data=self._stored())

    def _model(self, topology):
        return build_model(
            self.config_entry.entry_id, self.config_entry.data["name"], topology
        )

    def _stored(self) -> dict:
        return self.topology

    async def _select(self, category, user_input):
        if user_input is not None:
            self.selected = user_input["record"]
            self.new_id = uuid4().hex if self.selected == "__new__" else None
            return await getattr(self, f"async_step_{category}_details")()
        records = self.topology[
            {"zone": "zones", "group": "groups", "equipment": "equipment"}[category]
        ]
        return self.async_show_form(
            step_id=category,
            data_schema=vol.Schema(
                {
                    vol.Required("record"): choice(
                        [
                            {"value": "__new__", "label": "Add new"},
                            *[
                                {"value": item["id"], "label": item["name"]}
                                for item in records
                            ],
                        ]
                    ),
                }
            ),
        )

    async def async_step_zone(self, user_input=None):
        return await self._select("zone", user_input)

    async def async_step_group(self, user_input=None):
        return await self._select("group", user_input)

    async def async_step_equipment(self, user_input=None):
        if not self.topology["zones"]:
            return self.async_show_form(
                step_id="equipment", errors={"base": "add_zone_first"}
            )
        return await self._select("equipment", user_input)

    def _schema(self, category, suggestions=None):
        fields = {
            vol.Required("name"): selector.TextSelector(),
        }
        if category == "zone":
            fields.update(
                {
                    vol.Optional("temperature_entity"): entity_selector("sensor"),
                    vol.Optional("use_building_limits"): selector.BooleanSelector(),
                    vol.Optional("use_building_parameters"): selector.BooleanSelector(),
                    vol.Optional("occupancy_entity"): entity_selector("binary_sensor"),
                    vol.Optional("calendar_entity"): entity_selector("calendar"),
                    vol.Optional(
                        "use_building_preconditioning"
                    ): selector.BooleanSelector(),
                    **preconditioning_fields(),
                    **floor_fields(),
                    vol.Optional(
                        "use_building_floor_heating"
                    ): selector.BooleanSelector(),
                    vol.Optional("heating_limit"): numeric(5, 35, unit="°C"),
                    vol.Optional("cooling_limit"): numeric(5, 35, unit="°C"),
                    vol.Optional("humidity_entity"): entity_selector("sensor"),
                    vol.Optional("co2_entity"): entity_selector("sensor"),
                    vol.Optional("contact_entities"): selector.EntitySelector(
                        {"filter": {"domain": "binary_sensor"}, "multiple": True}
                    ),
                    vol.Optional("weight"): numeric(0.1, 10),
                    vol.Optional("hysteresis"): numeric(0.1, 2, unit="°C"),
                    vol.Optional("sensor_validation"): choice(
                        list(VALIDATION_STRATEGIES)
                    ),
                    vol.Optional("max_age_seconds"): numeric(0, 86400, 1, "s"),
                    vol.Optional("group_id"): self._group_selector(),
                }
            )
        elif category == "group":
            fields.update(
                {
                    vol.Optional("mode_entity"): entity_selector(
                        ["sensor", "select", "climate"]
                    ),
                    vol.Optional("min_switch_seconds"): numeric(0, 86400, 1, "s"),
                    vol.Optional("min_demand_score"): numeric(0.1, 100),
                    vol.Optional("score_deadband"): numeric(0.1, 100),
                    vol.Optional("max_age_seconds"): numeric(0, 86400, 1, "s"),
                }
            )
        elif category == "equipment":
            fields.update(
                {
                    vol.Required("kind"): choice(list(KINDS)),
                    vol.Required("entity_id"): entity_selector(),
                    vol.Required("zone_id"): choice(
                        [
                            {"value": item["id"], "label": item["name"]}
                            for item in self.topology["zones"]
                        ]
                    ),
                    vol.Optional("group_id"): self._group_selector(),
                }
            )
        if self.selected != "__new__":
            fields[vol.Optional("delete", default=False)] = selector.BooleanSelector()
        schema = vol.Schema(fields)
        key = {"zone": "zones", "group": "groups", "equipment": "equipment"}[category]
        current = next(
            (item for item in self.topology[key] if item["id"] == self.selected), {}
        )
        defaults = {}
        if category == "zone":
            zone = Zone(id="suggestion", name="suggestion")
            defaults = {
                "heating_limit": 21.0,
                "cooling_limit": 24.0,
                **{key: getattr(zone, key) for key in PRECONDITIONING_FIELDS},
                **{key: getattr(zone, key) for key in FLOOR_FIELDS},
                "weight": zone.weight,
                "hysteresis": zone.hysteresis,
                "max_age_seconds": zone.max_age_seconds,
                "sensor_validation": zone.sensor_validation,
                "use_building_limits": True,
                "use_building_parameters": True,
                "use_building_preconditioning": True,
                "use_building_floor_heating": True,
            }
        elif category == "group":
            group = EquipmentGroup(id="suggestion", name="suggestion")
            defaults = {
                key: getattr(group, key)
                for key in (
                    "min_switch_seconds",
                    "min_demand_score",
                    "score_deadband",
                    "max_age_seconds",
                )
            }
        if category == "zone":
            central = self.topology.get("building", {})
            defaults.update(
                {
                    key: central[key]
                    for key in (
                        *LIMIT_FIELDS,
                        *PARAMETER_FIELDS,
                        *PRECONDITIONING_FIELDS,
                        *FLOOR_FIELDS,
                    )
                    if key in central
                }
            )
        values = {**defaults, **current, **(suggestions or {})}
        if category == "zone" and suggestions is not None:
            for key in (
                "temperature_entity",
                "occupancy_entity",
                "humidity_entity",
                "co2_entity",
                "contact_entities",
                "group_id",
            ):
                if key not in suggestions:
                    values.pop(key, None)
        if category == "zone":
            for flag, block, keys in (
                ("use_building_limits", "limits", LIMIT_FIELDS),
                ("use_building_parameters", "parameters", PARAMETER_FIELDS),
                ("use_building_floor_heating", "floor_heating", FLOOR_FIELDS),
                (
                    "use_building_preconditioning",
                    "preconditioning",
                    PRECONDITIONING_FIELDS,
                ),
            ):
                self._visible_zone_blocks[block] = not values[flag]
                if values[flag]:
                    fields = {
                        marker: value
                        for marker, value in fields.items()
                        if marker.schema not in keys
                    }
            schema = vol.Schema(fields)
        return self.add_suggested_values_to_schema(schema, values)

    def _group_selector(self):
        return choice(
            [
                {"value": "", "label": "No group"},
                *[
                    {"value": item["id"], "label": item["name"]}
                    for item in self.topology["groups"]
                ],
            ]
        )

    async def _edit(self, category, user_input):
        errors = {}
        if user_input is not None:
            candidate = deepcopy(self.topology)
            key = {"zone": "zones", "group": "groups", "equipment": "equipment"}[
                category
            ]
            record = {k: v for k, v in user_input.items() if k != "delete" and v != ""}
            record["id"] = self.new_id if self.selected == "__new__" else self.selected
            previous_record = next(
                (item for item in self.topology[key] if item["id"] == self.selected), {}
            )
            if category == "zone":
                for flag, block, keys in (
                    ("use_building_limits", "limits", LIMIT_FIELDS),
                    ("use_building_parameters", "parameters", PARAMETER_FIELDS),
                    ("use_building_floor_heating", "floor_heating", FLOOR_FIELDS),
                    (
                        "use_building_preconditioning",
                        "preconditioning",
                        PRECONDITIONING_FIELDS,
                    ),
                ):
                    record.setdefault(flag, previous_record.get(flag, True))
                    if (
                        not record[flag]
                        and not self._visible_zone_blocks[block]
                        and not user_input.get("delete")
                    ):
                        return self.async_show_form(
                            step_id="zone_details",
                            data_schema=self._schema(
                                category, {**user_input, **{flag: record[flag]}}
                            ),
                            errors={},
                        )
                    if record[flag]:
                        for field in keys:
                            record.pop(field, None)
                            if field in previous_record:
                                record[field] = previous_record[field]
            if category == "group":
                previous_record = next(
                    (
                        item
                        for item in self.topology[key]
                        if item["id"] == self.selected
                    ),
                    {},
                )
                if "single_hvac_mode" in previous_record:
                    record["single_hvac_mode"] = previous_record["single_hvac_mode"]
            try:
                candidate[key] = [
                    item for item in candidate[key] if item["id"] != self.selected
                ]
                if not user_input.get("delete", False):
                    candidate[key].append(record)
                self._validate(candidate)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                self.topology = candidate
                return await self.async_step_init()
        schema = self._schema(category, user_input)
        return self.async_show_form(
            step_id=f"{category}_details", data_schema=schema, errors=errors
        )

    def _validate(self, candidate):
        building = self._model(candidate)
        previous = self._model(self.topology)
        known = previous.entity_ids | frozenset(
            previous.settings.outdoor_temperature_candidates
        )
        registry = er.async_get(self.hass)
        for entity_id in building.entity_ids:
            registered = registry.async_get(entity_id)
            if registered and registered.platform == DOMAIN:
                raise ValueError("invalid_entity")
            if (
                entity_id not in known
                and not registered
                and self.hass.states.get(entity_id) is None
            ):
                raise ValueError("entity_not_found")

    async def async_step_zone_details(self, user_input=None):
        return await self._edit("zone", user_input)

    async def async_step_group_details(self, user_input=None):
        return await self._edit("group", user_input)

    async def async_step_equipment_details(self, user_input=None):
        return await self._edit("equipment", user_input)


class CentralOptionsFlow(ZonentraOptionsFlow):
    """Central configuration: building settings and outdoor unit groups."""

    _limits_in_form = False  # Comfort/eco/away limits are set on the device.

    async def async_step_init(self, user_input=None):
        if self.topology is None:
            options = self.config_entry.options
            self.topology = {
                "building": dict(options.get("building", {})),
                "groups": [dict(group) for group in options.get("groups", [])],
                "zones": [],
                "equipment": [],
            }
        return self.async_show_menu(
            step_id="init",
            # Literal, so every subject's label can be checked (= CENTRAL_SECTIONS).
            menu_options=[
                "general",
                "presence",
                "regulation",
                "screens",
                "forecast",
                "preconditioning",
                "group",
                "finish",
            ],
        )

    async def _section(self, section, user_input):
        """One subject of the central settings; the others stay as they are."""
        keys = CENTRAL_SECTIONS[section]
        current = self.topology.get("building", {})
        errors = {}
        if user_input is not None:
            settings = {
                **{key: value for key, value in current.items() if key not in keys},
                **{key: value for key, value in user_input.items() if value != ""},
            }
            if settings.get("outdoor_temperature_entity"):
                # The old candidates are resolved by choosing a sensor.
                settings.pop("outdoor_temperature_candidates", None)
            candidate = {**self.topology, "building": settings}
            try:
                self._validate(candidate)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                self.topology = candidate
                return await self.async_step_init()
        schema = self.add_suggested_values_to_schema(
            vol.Schema(section_fields(keys)),
            {**building_defaults(), **current, **(user_input or {})},
        )
        return self.async_show_form(
            step_id=section,
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "outdoor_candidates": ", ".join(
                    current.get("outdoor_temperature_candidates", [])
                )
                or "—"
            },
        )

    async def async_step_general(self, user_input=None):
        return await self._section("general", user_input)

    async def async_step_presence(self, user_input=None):
        return await self._section("presence", user_input)

    async def async_step_regulation(self, user_input=None):
        return await self._section("regulation", user_input)

    async def async_step_screens(self, user_input=None):
        return await self._section("screens", user_input)

    async def async_step_forecast(self, user_input=None):
        return await self._section("forecast", user_input)

    async def async_step_preconditioning(self, user_input=None):
        return await self._section("preconditioning", user_input)

    def _model(self, topology):
        # Validate against every zone: a referenced group cannot be removed.
        return build_model(
            self.config_entry.entry_id,
            self.config_entry.data["name"],
            assemble(
                {
                    "building": topology.get("building", {}),
                    "groups": topology["groups"],
                },
                [dict(entry.options) for entry in config_zone_entries(self.hass)],
            ),
        )

    def _stored(self) -> dict:
        return {
            "building": self.topology.get("building", {}),
            "groups": self.topology["groups"],
        }
