"""Pilot mode: the assistant drives the mouse and keyboard, with a visible banner.

Started only with the user's approval (and the system's own permission on
Wayland); the user takes back control at any time with Escape, by moving the
mouse or with the banner's Stop button.
"""

from kreafinity.pilot.session import (
    ITERATIONS,
    PilotError,
    PilotSession,
    PilotStopped,
    end_request,
    get_session,
)

__all__ = ["ITERATIONS", "PilotError", "PilotSession", "PilotStopped", "end_request", "get_session"]
