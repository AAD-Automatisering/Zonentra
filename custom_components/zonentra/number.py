"""Climate limits as settings on the devices (0.9 / 0.10), like VTherm presets.

* Central device: the building's comfort, eco and away limits. They are kept
  in the central configuration and applied without reloading.
* Zone device: the room's own comfort, eco and away limits, only when the
  zone does not use the central climate limits; they start at the central
  values (0.13.3).
* Central device, with a weather entity (0.12): when a day counts as warm or
  cold (expected maximum of the coming hours) and the start margin.
"""

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from . import central_topology
from .const import DOMAIN
from .entity import ZoneEntity, remove_stale, zone_context
from .models import build_model
from .topology import ENTRY_TYPE, ZONE

LIMIT_KEYS = (
    "heating_limit",
    "cooling_limit",
    "eco_heating_limit",
    "eco_cooling_limit",
    "away_heating_limit",
    "away_cooling_limit",
)
PAIR = {
    "heating_limit": "cooling_limit",
    "cooling_limit": "heating_limit",
    "eco_heating_limit": "eco_cooling_limit",
    "eco_cooling_limit": "eco_heating_limit",
    "away_heating_limit": "away_cooling_limit",
    "away_cooling_limit": "away_heating_limit",
}
# 0.12 forecast settings: key -> (minimum, maximum, step).
FORECAST_KEYS = {
    "warm_day_temperature": (-20, 45, 0.5),
    "cold_day_temperature": (-20, 45, 0.5),
    "forecast_margin": (0, 3, 0.1),
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    if entry.data.get(ENTRY_TYPE) == ZONE:
        # 0.13.3: the room's own limits only when it does not use the central
        # climate limits (zone configuration); otherwise they disappear.
        entities = []
        if not entry.options.get("use_building_limits", True):
            context = zone_context(hass, entry)
            floor_only = bool(
                entry.options.get("floor_heating_entities")
            ) and not entry.options.get("indoor_units")
            keys = tuple(
                key for key in LIMIT_KEYS if not floor_only or "cooling" not in key
            )
            entities = [ZoneLimitNumber(entry, key, **context) for key in keys]
        remove_stale(hass, entry, "number", entities)
        async_add_entities(entities)
        return
    entities = [CentralLimitNumber(entry, key) for key in LIMIT_KEYS]
    if entry.runtime_data.building.settings.weather_entity:
        entities.extend(CentralForecastNumber(entry, key) for key in FORECAST_KEYS)
    async_add_entities(entities)


class LimitNumber(NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 5
    _attr_native_max_value = 35
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX


class CentralLimitNumber(LimitNumber):
    """A building-wide limit on the central device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key) -> None:
        self._entry = entry
        self._key = key
        self._attr_translation_key = f"central_{key}"
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data["name"],
            manufacturer="Zonentra",
        )

    @property
    def native_value(self) -> float | None:
        return getattr(self._entry.runtime_data.building.settings, self._key)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{DOMAIN}_{self._entry.entry_id}_updated", self._updated
            )
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        entry = self._entry
        options = dict(entry.options)
        building = {**options.get("building", {}), self._key: value}
        options["building"] = building
        try:
            model = build_model(
                entry.entry_id,
                entry.data["name"],
                central_topology(self.hass, entry, options),
            )
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=str(err)
            ) from err
        # No reload: the runtime takes the new building model directly.
        self.hass.config_entries.async_update_entry(entry, options=options)
        entry.runtime_data.apply_building(model)


class CentralForecastNumber(CentralLimitNumber):
    """When a day is warm or cold, and how much later heating/cooling starts."""

    def __init__(self, entry, key) -> None:
        super().__init__(entry, key)
        minimum, maximum, step = FORECAST_KEYS[key]
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        if key == "forecast_margin":
            self._attr_device_class = None  # A temperature difference.
            self._attr_icon = "mdi:thermometer-chevron-up"


class ZoneLimitNumber(ZoneEntity, LimitNumber):
    """The room's own limit; shows the central value until one is set."""

    def __init__(self, entry, key, provider, signal, device) -> None:
        super().__init__(entry, provider, signal, device)
        self._key = key
        comfort = key in ("heating_limit", "cooling_limit")
        self._attr_translation_key = f"comfort_{key}" if comfort else key
        self._attr_unique_id = (
            f"{entry.entry_id}_comfort_{key}"
            if key in ("heating_limit", "cooling_limit")
            else f"{entry.entry_id}_{key}"
        )

    @property
    def native_value(self) -> float | None:
        runtime = self.runtime
        if self._key in ("heating_limit", "cooling_limit"):
            index = 0 if self._key == "heating_limit" else 1
            return runtime.comfort[self._zone_id][index].value
        own = getattr(self.thermostat, self._key)
        if own is not None:
            return own
        zone = next(z for z in runtime.building.zones if z.id == self._zone_id)
        return getattr(zone, self._key)

    @property
    def extra_state_attributes(self) -> dict:
        thermostat = self.thermostat
        own = (
            thermostat.has_band
            if self._key in ("heating_limit", "cooling_limit")
            else getattr(thermostat, self._key) is not None
        )
        return {"source": "zone" if own else "central"}

    @property
    def native_max_value(self) -> float:
        runtime = self.runtime
        if runtime is not None and self._zone_id in runtime.building.floor_only_zones:
            zone = next(z for z in runtime.building.zones if z.id == self._zone_id)
            return min(35, 35 - 2 * zone.hysteresis - self._attr_native_step)
        return super().native_max_value

    async def async_set_native_value(self, value: float) -> None:
        runtime = self.runtime
        floor_only = (
            runtime is not None and self._zone_id in runtime.building.floor_only_zones
        )
        zone = (
            next((z for z in runtime.building.zones if z.id == self._zone_id), None)
            if runtime
            else None
        )
        if self._key in ("heating_limit", "cooling_limit"):
            heating, cooling = (
                reading.value for reading in self.runtime.comfort[self._zone_id]
            )
            band = {"heating_limit": heating, "cooling_limit": cooling}
            band[self._key] = value
            if (
                floor_only
                and self._key == "heating_limit"
                and band["cooling_limit"] is not None
                and value + 2 * zone.hysteresis >= band["cooling_limit"]
            ):
                band["cooling_limit"] = min(
                    35, value + 2 * zone.hysteresis + self._attr_native_step
                )
            if band["heating_limit"] is None or band["cooling_limit"] is None:
                # No band yet: open one 2 °C wide around the first value set.
                other = PAIR[self._key]
                band[other] = value + (2 if other == "cooling_limit" else -2)
            self._change(**band)
            return
        changes = {self._key: value}
        if floor_only and self._key in ("eco_heating_limit", "away_heating_limit"):
            cooling_key = PAIR[self._key]
            cooling = getattr(self.thermostat, cooling_key)
            if cooling is None:
                cooling = getattr(zone, cooling_key)
            if value + 2 * zone.hysteresis >= cooling:
                changes[cooling_key] = min(
                    35, value + 2 * zone.hysteresis + self._attr_native_step
                )
        self._change(**changes)
