"""Push-to-talk: hold a key (default space) to make Jarvis listen — no wake word.

A `pynput` keyboard listener runs in its own thread and reports key down/up across the
whole desktop, so you can trigger Jarvis while focused anywhere. Transitions are marshalled
onto the asyncio loop via `call_soon_threadsafe`, exposing `wait_press()` / `wait_release()`
the conversation loop can await, plus an `is_down` snapshot.

Note: monitoring the spacebar globally means a held space will also type spaces into whatever
text field currently has focus. Pick a non-typing key (e.g. "ctrl_r") via config `ptt_key` if
that bothers you. We do not suppress the key (that would break normal typing everywhere).
"""

from __future__ import annotations

import asyncio


class PushToTalk:
    def __init__(self, key: str = "space") -> None:
        self._keyname = (key or "space").strip().lower()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener = None
        self._target = None
        self._down = False
        self._press_waiters: list[asyncio.Future] = []
        self._release_waiters: list[asyncio.Future] = []

    @property
    def is_down(self) -> bool:
        return self._down

    # ------------------------------------------------------------------ lifecycle
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        from pynput import keyboard  # imported lazily so text mode never needs it

        self._loop = loop
        self._target = self._resolve_key(keyboard, self._keyname)
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    @staticmethod
    def _resolve_key(keyboard, name: str):
        """Map a config key name to a pynput key (a special Key or a character)."""
        special = getattr(keyboard.Key, name, None)
        if special is not None:
            return special
        # single character key (e.g. "`" or a letter)
        return name[:1] if name else " "

    def _matches(self, key) -> bool:
        if key == self._target:
            return True
        # character keys arrive as KeyCode with a `.char`
        char = getattr(key, "char", None)
        return isinstance(self._target, str) and char == self._target

    # -------------------------------------------------------------- pynput thread
    def _on_press(self, key) -> None:
        if self._matches(key) and not self._down:
            self._down = True
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._resolve, True)

    def _on_release(self, key) -> None:
        if self._matches(key) and self._down:
            self._down = False
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._resolve, False)

    # ----------------------------------------------------------------- loop side
    def _resolve(self, pressed: bool) -> None:
        waiters = self._press_waiters if pressed else self._release_waiters
        if pressed:
            self._press_waiters = []
        else:
            self._release_waiters = []
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)

    async def wait_press(self) -> None:
        if self._down:
            return
        fut = self._loop.create_future()
        self._press_waiters.append(fut)
        try:
            await fut
        finally:
            if fut in self._press_waiters:
                self._press_waiters.remove(fut)

    async def wait_release(self) -> None:
        if not self._down:
            return
        fut = self._loop.create_future()
        self._release_waiters.append(fut)
        try:
            await fut
        finally:
            if fut in self._release_waiters:
                self._release_waiters.remove(fut)
