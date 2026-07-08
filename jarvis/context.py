"""Shared runtime context passed to the brain, tools, and hooks.

Holds the config, event bus, state store, project directory, and the queue of
things Jarvis wants to say (consumed by voice/Telegram in Phase 3). Bundling these
into one object keeps tool/hook closures clean and avoids global state.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import time
from pathlib import Path

from .config import JarvisConfig
from .eventbus import EventBus
from .state import StateStore


@dataclasses.dataclass
class PendingNote:
    message: str
    importance: str = "normal"


class RuntimeContext:
    def __init__(
        self,
        config: JarvisConfig,
        bus: EventBus,
        state: StateStore,
        project_dir: Path,
    ) -> None:
        self.config = config
        self.bus = bus
        self.state = state
        self.project_dir = Path(project_dir)
        # The running event loop, set by the orchestrator at start(); lets worker
        # threads (TTS playback, mic listener) post events safely.
        self.loop: asyncio.AbstractEventLoop | None = None
        # Things Jarvis has decided to tell the user; drained by the presence
        # layer (Phase 3) and, in the meantime, surfaced in the REPL via the bus.
        self.pending_notes: list[PendingNote] = []
        # When the user last interacted *locally* (voice / REPL), as a monotonic
        # timestamp. Drives presence: the heartbeat uses it to decide whether to
        # reach the user by voice (they're here) or Telegram (they're away).
        # Remote Telegram turns deliberately do NOT count as local presence.
        self._last_active: float | None = None
        # The canvas item the user has currently *selected* (focused) on the board,
        # so a turn like "make this bigger" / "explain this" knows what "this" is.
        # Set from the dashboard over the WebSocket; injected into the brain turn.
        self.focused_canvas: dict | None = None
        # A live browser session (lazily created by the `browse` tool) for the web-browsing
        # fallback; closed by the orchestrator on shutdown. Typed loosely to avoid importing
        # the (optional) web/playwright module here.
        self.browser = None
        # Remote/tunneled voice: the bridge between the dashboard WebSocket and the voice
        # loop's audio (a RemoteAudioHub, set by the orchestrator in browser-transport mode).
        # The dashboard server reads this to route mic PCM in and TTS PCM out. None otherwise.
        self.remote_audio = None

    def notify(self, message: str, importance: str = "normal") -> None:
        """Queue a short message for the user and announce it on the bus."""
        self.pending_notes.append(PendingNote(message, importance))
        self.bus.emit("notify", message=message, importance=importance)

    def peek_notes(self) -> list[PendingNote]:
        """Look at the queued notes without consuming them."""
        return list(self.pending_notes)

    def drain_notes(self) -> list[PendingNote]:
        notes, self.pending_notes = self.pending_notes, []
        return notes

    def mark_user_active(self) -> None:
        """Record that the user just interacted locally (voice or terminal)."""
        self._last_active = time.monotonic()

    def seconds_since_active(self) -> float | None:
        """Seconds since the last local interaction, or None if there's been none."""
        if self._last_active is None:
            return None
        return time.monotonic() - self._last_active

    def set_focused_canvas(self, item: dict | None) -> None:
        """Record (or clear) the canvas item the user has selected on the board."""
        self.focused_canvas = item

    def canvas_focus_hint(self) -> str | None:
        """A short note describing the focused canvas item, for the brain — or None.

        Lets the user say "make this bigger", "explain this", "redo it as a chart"
        and have Jarvis know which board item "this" refers to.
        """
        item = self.focused_canvas
        if not item:
            return None
        kind = item.get("kind") or "item"
        title = (item.get("title") or "").strip()
        content = (item.get("content") or "").strip()
        if len(content) > 600:
            content = content[:600] + " …(truncated)"
        label = f'titled "{title}"' if title else "(untitled)"
        hint = f"The user has selected a {kind} card {label} on the dashboard board."
        if content:
            hint += f" Its content is:\n{content}"
        return hint

    def post_event(self, event_type: str, **data) -> None:
        """Emit a bus event safely from any thread (e.g. audio worker threads)."""
        loop = self.loop
        if loop is None:
            self.bus.emit(event_type, **data)
            return
        loop.call_soon_threadsafe(functools.partial(self.bus.emit, event_type, **data))
