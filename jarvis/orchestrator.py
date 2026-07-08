"""The orchestrator wires the components together and supervises Jarvis.

In Phase 0 it owns the event bus, state store, runtime context, and brain, and
exposes a simple `ask()` stream. Later phases attach the voice loop, dashboard
server, scheduler, and Telegram bot to this same object.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from .brain.agent import JarvisBrain
from .config import JarvisConfig
from .context import RuntimeContext
from .eventbus import EventBus
from .state import JarvisState, StateStore


class VoiceConfigError(RuntimeError):
    """Raised when voice mode is requested but required keys are missing."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("missing voice configuration: " + "; ".join(missing))


class Orchestrator:
    def __init__(
        self,
        config: JarvisConfig,
        project_dir: str | Path,
        resume: bool = False,
        voice_mode: bool = False,
    ) -> None:
        self.config = config
        self.project_dir = Path(project_dir)
        self.voice_mode = voice_mode
        self.bus = EventBus()
        self.state = StateStore(self.bus)
        self.ctx = RuntimeContext(config, self.bus, self.state, self.project_dir)
        self.brain = JarvisBrain(self.ctx, resume=resume, voice_mode=voice_mode)
        self.dashboard = None
        # Voice mode only: the rendezvous letting the presence layer borrow the mic/speaker
        # to speak notes / run the "are you still here?" check (see voice/link.py).
        self.voice_link = None
        if voice_mode:
            try:
                from .voice.link import VoiceLink

                self.voice_link = VoiceLink()
            except Exception:
                self.voice_link = None
        # Phase 3: presence (heartbeat) + Telegram bridge, started in start() when configured.
        self.telegram = None
        self.presence = None
        # One brain, one session — serialize turns so a Telegram message and a voice/REPL
        # turn can never interleave on the shared ClaudeSDKClient and corrupt the stream.
        self.ask_lock = asyncio.Lock()
        # The local faster-whisper model, lazily built and shared by voice mode and inbound
        # Telegram voice notes (so the model loads at most once).
        self.transcriber = None
        self._transcriber_lock = asyncio.Lock()
        # The cinematic audio layer (SFX + spoken progress). Optional: if the audio
        # deps aren't installed (lean text-only install), it simply stays absent.
        self.audiofx = None
        try:
            from .voice.audiofx import AudioFX

            self.audiofx = AudioFX(self.ctx, voice_mode=voice_mode)
        except Exception:
            self.audiofx = None

    async def start(self) -> None:
        self.ctx.loop = asyncio.get_running_loop()
        # Remote/tunneled voice: create the audio hub BEFORE the dashboard starts, so a browser
        # tab that connects during model load already sees remote mode (and can claim the audio
        # slot) instead of being told there's no remote audio.
        if self.voice_mode and self.config.voice.transport == "browser":
            from .voice.remote import RemoteAudioHub

            self.ctx.remote_audio = RemoteAudioHub(self.ctx)
        if self.config.claude.model:
            self.state.set_model(self.config.claude.model)
        await self.brain.start()
        if self.audiofx is not None:
            try:
                await self.audiofx.start()
            except Exception as exc:
                self.bus.emit("error", where="audiofx", message=str(exc))
                self.audiofx = None
        await self._maybe_start_presence()

    async def _maybe_start_presence(self) -> None:
        """Start the presence layer (heartbeat + note routing).

        Runs when there's at least one away/voice channel to deliver through: a configured
        Telegram bot, and/or voice mode (so queued notes can be spoken / the "are you still
        here?" check can run). With neither there's nothing to deliver, so we skip it.
        """
        tg = self.config.telegram
        has_telegram = bool(tg.enabled and tg.bot_token)
        if not has_telegram and self.voice_link is None:
            return
        try:
            from .presence.manager import PresenceManager

            if has_telegram:
                from .presence.telegram_bot import TelegramBridge

                self.telegram = TelegramBridge(
                    tg.bot_token,
                    tg.chat_id,
                    on_message=self._on_telegram,
                    on_audio=self._on_telegram_audio,
                    on_event=self.bus.emit,
                )
                await self.telegram.start()
            self.presence = PresenceManager(
                self, telegram=self.telegram, voice_link=self.voice_link
            )
            await self.presence.start()
            self.bus.emit(
                "presence",
                event="online",
                telegram=has_telegram,
                voice=self.voice_link is not None,
            )
        except Exception as exc:
            self.bus.emit("error", where="presence", message=str(exc))
            self.telegram = self.presence = None

    async def _on_telegram(self, text: str) -> str:
        """Handle an inbound Telegram message → a full brain reply (sent back by the bridge)."""
        reply = await self.ask_text(text, status="Replying on Telegram, sir")
        return reply or "(I had nothing to add, sir.)"

    async def _ensure_transcriber(self):
        """Lazily build (once) the local faster-whisper model, shared by voice mode and
        inbound Telegram voice notes. Loading is done off-thread so the loop isn't blocked."""
        if self.transcriber is not None:
            return self.transcriber
        async with self._transcriber_lock:
            if self.transcriber is None:
                from .voice.stt import Transcriber

                v = self.config.voice
                self.transcriber = await asyncio.to_thread(
                    Transcriber,
                    v.whisper_model,
                    v.whisper_device,
                    v.whisper_compute_type,
                    v.whisper_beam_size,
                )
        return self.transcriber

    async def _on_telegram_audio(self, data: bytes) -> str:
        """Transcribe an inbound Telegram voice note, then answer it like a text message.

        Telegram voice notes arrive as OGG/Opus; faster-whisper decodes the bytes directly
        (via PyAV), so no external ffmpeg is needed. The reply echoes what was heard so the
        user can confirm the transcription.
        """
        import io

        try:
            transcriber = await self._ensure_transcriber()
            text = (
                await asyncio.to_thread(transcriber.transcribe, io.BytesIO(bytes(data)))
            ).strip()
        except Exception as exc:
            self.bus.emit("error", where="telegram_stt", message=str(exc))
            return "I couldn't transcribe that audio, sir."
        if not text:
            return "I couldn't make out any speech in that, sir."
        self.bus.emit("heard", text=text, source="telegram")
        reply = await self.ask_text(text, status="Replying on a voice note, sir")
        heard = f"\U0001f399️ “{text}”"
        return f"{heard}\n\n{reply}" if reply else f"{heard}\n\n(I had nothing to add, sir.)"

    async def play_boot(self) -> None:
        """Play the launch power-up ambience + spoken welcome (best-effort)."""
        if self.audiofx is not None:
            await self.audiofx.boot()

    async def start_dashboard(self) -> str:
        """Launch the live dashboard server; returns its URL."""
        from .dashboard.server import DashboardServer

        self.dashboard = DashboardServer(
            self.ctx, self.config.dashboard.host, self.config.dashboard.port
        )
        await self.dashboard.start()
        return self.dashboard.url

    async def stop(self) -> None:
        if self.presence is not None:
            await self.presence.stop()
        if self.telegram is not None:
            await self.telegram.stop()
        if self.audiofx is not None:
            await self.audiofx.stop()
        if self.dashboard is not None:
            await self.dashboard.stop()
        if getattr(self.ctx, "remote_audio", None) is not None:
            self.ctx.remote_audio.stop()
        if getattr(self.ctx, "browser", None) is not None:
            await self.ctx.browser.close()
        await self.brain.stop()

    def ask(self, text: str) -> AsyncIterator[str]:
        return self.brain.ask(text)

    async def ask_text(self, text: str, status: str | None = None) -> str:
        """Run one full brain turn under the brain lock and return the whole reply.

        For non-streaming callers (Telegram now, the voice presence-check later). `status`
        optionally shows a THINKING detail on the HUD for the turn; the prior state is
        restored afterwards so a concurrent voice/REPL turn isn't visually clobbered.
        """
        async with self.ask_lock:
            prev = (self.state.status.state, self.state.status.detail)
            if status is not None:
                self.state.set_state(JarvisState.THINKING, status)
            parts: list[str] = []
            try:
                async for chunk in self.brain.ask(text):
                    parts.append(chunk)
            finally:
                if status is not None:
                    self.state.set_state(prev[0], prev[1])
            return "".join(parts).strip()

    def _report_stt_device(self, transcriber) -> None:
        """Print (and feed) where speech-to-text actually runs, so a CPU fallback is obvious."""
        v = self.config.voice
        got = transcriber.device_report()
        line = (f"STT  · faster-whisper {v.whisper_model} → {got} "
                f"(requested {v.whisper_device}/{v.whisper_compute_type})")
        print(f"   [device] {line}", flush=True)
        self.bus.emit("status", state="thinking", detail=line)
        if v.whisper_device == "cuda" and got != "cuda":
            warn = ("STT asked for CUDA but is on the CPU — install the CUDA build "
                    "(nvidia-cublas-cu12 + nvidia-cudnn-cu12 on PATH).")
            print(f"   [device] ! {warn}", flush=True)
            self.bus.emit("error", where="stt", message=warn)

    def _report_kokoro_device(self, providers: list[str]) -> None:
        """Print (and feed) the ONNX providers Kokoro loaded — onnxruntime falls back silently."""
        v = self.config.voice
        on_gpu = any("CUDA" in p or "Tensorrt" in p for p in providers)
        shown = ", ".join(providers) if providers else "unknown"
        line = f"TTS  · Kokoro {v.kokoro_voice} → {shown} (requested {v.kokoro_device})"
        print(f"   [device] {line}", flush=True)
        self.bus.emit("status", state="thinking", detail=line)
        if v.kokoro_device == "cuda" and not on_gpu:
            warn = ("Kokoro asked for CUDA but loaded CPU only — `pip install onnxruntime-gpu` "
                    "on this machine (and put matching CUDA + cuDNN on PATH).")
            print(f"   [device] ! {warn}", flush=True)
            self.bus.emit("error", where="tts", message=warn)

    def _report_xtts_device(self, device: str) -> None:
        """Print (and feed) whether the XTTS-v2 clone loaded on the GPU or fell back to CPU."""
        v = self.config.voice
        ref = Path(str(v.xtts_reference)).name if v.xtts_reference else "?"
        line = f"TTS  · XTTS-v2 clone ({ref}) → {device} (requested {v.xtts_device})"
        print(f"   [device] {line}", flush=True)
        self.bus.emit("status", state="thinking", detail=line)
        if v.xtts_device == "cuda" and device != "cuda":
            warn = ("Voice clone asked for CUDA but is on the CPU (torch sees no GPU) — install a "
                    "CUDA torch build; XTTS is near-unusable in real time on CPU.")
            print(f"   [device] ! {warn}", flush=True)
            self.bus.emit("error", where="tts", message=warn)

    async def run_voice(self) -> None:
        """Run the continuous voice conversation loop (Phase 1).

        Builds the wake-word listener, the local STT model, and the ElevenLabs voice,
        then hands off to VoiceConversation. Imports are local so text mode never pays
        for the (heavier) voice dependencies.
        """
        v = self.config.voice
        missing: list[str] = []
        if v.wake_enabled and v.wake_engine == "porcupine" and not v.picovoice_access_key:
            missing.append("Picovoice access key (PICOVOICE_ACCESS_KEY) — needed for wake_engine='porcupine'")
        # Only the cloud engine needs keys; the local Kokoro engine is keyless.
        if v.tts_engine == "elevenlabs":
            if not v.elevenlabs_api_key:
                missing.append("ElevenLabs API key (ELEVENLABS_API_KEY)")
            if not v.elevenlabs_voice_id:
                missing.append("ElevenLabs voice id (JARVIS_ELEVENLABS_VOICE_ID)")
        if missing:
            raise VoiceConfigError(missing)

        from .voice.listener import Listener
        from .voice.loop import VoiceConversation
        from .voice.tts import build_speaker
        from .voice.wake import build_detector

        remote = v.transport == "browser"
        load_msg = ("Loading the speech model" if remote or not v.wake_enabled
                    else "Loading the speech + wake models")
        self.state.set_state(JarvisState.THINKING, load_msg)
        transcriber = await self._ensure_transcriber()  # shared with Telegram voice notes
        self._report_stt_device(transcriber)
        speaker = build_speaker(
            v,
            on_level=lambda value: self.ctx.post_event("level", value=value, source="speaking"),
        )
        if v.tts_engine == "kokoro":
            # Pre-load the ~310 MB Kokoro model off-thread now, so the FIRST spoken reply
            # isn't stalled ~7 s while it loads on demand.
            async def _warm_kokoro() -> None:
                try:
                    from .voice.tts import kokoro_providers, load_kokoro

                    inst = await asyncio.to_thread(load_kokoro, v)
                    self._report_kokoro_device(kokoro_providers(inst))
                except Exception as exc:
                    self.bus.emit("error", where="tts", message=f"Kokoro warm-up failed: {exc}")

            asyncio.ensure_future(_warm_kokoro())
        elif v.tts_engine == "xtts":
            # Pre-load the ~1.8 GB XTTS-v2 clone model + reference latents off-thread, so the
            # first spoken reply isn't stalled while it loads (and the download can be huge).
            async def _warm_xtts() -> None:
                try:
                    from .voice.tts import load_xtts

                    bundle = await asyncio.to_thread(load_xtts, v)
                    self._report_xtts_device(bundle.device)
                except Exception as exc:
                    self.bus.emit("error", where="tts", message=f"XTTS warm-up failed: {exc}")

            asyncio.ensure_future(_warm_xtts())

        if remote:
            # Remote/tunneled mode: a browser tab is the mic + speaker (over the dashboard
            # WebSocket); this process only does STT + TTS. No local mic/wake detector is built.
            d = self.config.dashboard
            if not d.enabled:
                raise VoiceConfigError([
                    "remote voice (transport='browser') needs the dashboard — it IS the "
                    "audio transport. Don't combine --remote with --no-dashboard."
                ])
            if d.host not in ("127.0.0.1", "localhost", "::1"):
                raise VoiceConfigError([
                    f"remote voice requires the dashboard bound to loopback (host is "
                    f"'{d.host}'). It must be reached over your SSH tunnel, never exposed on "
                    "the network — a mic-streaming, brain-triggering socket has no auth. Set "
                    "[dashboard] host = \"127.0.0.1\"."
                ])
            from .voice.remote import RemoteAudioHub, build_browser_transport

            if self.ctx.remote_audio is None:  # normally created early in start()
                self.ctx.remote_audio = RemoteAudioHub(self.ctx)
            listener, speaker, ptt = build_browser_transport(
                self.ctx.remote_audio, v, engine=speaker, frame_length=1600,
            )
        else:
            detector = await asyncio.to_thread(build_detector, v)  # NullDetector when wake is off
            listener = Listener(
                detector,
                input_device=v.input_device,
                silence_timeout=v.silence_timeout,
                max_utterance_seconds=v.max_utterance_seconds,
                energy_threshold=v.energy_threshold,
                input_gain=v.input_gain,
                start_guard_seconds=v.start_guard_seconds,
                start_active_seconds=v.vad_start_active_seconds,
                end_threshold_ratio=v.vad_end_threshold_ratio,
                vad_engine=v.vad_engine,
                silero_model_path=v.silero_model_path,
                silero_threshold=v.silero_threshold,
                on_level=lambda value, source: self.ctx.post_event("level", value=value, source=source),
            )
            ptt = None
            if v.ptt_enabled:
                try:
                    from .voice.ptt import PushToTalk

                    ptt = PushToTalk(v.ptt_key)
                except Exception as exc:  # pynput missing or unavailable
                    self.bus.emit("error", where="ptt", message=f"push-to-talk disabled: {exc}")
                    ptt = None

            # Button-only mode needs a working push-to-talk key, or there's no way to talk.
            if not v.wake_enabled and ptt is None:
                raise VoiceConfigError([
                    f"push-to-talk (key '{v.ptt_key}') — required because the wake word is off. "
                    "Install the voice extra (pynput) or set [voice] wake_enabled = true."
                ])

        conversation = VoiceConversation(
            self, listener, transcriber, speaker, self.config,
            ptt=ptt, voice_link=self.voice_link,
        )
        await conversation.run()
