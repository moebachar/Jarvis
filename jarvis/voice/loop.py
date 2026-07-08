"""The voice conversation loop.

Flow:
  idle ──"jarvis" OR hold push-to-talk──► listen ─► transcribe ─► think ─► speak
                                          ▲                                  │
                                          └────────── follow-up window ◄─────┘

Two ways to start talking:
  * say the wake word "jarvis", or
  * hold the push-to-talk key (default space) and speak while held — no wake word.

Barge-in: while Jarvis is speaking, saying "jarvis" again (or pressing push-to-talk)
cuts him off (stops audio + interrupts the brain) and captures your new command. After
a reply he keeps listening for a few seconds so you can simply continue.
"""

from __future__ import annotations

import asyncio

from ..state import JarvisState
from .chunker import SentenceChunker
from .tts import describe_tts_error, play_activation_chime, play_tone


class VoiceConversation:
    def __init__(self, orchestrator, listener, transcriber, speaker, config, ptt=None,
                 voice_link=None) -> None:
        self.orch = orchestrator
        self.listener = listener
        self.stt = transcriber
        self.speaker = speaker
        self.cfg = config
        self.ptt = ptt
        self._voice_link = voice_link
        self.state = orchestrator.state
        self.bus = orchestrator.bus
        self._wake_enabled = config.voice.wake_enabled
        self._wake_phrase = config.voice.wake_phrase
        self._ptt_key = config.voice.ptt_key
        self._ack = config.voice.ack_sound
        self._ack_volume = config.voice.ack_volume
        self._follow_up = config.voice.follow_up_seconds
        self._out_device = config.voice.output_device
        # Flips true once ElevenLabs reports the account is out of credits, so we stop
        # re-hitting the dead quota on every sentence and degrade to text-only for the
        # rest of the session (the reply still streams to the feed / REPL, just unspoken).
        self._tts_disabled = False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.listener.start(loop)
        if self.ptt is not None:
            try:
                self.ptt.start(loop)
            except Exception as exc:
                self.bus.emit("error", where="ptt", message=f"push-to-talk unavailable: {exc}")
                self.ptt = None
        if self._voice_link is not None:
            self._voice_link.attach()  # the presence manager may now borrow the mic/speaker
        self.bus.emit("voice", event="ready", threshold=self.listener.threshold)
        self._idle()
        try:
            while True:
                trigger = await self._wait_trigger()
                if trigger == "ptt":
                    await self._handle_ptt()
                elif trigger == "deliver":
                    await self._handle_delivery()
                else:
                    await self._handle_wake()
        finally:
            if self._voice_link is not None:
                self._voice_link.detach()
            self.listener.stop()
            if self.ptt is not None:
                self.ptt.stop()

    # ------------------------------------------------------------------ triggers
    async def _wait_trigger(self) -> str:
        """Block until push-to-talk is pressed (and, if the wake word is enabled, until it
        fires) or — only while we're idle here between turns — the presence manager asks us
        to deliver a note."""
        tasks = {}
        if self._wake_enabled:
            wake = asyncio.ensure_future(self.listener.wait_for_wake())
            tasks[wake] = "wake"
        if self.ptt is not None:
            press = asyncio.ensure_future(self.ptt.wait_press())
            tasks[press] = "ptt"
        if self._voice_link is not None:
            deliver = asyncio.ensure_future(self._voice_link.wait())
            tasks[deliver] = "deliver"
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass
        winner = next(t for t in tasks if t in done)
        return tasks[winner]

    async def _handle_delivery(self) -> None:
        """Deliver notes the presence manager handed us, reporting whether we reached him."""
        notes = self._voice_link.notes or []
        delivered = False
        try:
            delivered = await self._presence_deliver(notes)
        except Exception as exc:
            self.bus.emit("error", where="presence_deliver", message=str(exc))
        finally:
            self._voice_link.resolve(delivered)
        self._idle()

    async def _presence_deliver(self, notes) -> bool:
        """Speak the queued notes — first confirming he's here if we're not sure.

        Returns True if delivered by voice, False if the room was silent (so the manager
        falls back to Telegram). If he spoke very recently we skip the question entirely.
        """
        if not notes:
            return True
        # No audio sink to reach him (e.g. remote mode with no browser tab connected) → let
        # Telegram handle it rather than "speaking" into a void and counting it delivered.
        if not getattr(self.speaker, "can_speak", True):
            return False
        v = self.cfg.voice
        since = self.orch.ctx.seconds_since_active()
        present = since is not None and since <= v.presence_fresh_seconds
        # Remote (browser) mode has no open mic, so we can't hear an answer to the "still here?"
        # prompt — asking it would be pointless. A connected tab (checked above) is our presence
        # signal there; only the local sounddevice path asks and listens.
        if not present and v.transport != "browser":
            await self._speak(v.presence_prompt)
            answer = await self._listen(timeout=float(self.cfg.presence_listen_seconds))
            if not answer:
                return False  # silence — he's away; let Telegram handle it
        for note in notes:
            await self._speak(note.message)
        return True

    async def _handle_wake(self) -> None:
        await self._chime()
        text = await self._listen()
        if not text:
            self.bus.emit("voice", event="missed")  # heard the wake word but caught no command
            self._idle()
            return
        await self._converse(text)

    async def _handle_ptt(self) -> None:
        text = await self._ptt_capture()
        if not text:
            self.bus.emit("voice", event="missed")
            self._idle()
            return
        await self._converse(text)

    # ------------------------------------------------------------------ helpers
    def _idle(self) -> None:
        if self._wake_enabled:
            detail = f"Listening for '{self._wake_phrase}'"
        else:
            detail = f"Hold {self._ptt_key} to talk"
        self.state.set_state(JarvisState.IDLE, detail)

    async def _chime(self) -> None:
        """The 'I'm listening' activation sound, played right after the wake word."""
        if self._ack:
            await asyncio.to_thread(
                play_activation_chime, 16000, self._ack_volume, self._out_device
            )

    async def _tone(self, frequency: float) -> None:
        if self._ack:
            await asyncio.to_thread(play_tone, frequency, 0.12, 16000, 0.25, self._out_device)

    async def _listen(self, timeout: float | None = None) -> str:
        self.state.set_state(JarvisState.LISTENING, "Listening")
        audio = await self.listener.record_utterance(timeout=timeout)
        if audio is None:
            return ""
        self.state.set_state(JarvisState.THINKING, "Transcribing")
        text = (await asyncio.to_thread(self.stt.transcribe, audio)).strip()
        if text:
            self.orch.ctx.mark_user_active()  # local presence: you're here, at the mic
            self.bus.emit("heard", text=text)
        return text

    async def _ptt_capture(self) -> str:
        """Capture audio for as long as the push-to-talk key is held."""
        if self._ack:
            asyncio.create_task(self._tone(990.0))  # brief ack, don't delay capture
        self.state.set_state(JarvisState.LISTENING, "Listening (hold to talk)")
        self.listener.start_ptt()
        await self.ptt.wait_release()
        audio = self.listener.stop_ptt()
        if audio is None or len(audio) < int(0.2 * 16000):  # too brief to be speech
            return ""
        self.state.set_state(JarvisState.THINKING, "Transcribing")
        text = (await asyncio.to_thread(self.stt.transcribe, audio)).strip()
        if text:
            self.orch.ctx.mark_user_active()  # local presence: you're here, at the keyboard
            self.bus.emit("heard", text=text)
        return text

    async def _capture_after_barge(self) -> str:
        """After an interruption, capture however the user signalled it."""
        if self.ptt is not None and self.ptt.is_down:
            return await self._ptt_capture()
        return await self._listen()

    def _with_context(self, text: str, interrupted_context: str | None) -> str:
        if not interrupted_context:
            return text
        # Tell the brain what the user actually heard before cutting in, so it can
        # clarify or course-correct rather than blindly restarting.
        return (
            "[The user interrupted you mid-sentence. You had said aloud so far: "
            f"\"{interrupted_context[-320:]}\". They cut in to say the following — "
            "respond to it directly, and if their interruption suggests they want you "
            "to stop, correct course, or missed something, address that:] " + text
        )

    async def _converse(self, text: str) -> None:
        interrupted_context: str | None = None
        while text:
            prompt = self._with_context(text, interrupted_context)
            barged, spoken = await self._respond(prompt)
            if not barged:
                interrupted_context = None
                # Open-mic follow-up only when the wake word is on. In button-only mode we
                # never listen without a press, so we return to idle and wait for the key.
                text = await self._listen(timeout=self._follow_up) if self._wake_enabled else ""
            else:
                await self._tone(660.0)
                interrupted_context = spoken
                text = await self._capture_after_barge()
        self._idle()

    async def _respond(self, text: str) -> tuple[bool, str]:
        """Stream the brain's reply to speech.

        Returns (barged_in, spoken_text) — spoken_text is what was actually voiced, so the
        caller can hand it back as context if the user interrupted.
        """
        self.state.set_state(JarvisState.THINKING, "Composing a reply")
        barged = asyncio.Event()

        def on_barge() -> None:
            if not barged.is_set():
                barged.set()
                self.speaker.stop()
                asyncio.create_task(self._safe_interrupt())

        # Voice barge-in (say the wake word to cut in) only when the wake word is enabled;
        # in button-only mode a push-to-talk press is the sole way to interrupt.
        if self._wake_enabled:
            self.listener.start_monitor(on_barge)
        ptt_watch = (
            asyncio.ensure_future(self._watch_ptt_barge(on_barge))
            if self.ptt is not None else None
        )
        chunker = SentenceChunker()
        spoken: list[str] = []
        try:
            # Hold the brain lock for the streamed turn so an inbound Telegram message
            # can't interleave on the shared session mid-reply (it waits its turn).
            async with self.orch.ask_lock:
                async for chunk in self.orch.ask(text):
                    if barged.is_set():
                        continue  # keep draining so the brain session ends cleanly
                    for sentence in chunker.add(chunk):
                        if barged.is_set():
                            break
                        await self._speak(sentence)
                        spoken.append(sentence)
                if not barged.is_set():
                    tail = chunker.flush()
                    if tail:
                        await self._speak(tail)
                        spoken.append(tail)
        except Exception as exc:  # never let one turn kill the loop
            self.bus.emit("error", where="respond", message=str(exc))
        finally:
            if self._wake_enabled:
                self.listener.stop_monitor()
            if ptt_watch is not None:
                ptt_watch.cancel()
                try:
                    await ptt_watch
                except (asyncio.CancelledError, Exception):
                    pass
        return barged.is_set(), " ".join(spoken)

    async def _watch_ptt_barge(self, on_barge) -> None:
        """While speaking, a push-to-talk press also interrupts (then we capture it)."""
        try:
            await self.ptt.wait_press()
            on_barge()
        except asyncio.CancelledError:
            pass

    async def _speak(self, sentence: str) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        self.state.set_state(JarvisState.SPEAKING, sentence[:60])
        self.bus.emit("said", text=sentence)
        if self._tts_disabled:
            return  # voice is unavailable this session; the text still reaches the feed
        try:
            await asyncio.to_thread(self.speaker.speak, sentence)
        except Exception as exc:
            message, is_quota = describe_tts_error(exc)
            if is_quota:
                # Report once, then go quiet — don't re-hit the dead quota per sentence.
                self._tts_disabled = True
            self.bus.emit("error", where="tts", message=message)

    async def _safe_interrupt(self) -> None:
        try:
            await self.orch.brain.interrupt()
        except Exception:
            pass
