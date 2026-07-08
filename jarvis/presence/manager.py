"""Presence + heartbeat — deliver Jarvis's queued notes, and never sleep forever.

An independent asyncio task ticks every `heartbeat_minutes`. On each tick, if Jarvis has
queued anything to say (via `notify_user`), it's delivered to whoever can hear it:

  * if a voice presence-check is wired and the user answers  → spoken aloud (Slice 2);
  * otherwise, if Telegram is configured                     → sent there.

Because the heartbeat is its own task, a long or stuck brain turn can never stop the wake —
that's the "never sleeps forever" guarantee. A second, lighter task watches the bus so a
*high*-importance note goes out immediately instead of waiting up to a full interval.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ..context import PendingNote


class PresenceManager:
    def __init__(self, orch, telegram=None, voice_link=None) -> None:
        self.orch = orch
        self.ctx = orch.ctx
        self.bus = orch.bus
        self.telegram = telegram
        self._voice_link = voice_link
        self._interval = max(60, int(orch.config.heartbeat_minutes) * 60)
        # Don't consider voice if the user's last local activity is older than this.
        self._presence_max = float(orch.config.voice.presence_max_seconds)
        # How long to wait for the (possibly mid-turn) voice loop to service a delivery.
        self._voice_timeout = max(120.0, float(orch.config.presence_listen_seconds) + 90.0)
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._consume_task: Optional[asyncio.Task] = None
        self._queue = None  # bus subscription for prompt delivery

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        if self.telegram is not None or self._voice_link is not None:
            self._queue = self.bus.subscribe()
            self._consume_task = asyncio.ensure_future(self._consume())

    async def stop(self) -> None:
        for task in (self._heartbeat_task, self._consume_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._heartbeat_task = self._consume_task = None
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
            self._queue = None

    # ------------------------------------------------------------------ heartbeat
    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self._tick()
                except Exception as exc:
                    self.bus.emit("error", where="heartbeat", message=str(exc))
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        self.bus.emit("heartbeat", pending=len(self.ctx.peek_notes()))
        if not self.ctx.peek_notes():
            return
        await self._deliver_pending()

    async def _deliver_pending(self) -> None:
        """Route queued notes: voice if he might be at the machine, else Telegram.

        The voice loop confirms presence ("are you still here?") and speaks the notes; if it
        reports back that the room was silent (or there's no loop), we fall through to Telegram.
        """
        notes = self.ctx.drain_notes()
        if not notes:
            return
        if (
            self._voice_link is not None
            and self._voice_link.attached
            and self._maybe_present()
        ):
            try:
                delivered = await asyncio.wait_for(
                    self._voice_link.request(notes), timeout=self._voice_timeout
                )
            except (asyncio.TimeoutError, Exception):
                delivered = False
            if delivered:
                self.bus.emit("delivered", channel="voice", count=len(notes))
                return
        if self.telegram is not None and self.telegram.running:
            if await self._send_telegram(notes):
                return
        # Nothing could deliver right now — keep the notes for the next attempt.
        self.ctx.pending_notes[:0] = notes

    def _maybe_present(self) -> bool:
        """True if the user was active locally recently enough to be worth a voice check."""
        since = self.ctx.seconds_since_active()
        return since is not None and since < self._presence_max

    # ------------------------------------------------------------------ immediate
    async def _consume(self) -> None:
        """Deliver notes the moment Jarvis queues one, instead of waiting for the tick.

        `notify_user` is the brain's deliberate "this is worth {the user's} attention" signal —
        a finished task or something he asked to be told about — so it should reach him
        promptly (within seconds), not up to a heartbeat later. The periodic tick remains a
        backstop for anything still queued (e.g. Telegram was momentarily down).
        """
        assert self._queue is not None
        try:
            while True:
                event = await self._queue.get()
                if event.type != "notify":
                    continue
                if self.ctx.peek_notes():
                    await self._deliver_pending()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ delivery
    async def _send_telegram(self, notes: list[PendingNote]) -> bool:
        # Retention on failure is handled by the caller (_deliver_pending).
        if not notes:
            return False
        sent = await self.telegram.send(self._format(notes))
        if sent:
            self.bus.emit("delivered", channel="telegram", count=len(notes))
        return sent

    @staticmethod
    def _format(notes: list[PendingNote]) -> str:
        if len(notes) == 1:
            return notes[0].message
        return "\n".join(f"• {n.message}" for n in notes)
