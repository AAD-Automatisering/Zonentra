"""Finite temperature values and intervals at Home Assistant boundaries."""

import math

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import (
    TemperatureConverter,
    TemperatureDeltaConverter,
)


def climate_native_unit(entity) -> str:
    """Read a climate entity's native unit on both supported APIs."""
    try:
        return entity.native_temperature_unit
    except AttributeError:
        return entity.temperature_unit


def convert_temperature(
    value, source, target=UnitOfTemperature.CELSIUS, *, interval=False
):
    """Return a finite converted number, or None for unusable input/units."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not isinstance(source, str)
        or not isinstance(target, str)
        or source not in TemperatureConverter.VALID_UNITS
        or target not in TemperatureConverter.VALID_UNITS
    ):
        return None
    converter = TemperatureDeltaConverter if interval else TemperatureConverter
    result = converter.convert(float(value), source, target)
    return result if math.isfinite(result) else None
