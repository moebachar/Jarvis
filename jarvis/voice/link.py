"""VoiceLink — a one-slot rendezvous that lets the presence manager borrow the voice loop.

The voice conversation loop owns the mic and speaker. When the presence manager wants Jarvis
to *speak* a pending note (or ask "are you still here?"), it can't grab those devices itself —
that would collide with an in-progress turn. Instead it hands the notes to this link; the loop
picks them up only when it's idle between turns, does the talking/listening, and reports back
whether the user was reached. If no loop is attached (text mode) the request resolves to False
immediately, so the manager falls straight through to Telegram.
"""

from __future__ import annotations

import asyncio


class VoiceLink:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.notes: list | None = None       # the request payload, read by the loop
        self._fut: asyncio.Future | None = None
        self.attached = False                 # True while a live voice loop is servicing

    # -- voice-loop side ----------------------------------------------------- #
    def attach(self) -> None:
        self.attached = True

    def detach(self) -> None:
        """Loop is going away — resolve any in-flight request as undelivered."""
        self.attached = False
        self._resolve(False)

    async def wait(self) -> list:
        """Block until the manager posts a request; returns the notes to deliver."""
        await self._event.wait()
        return self.notes or []

    def resolve(self, delivered: bool) -> None:
        """Report whether the notes were spoken to (and acknowledged by) the user."""
        self._resolve(delivered)

    # -- manager side -------------------------------------------------------- #
    async def request(self, notes: list) -> bool:
        """Ask the loop to deliver `notes` by voice. Returns True if it reached the user."""
        if not self.attached:
            return False
        loop = asyncio.get_running_loop()
        self._fut = loop.create_future()
        self.notes = notes
        self._event.set()
        try:
            return await self._fut
        finally:
            self.notes = None
            self._fut = None
            self._event.clear()

    # -- internal ------------------------------------------------------------ #
    def _resolve(self, delivered: bool) -> None:
        self._event.clear()
        if self._fut is not None and not self._fut.done():
            self._fut.set_result(delivered)
