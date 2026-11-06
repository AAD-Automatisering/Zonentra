"""Pure manual-override detection and command tracking; no HA imports.

Policy for this installation: an external switch-on of a linked indoor unit
(state ``off`` -> an active mode) is treated as a manual override. Home Assistant
state events do not prove who changed a state. The runtime uses HA contexts
to distinguish recognizable automations from users and room controllers.
Zonentra-initiated changes are excluded through the CommandTracker.

Overrides live in memory only and do not survive a Home Assistant restart: an
expired override can never come back, and a restart is never a transition.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

ACTIVE_MODES = ("heat", "cool", "heat_cool", "auto", "dry", "fan_only")
UNAVAILABLE = (None, "unknown", "unavailable")


@dataclass(frozen=True)
class ManualOverride:
    equipment_id: str
    started_at: datetime
    expires_at: datetime | None
    mode_at_start: str

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or now < self.expires_at

    def remaining_seconds(self, now: datetime) -> int | None:
        if self.expires_at is None:
            return None
        return max(0, round((self.expires_at - now).total_seconds()))


@dataclass(frozen=True)
class PendingCommand:
    """A change Zonentra itself requested, awaiting its expected state."""

    entity_id: str
    expected_state: str
    issued_at: datetime
    deadline: datetime


class CommandTracker:
    """Bounded record of Zonentra-initiated changes awaiting state confirmation.

    A state transition only counts as Zonentra-initiated when it matches the
    expected state of a pending command before that command's deadline. There
    is no open-ended time-based suppression: once confirmed or expired, the
    entry is gone and any later transition is external again.
    """

    def __init__(self, confirmation_seconds: float = 60) -> None:
        self.confirmation_seconds = confirmation_seconds
        self._pending: dict[str, PendingCommand] = {}

    @property
    def pending(self) -> tuple[PendingCommand, ...]:
        return tuple(self._pending.values())

    def register(self, entity_id: str, expected_state: str, now: datetime) -> None:
        self._pending[entity_id] = PendingCommand(
            entity_id,
            expected_state,
            now,
            now + timedelta(seconds=self.confirmation_seconds),
        )

    def is_pending(self, entity_id: str, now: datetime) -> bool:
        """A command for this entity still awaits its expected state."""
        self.expire(now)
        return entity_id in self._pending

    def expire(self, now: datetime) -> None:
        self._pending = {
            key: item for key, item in self._pending.items() if now <= item.deadline
        }

    def cancel(self, entity_id: str) -> None:
        """Forget a pending command (someone else took over the device)."""
        self._pending.pop(entity_id, None)

    def confirm(self, entity_id: str, new_state: str | None, now: datetime) -> bool:
        """Consume a matching pending command; True means Zonentra caused it."""
        self.expire(now)
        item = self._pending.get(entity_id)
        if item and item.expected_state == new_state:
            del self._pending[entity_id]
            return True
        return False


def classify_transition(
    old_state: str | None,
    new_state: str | None,
    *,
    old_present: bool = True,
    trusted: bool = True,
    zonentra_initiated: bool = False,
    origin: str = "device",
) -> str:
    """Classify one HA state_changed event of a linked indoor unit."""
    if not old_present:
        return "initial"  # Entity appeared (startup or newly created).
    if old_state in UNAVAILABLE:
        return "from_unavailable"  # Never a manual switch-on.
    if new_state in UNAVAILABLE:
        return "to_unavailable"
    if old_state == new_state:
        return "attribute_update"
    if zonentra_initiated:
        return "zonentra_initiated"
    if not trusted:
        return "untrusted"  # restored or assumed state
    if old_state == "off" and new_state in ACTIVE_MODES:
        # Automations and scripts are not people at a room controller.
        return "automation_on" if origin == "automation" else "manual_on"
    if old_state in ACTIVE_MODES and new_state == "off":
        return "manual_off"
    if old_state in ACTIVE_MODES and new_state in ACTIVE_MODES:
        return "mode_change"
    return "ignored"


def update_override(
    current: ManualOverride | None,
    transition: str | None,
    equipment_id: str,
    new_state: str | None,
    now: datetime,
    *,
    enabled: bool,
    duration_minutes: float,
    regulated: bool = False,
) -> tuple[ManualOverride | None, str | None]:
    """Apply expiry and one transition; returns (override, event code).

    ``regulated`` (0.9 room regulation active for the unit's room): a person
    switching the unit off, or changing its setpoint, is respected for the
    override duration too, instead of being undone by Zonentra.
    """
    event = None
    if current and not current.active(now):
        current, event = None, "override_expired"
    if regulated and transition in ("manual_off", "manual_setpoint"):
        if not enabled:
            return None, "override_disabled"
        mode = "off" if transition == "manual_off" else new_state or "unknown"
        return (
            ManualOverride(
                equipment_id, now, now + timedelta(minutes=duration_minutes), mode
            ),
            "override_started",
        )
    if transition == "manual_on":
        if not enabled:
            return None, "override_disabled"
        # Only an off -> on transition starts (or restarts) an override, so a
        # state update or a mode correction can never extend the timer.
        return (
            ManualOverride(
                equipment_id,
                now,
                now + timedelta(minutes=duration_minutes),
                new_state or "unknown",
            ),
            "override_started",
        )
    if transition == "manual_off" and current:
        return None, "override_ended_manual_off"
    return current, event
