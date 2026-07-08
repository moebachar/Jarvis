"""The cinematic audio layer: the boot power-up and a couple of attention cues.

Deliberately minimal. Jarvis used to sprinkle a synthesized "blip" around every tool
call and speak canned lines ("Searching the codebase, sir.") — both were noise. Those
are gone. Progress is now narrated by Jarvis himself, out loud, in his own words (see the
persona's narrate_work instruction), so this layer only owns:
  * the boot power-up ambience + spoken welcome, and
  * a short cue when he notifies you or hits an error.

`AudioFX` subscribes to the same EventBus the brain/state publish on. Everything degrades
gracefully: no audio deps -> no-op; no ElevenLabs key -> the boot bed still plays, silent.
"""

from __future__ import annotations

import asyncio

from ..config import global_dir
from .sfx import SoundPlayer, build_sound_bank
from .voicebank import VoiceBank


class AudioFX:
    def __init__(self, ctx, *, voice_mode: bool = False) -> None:
        self.ctx = ctx
        self.cfg = ctx.config.voice
        self.bus = ctx.bus
        self.voice_mode = voice_mode

        self.player = SoundPlayer(
            sample_rate=16000, device=self.cfg.output_device, master=0.9
        )
        self.bank = VoiceBank(self.cfg, cache_dir=global_dir() / "cache" / "voice")
        self.sounds = build_sound_bank(16000)

        self._sub = None
        self._task = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.cfg.sfx_enabled:
            try:
                self.player.start()
            except Exception as exc:
                self.bus.emit("error", where="sfx", message=f"audio output unavailable: {exc}")
        self._sub = self.bus.subscribe()
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._sub is not None:
            self.bus.unsubscribe(self._sub)
        self.player.stop()

    async def boot(self) -> None:
        """Power-up ambience under a spoken welcome line. Best-effort; never raises."""
        if not self.cfg.boot_sound or not self._started:
            return
        try:
            if self.cfg.sfx_enabled:
                self.player.play(self.sounds.get("boot"), gain=self.cfg.sfx_volume * 1.3)
            if self.bank.available and self.cfg.boot_line:
                arr = await asyncio.to_thread(
                    self.bank.ensure, self.cfg.boot_line, self.cfg.boot_speed
                )
                if arr is not None:
                    await asyncio.sleep(0.8)  # let the bed swell before he speaks
                    self.player.play(arr, gain=self.cfg.voice_line_volume)
        except Exception as exc:
            self.bus.emit("error", where="boot", message=str(exc))

    # -- event loop --------------------------------------------------------- #
    async def _consume(self) -> None:
        try:
            while True:
                ev = await self._sub.get()
                if not self.cfg.sfx_enabled:
                    continue
                if ev.type == "notify":
                    self.player.play(self.sounds.get("notify"), gain=self.cfg.sfx_volume)
                elif ev.type == "error" and ev.data.get("where") not in {"sfx", "boot"}:
                    self.player.play(self.sounds.get("error"), gain=self.cfg.sfx_volume)
        except asyncio.CancelledError:
            pass
