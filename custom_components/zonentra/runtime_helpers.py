"""Shared state confirmation and event-origin helpers for runtime controls."""


def _setpoint(value) -> str | None:
    try:
        return f"{float(value):.1f}"
    except TypeError, ValueError:
        return None


def _origin(event) -> str:
    """Who caused a state change, as far as Home Assistant can tell.

    Device reports can have a bare context. Automation and script runs can
    carry a parent context, and Home Assistant actions can carry a user.
    """
    context = getattr(event, "context", None)
    if context is not None and context.user_id:
        return "ha_user"
    if context is not None and context.parent_id:
        return "automation"
    return "device"
