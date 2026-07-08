"""Jarvis's state machine and shared status snapshot.

Every mutation emits a `status` event on the bus, so the REPL (now) and the
dashboard (Phase 2) always reflect the live state without polling.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

from .eventbus import EventBus


class JarvisState(str, enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    WORKING = "working"
    SLEEPING = "sleeping"


@dataclasses.dataclass
class StatusSnapshot:
    state: str = JarvisState.IDLE.value
    detail: str = ""
    current_tool: str | None = None
    waiting_for: str | None = None
    model: str | None = None
    session_id: str | None = None
    tools_used: int = 0  # running count of tool calls this session (for the stats panel)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class StateStore:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.status = StatusSnapshot()

    # -- mutations (each publishes a status event) --------------------------- #
    def set_state(self, state: str | JarvisState, detail: str | None = None) -> None:
        self.status.state = state.value if isinstance(state, JarvisState) else state
        if detail is not None:
            self.status.detail = detail
        self._publish()

    def set_tool(self, tool: str | None) -> None:
        self.status.current_tool = tool
        self._publish()

    def bump_tools(self) -> None:
        """Count one tool call (published so the dashboard stats stay live)."""
        self.status.tools_used += 1
        self._publish()

    def set_waiting(self, waiting_for: str | None) -> None:
        self.status.waiting_for = waiting_for
        self._publish()

    def set_session(self, session_id: str | None) -> None:
        self.status.session_id = session_id
        self._publish()

    def set_model(self, model: str | None) -> None:
        self.status.model = model
        self._publish()

    def _publish(self) -> None:
        self._bus.emit("status", **self.status.as_dict())
