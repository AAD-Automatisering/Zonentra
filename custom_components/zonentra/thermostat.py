"""Pure room thermostat logic (0.9): band choice, room regulation, setpoints.

Like Versatile Thermostat, every zone gets its own thermostat. Its state is
small and persistent:

* ``hvac_mode``: the standard Home Assistant modes. ``heat`` (the room only
  asks for heating, up to its heating limit), ``cool`` (only cooling, from its
  cooling limit), ``heat_cool`` (both: Zonentra keeps the room inside its
  comfort band) or ``off`` (the room asks for nothing; regulated units are
  switched off). The room mode never forces the outdoor unit: the group still
  weighs every room's demand, and a heat-only room waits while its group cools;
* ``preset``: ``comfort`` (presence and motion still lower the band to eco or
  away, as in 0.8), or a fixed ``eco`` / ``away``; building absence always
  wins;
* ``heating_limit`` / ``cooling_limit``: the room's own comfort band, set from
  the thermostat or the number entities. Unset means the configured limits;
* ``regulation``: whether Zonentra really switches and steers this room's
  indoor units. Off by default: the room is then only simulated.

Room regulation never decides the outdoor unit mode: a unit is only switched
on in a mode the outdoor unit group already runs in (0.7 stand control). The
setpoint of a running unit is steered on the room sensor, bounded.
Pure module: it only returns decisions; the runtime sends commands.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .demand import ZoneDemand
from .executor import ModeCommand
from .presence import Occupancy

THERMOSTAT_MODES = ("heat_cool", "heat", "cool", "dry", "fan_only", "off")
# Modes that run the units without heating or cooling demand of their own.
UNIT_MODES = ("dry", "fan_only")
# Demand that a thermostat mode does not allow, and the reason it is dropped.
IGNORED_DEMAND = {
    "heat": ("cooling_demand", "thermostat_heat_only"),
    "cool": ("heating_demand", "thermostat_cool_only"),
}
PRESETS = ("comfort", "eco", "away")
BAND_KEYS = (
    "eco_heating_limit",
    "eco_cooling_limit",
    "away_heating_limit",
    "away_cooling_limit",
)
BANDS = ("comfort", "eco", "away")
DEFAULT_MIN_SETPOINT = 16.0
DEFAULT_MAX_SETPOINT = 30.0
# Conservative trial settings, independent of the manufacturer's fan control.
NATIVE_GAIN = 0.25
NATIVE_MAX_OFFSET = 1.0


def native_hold_allowed(mode, temperature, heating_limit, cooling_limit, hysteresis):
    """Keep running only inside the external room sensor's bounded tolerance."""
    if temperature is None or heating_limit is None or cooling_limit is None:
        return False
    tolerance = max(1.0, 2 * hysteresis)
    if mode == "heat":
        return temperature <= heating_limit + tolerance
    if mode == "cool":
        return temperature >= cooling_limit - tolerance
    return False


@dataclass(frozen=True)
class Thermostat:
    hvac_mode: str = "heat_cool"
    preset: str = "comfort"
    heating_limit: float | None = None
    cooling_limit: float | None = None
    regulation: bool = False
    # None: the automatic fan (0.13.5; before: leave the units' fan alone).
    fan_mode: str | None = None
    window_bypass: bool = False  # 0.10: an open window is ignored
    screen_regulation: bool = False  # 0.11: Zonentra really moves the screens
    # 0.10: the room's own eco / away limits (None: the central value).
    eco_heating_limit: float | None = None
    eco_cooling_limit: float | None = None
    away_heating_limit: float | None = None
    away_cooling_limit: float | None = None
    # 0.10: one-off manual targets set on the thermostat (not configuration).
    manual_heating_limit: float | None = None
    manual_cooling_limit: float | None = None
    manual_band: str | None = None  # the band they were set in
    manual_until: str | None = None  # ISO time; set by the runtime
    # 0.13.5: a fixed fan speed chosen on the thermostat is temporary too.
    fan_until: str | None = None  # ISO time; set by the runtime
    # 0.13.5: off = Zonentra leaves the units' fan alone in this room.
    fan_control: bool = True
    # 0.13.6: last known HVAC modes and fan modes that every indoor unit of the
    # room supports. The thermostat always offers these, also while a unit is
    # (briefly) unavailable, so HomeKit and dashboards see stable features.
    unit_hvac_modes: tuple[str, ...] = ()
    unit_fan_modes: tuple[str, ...] = ()
    floor_regulation: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "Thermostat":
        """Tolerant restore: unknown or invalid stored values become defaults."""
        if not isinstance(data, dict):
            return cls()

        def number(key):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value) if 5 <= value <= 35 else None

        heat, cool = number("heating_limit"), number("cooling_limit")
        if heat is None or cool is None or heat >= cool:
            heat = cool = None
        manual = {}
        low, high = number("manual_heating_limit"), number("manual_cooling_limit")
        if low is not None and high is not None and low < high:
            if data.get("manual_band") in BANDS:
                until = data.get("manual_until")
                manual = {
                    "manual_heating_limit": low,
                    "manual_cooling_limit": high,
                    "manual_band": data["manual_band"],
                    "manual_until": until if isinstance(until, str) else None,
                }
        return cls(
            data.get("hvac_mode")
            if data.get("hvac_mode") in THERMOSTAT_MODES
            else "heat_cool",
            data.get("preset") if data.get("preset") in PRESETS else "comfort",
            heat,
            cool,
            data.get("regulation") is True,
            data.get("fan_mode") if isinstance(data.get("fan_mode"), str) else None,
            data.get("window_bypass") is True,
            data.get("screen_regulation") is True,
            *(number(key) for key in BAND_KEYS),
            **manual,
            fan_until=data.get("fan_until")
            if isinstance(data.get("fan_until"), str)
            else None,
            fan_control=data.get("fan_control") is not False,
            unit_hvac_modes=_names(data.get("unit_hvac_modes")),
            unit_fan_modes=_names(data.get("unit_fan_modes")),
            floor_regulation=data.get("floor_regulation") is True,
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def bands(self) -> dict[str, float]:
        """The room's own eco/away limits that are set."""
        return {
            key: getattr(self, key)
            for key in BAND_KEYS
            if getattr(self, key) is not None
        }

    @property
    def manual(self) -> bool:
        return self.manual_band is not None

    @property
    def has_band(self) -> bool:
        return self.heating_limit is not None and self.cooling_limit is not None


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


CLEAR_MANUAL = {
    "manual_heating_limit": None,
    "manual_cooling_limit": None,
    "manual_band": None,
    "manual_until": None,
}


def update_thermostat(
    current: Thermostat, hysteresis: float, **changes: Any
) -> Thermostat:
    """Validated change; raises ValueError with a translatable code."""
    if "hvac_mode" in changes and changes["hvac_mode"] not in THERMOSTAT_MODES:
        raise ValueError("invalid_hvac_mode")
    if "preset" in changes and changes["preset"] not in PRESETS:
        raise ValueError("invalid_preset")
    for key in (
        "regulation",
        "window_bypass",
        "screen_regulation",
        "fan_control",
        "floor_regulation",
    ):
        if key in changes and not isinstance(changes[key], bool):
            raise ValueError("invalid_parameters")
    for key in BAND_KEYS:
        value = changes.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 5 <= value <= 35
        ):
            raise ValueError("invalid_limits")
    if "fan_mode" in changes and not isinstance(changes["fan_mode"], str | None):
        raise ValueError("invalid_parameters")
    if "preset" in changes and "manual_band" not in changes:
        # Choosing a preset ends a manual deviation.
        changes = {**changes, **CLEAR_MANUAL}
    new = replace(current, **changes)
    if new.manual_band is not None:
        low, high = new.manual_heating_limit, new.manual_cooling_limit
        if (
            new.manual_band not in BANDS
            or low is None
            or high is None
            or not 5 <= low <= 35
            or not 5 <= high <= 35
            or low + 2 * hysteresis >= high
        ):
            raise ValueError("invalid_limits")
    if new.heating_limit is not None or new.cooling_limit is not None:
        if new.heating_limit is None or new.cooling_limit is None:
            raise ValueError("invalid_limits")
        for limit in (new.heating_limit, new.cooling_limit):
            if not 5 <= limit <= 35:
                raise ValueError("invalid_limits")
        if new.heating_limit + 2 * hysteresis >= new.cooling_limit:
            raise ValueError("invalid_limits")
    return new


def room_demand(hvac_mode: str, demand: ZoneDemand) -> ZoneDemand:
    """The room's demand as its thermostat mode allows it.

    ``off`` asks for nothing; ``heat`` drops cooling demand and ``cool`` drops
    heating demand (neutral, with the reason). Unknown demand stays unknown.
    """
    if hvac_mode in ("off", *UNIT_MODES):
        reason = f"thermostat_{hvac_mode}"
    elif hvac_mode in IGNORED_DEMAND and demand.demand == IGNORED_DEMAND[hvac_mode][0]:
        reason = IGNORED_DEMAND[hvac_mode][1]
    else:
        return demand
    return ZoneDemand(demand.zone_id, "neutral", 0.0, 0.0, reason, demand.sensor_health)


def single_target(
    hvac_mode: str,
    value: float,
    heating_limit: float | None,
    cooling_limit: float | None,
    hysteresis: float,
) -> tuple[float, float]:
    """A single target temperature (heat or cool mode) as a comfort band.

    ``heat`` sets the heating limit, ``cool`` the cooling limit. The other
    limit stays where it is when the band remains valid; otherwise it moves
    along with the band's current width (at least 2 °C).
    """
    width = (
        cooling_limit - heating_limit
        if heating_limit is not None and cooling_limit is not None
        else 0
    )
    width = max(width, 2.0, 2 * hysteresis + 0.5)
    if hvac_mode == "cool":
        low = heating_limit
        if low is None or low + 2 * hysteresis >= value:
            low = value - width
        return low, value
    high = cooling_limit
    if high is None or value + 2 * hysteresis >= high:
        high = value + width
    return value, high


def active_band(preset: str, occupancy: Occupancy) -> tuple[str, str]:
    """(band, reason). Building absence always wins, as in 0.8."""
    if occupancy.mode == "away" or preset == "away":
        return "away", occupancy.reason if occupancy.mode == "away" else "preset"
    if preset == "comfort":  # Comfort, lowered by presence and motion.
        return occupancy.mode, occupancy.reason
    return preset, "preset"


@dataclass(frozen=True)
class RoomUnitControl:
    """What room regulation did (or would do) for one indoor unit."""

    equipment_id: str
    status: str
    command: ModeCommand | None = None


def plan_room_unit(
    action: str,
    reason: str,
    equipment_id: str,
    entity_id: str,
    *,
    enabled: bool,
    safe_modes: frozenset[str],
    busy: bool,
) -> RoomUnitControl:
    """Execute a 0.5 unit intent: only switching on and off, never the mode.

    ``safe_modes`` are the modes the outdoor unit group allows right now;
    ``busy`` means a Zonentra command for this unit is still unconfirmed.
    """
    if not enabled:
        return RoomUnitControl(equipment_id, "simulation")
    if action in ("turn_on_heat", "turn_on_cool"):
        target = action.removeprefix("turn_on_")
        if busy:
            return RoomUnitControl(equipment_id, "waiting_for_confirmation")
        if target not in safe_modes:
            return RoomUnitControl(equipment_id, "blocked_outdoor_mode")
        return RoomUnitControl(
            equipment_id,
            action,
            ModeCommand(
                equipment_id, entity_id, "set_hvac_mode", target, 0, "room_demand"
            ),
        )
    if action == "turn_off":
        if busy:
            return RoomUnitControl(equipment_id, "waiting_for_confirmation")
        return RoomUnitControl(
            equipment_id,
            action,
            ModeCommand(equipment_id, entity_id, "turn_off", None, 0, reason),
        )
    if action == "correct_mode":
        return RoomUnitControl(equipment_id, "left_to_stand_control")
    return RoomUnitControl(equipment_id, action)  # hold / blocked


def plan_room_mode(
    mode: str,
    unit_mode: str,
    equipment_id: str,
    entity_id: str,
    *,
    enabled: bool,
    safe_modes: frozenset[str],
    busy: bool,
    applied: str | None,
) -> RoomUnitControl:
    """Thermostat in dry or fan_only: run the room's units in that mode.

    ``dry`` cools, so it needs a group that cools (or may cool); a unit that
    can no longer dry is switched off. ``fan_only`` fits both. Zonentra sets the mode once per thermostat change (``applied``):
    a person who later changes the unit at the room controller is not fought.
    A unit that is off is switched on again (a person switching it off starts
    a manual override, which the runtime respects first).
    """
    if not enabled:
        return RoomUnitControl(equipment_id, "simulation")
    if mode == "dry" and "cool" not in safe_modes:
        if unit_mode not in ("off", "unknown") and applied == mode and not busy:
            # The group went to heating and switched our drying unit along:
            # it cannot dry now, so it goes off instead of heating the room.
            return RoomUnitControl(
                equipment_id,
                "turn_off",
                ModeCommand(
                    equipment_id, entity_id, "turn_off", None, 0, "dry_not_possible"
                ),
            )
        return RoomUnitControl(equipment_id, "blocked_outdoor_mode")
    if unit_mode == mode:
        return RoomUnitControl(equipment_id, "hold")
    if unit_mode != "off" and applied == mode:
        return RoomUnitControl(equipment_id, "changed_at_unit")
    if busy:
        return RoomUnitControl(equipment_id, "waiting_for_confirmation")
    return RoomUnitControl(
        equipment_id,
        f"set_{mode}",
        ModeCommand(
            equipment_id, entity_id, "set_hvac_mode", mode, 0, "thermostat_mode"
        ),
    )


def plan_fan(
    desired: str | None,
    unit_mode: str,
    unit_fan: str | None,
    applied: str | None,
    supported: tuple[str, ...],
) -> str | None:
    """Fan speed to send to a running unit, once per thermostat change."""
    if (
        desired is None
        or desired not in supported
        or unit_mode in ("off", "unknown")
        or desired == unit_fan
        or desired == applied
    ):
        return None
    return desired


def safe_modes(running: tuple[str, ...], preset_mode: str | None) -> frozenset[str]:
    """Modes a unit may be switched on in without fighting the outdoor unit.

    With active stand control the preset mode is the outdoor mode. Without it,
    the units already running decide; nothing running allows both.
    """
    if preset_mode is not None:
        return (
            frozenset({preset_mode})
            if preset_mode in ("heat", "cool")
            else (frozenset())
        )
    leading = {
        "heat" if mode == "heat" else "cool"
        for mode in running
        if mode in ("heat", "cool", "dry")
    }
    if len(leading) > 1:
        return frozenset()
    return frozenset(leading) if leading else frozenset({"heat", "cool"})


@dataclass(frozen=True)
class SetpointPlan:
    status: str
    setpoint: float | None = None
    base: float | None = None
    offset: float | None = None
    next_check: datetime | None = None
    send: bool = False


def plan_setpoint(
    mode: str,
    heating_limit: float | None,
    cooling_limit: float | None,
    hysteresis: float,
    room_temperature: float | None,
    current_setpoint: float | None,
    last_sent_at: datetime | None,
    now: datetime,
    *,
    max_offset: float,
    interval_seconds: float,
    step: float | None = None,
    step_origin: float = 0,
    min_temp: float | None = None,
    max_temp: float | None = None,
    gain: float = 1.0,
) -> SetpointPlan:
    """Self-regulating setpoint for a running unit, steered on the room sensor.

    The base is the edge of the comfort band the room is heading for
    (heating limit + hysteresis, or cooling limit - hysteresis). The unit's
    own sensor sits in the unit, so the room error is added on top (default gain 1;
    the optional native trial uses gain 0.25),
    bounded by ``max_offset``: a cold room asks the unit for more, a room that
    is already warm enough for less. Changes are rounded to the unit's step
    and sent at most once per ``interval_seconds``.

    Values and step intervals are in Celsius. ``step_origin`` aligns the grid
    with the device's native zero after conversion (for example 0 °F).
    """
    if mode not in ("heat", "cool"):
        return SetpointPlan("unit_not_running")
    if current_setpoint is None:
        return SetpointPlan("no_setpoint_support")
    if room_temperature is None:
        return SetpointPlan("room_temperature_unknown")
    if heating_limit is None or cooling_limit is None:
        return SetpointPlan("limits_unknown")
    step = step if step and step > 0 else 0.5
    if mode == "heat":
        base = heating_limit + hysteresis
        offset = max(-max_offset, min(max_offset, gain * (base - room_temperature)))
        raw = base + offset
    else:
        base = cooling_limit - hysteresis
        offset = max(-max_offset, min(max_offset, gain * (room_temperature - base)))
        raw = base - offset
    low = min_temp if min_temp is not None else DEFAULT_MIN_SETPOINT
    high = max_temp if max_temp is not None else DEFAULT_MAX_SETPOINT
    # Keep conversion precision until service dispatch (for example whole °F
    # steps represented in °C). step_origin preserves the native unit's grid.
    rounded = step_origin + round((raw - step_origin) / step) * step
    setpoint = round(min(high, max(low, rounded)), 8)
    plan = SetpointPlan("in_sync", setpoint, round(base, 2), round(setpoint - base, 2))
    if abs(setpoint - current_setpoint) < step / 2:
        return plan
    if last_sent_at is not None:
        allowed = last_sent_at + timedelta(seconds=interval_seconds)
        if now < allowed:
            return replace(plan, status="interval", next_check=allowed)
    return replace(plan, status="setpoint_changed", send=True)
