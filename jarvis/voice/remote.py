"""Remote / tunneled voice transport — the mic and speaker live in a browser tab.

In `transport = "browser"` mode Jarvis's brain + STT + TTS run on one machine (ideally a
GPU desktop) while the user sits at another with only the dashboard open in a browser. The
browser captures the mic (16 kHz int16 PCM) and streams it over the dashboard WebSocket; the
desktop transcribes it, thinks, synthesizes the reply, and streams the TTS PCM back over the
same socket to play on the browser's speakers. Push-to-talk and barge-in are a button/key in
the page.

Nothing about the voice *loop* changes: this module provides three drop-in, duck-typed
transports — `BrowserListener` / `BrowserSpeaker` / `BrowserPTT` — that satisfy exactly the
interfaces `VoiceConversation` already calls on the local sounddevice objects. A
`RemoteAudioHub` bridges them to the `DashboardServer`'s `/ws` socket:

  browser mic ──BINARY PCM──►  Hub.on_binary ► BrowserListener (buffer while PTT held)
  {ptt_press/ptt_release}  ──►  Hub.on_control ► BrowserPTT / arms the listener
  BrowserSpeaker.speak(text) ► engine synth ► _WsSink ► Hub queue ──BINARY PCM──► browser

All outbound audio (tts_start / PCM slices / tts_end / tts_flush) goes through ONE ordered
queue drained by a single task, so control frames can never overtake the PCM they bracket.
"""

from __future__ import annotations

import asyncio

import numpy as np

from .tts import _PcmSpeaker


class RemoteAudioHub:
    """Bridges the dashboard `/ws` socket to the Browser{Listener,Speaker,PTT}.

    One active audio client at a time (the tab that last pressed PTT / said hello). Inbound
    control + binary arrive on the event loop from `DashboardServer`; outbound audio is pushed
    through a single ordered queue drained to that client.
    """

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._loop = ctx.loop
        self._client = None          # the WebSocket acting as mic/speaker
        self._listener = None        # BrowserListener
        self._ptt = None             # BrowserPTT
        # One ordered queue drained by a single task. Items are (kind, gen, payload):
        # kind ∈ {"json","pcm"}; gen tags the utterance (None = never-dropped control, e.g. flush).
        self._q: asyncio.Queue = asyncio.Queue()
        self._drain_task = None
        self._gen = 0            # generation of the current/last-started TTS utterance
        self._flushed_gen = 0    # items with gen <= this were barged → dropped at drain

    # ---- registration (called from build_browser_transport / the transports) ----
    def attach_listener(self, listener) -> None:
        self._listener = listener

    def detach_listener(self) -> None:
        self._listener = None

    def attach_ptt(self, ptt) -> None:
        self._ptt = ptt
        self._ensure_drain()

    def detach_ptt(self) -> None:
        self._ptt = None

    # ---- inbound from the DashboardServer (all ON the event loop) ----
    def on_control(self, ws, msg: dict) -> None:
        t = msg.get("type")
        if t == "ping":
            asyncio.create_task(self._safe_send_json(ws, {"type": "pong"}))
            return
        if t in ("audio_hello", "ptt_press", "barge_in"):
            self._client = ws                       # claim the audio slot
        if t in ("ptt_press", "barge_in"):
            # Arm capture on the PRESS (not when the loop later picks it up) so the first
            # syllable — which arrives as binary right after this message — isn't dropped.
            if self._listener is not None:
                self._listener.start_ptt()
            if self._ptt is not None:
                self._ptt.on_press()
        elif t == "ptt_release":
            if self._ptt is not None:
                self._ptt.on_release()

    def on_binary(self, ws, data: bytes) -> None:
        # Only the active client's mic counts (a second tab holding PTT can't interleave).
        if ws is self._client and self._listener is not None:
            self._listener.feed_pcm(data)

    def on_disconnect(self, ws) -> None:
        if ws is self._client:
            # A tab that drops mid-turn must not spawn a phantom brain turn from a partial
            # capture: tell the listener to discard, and unblock any awaited release.
            if self._listener is not None:
                self._listener.mark_discard()
            if self._ptt is not None and self._ptt.is_down:
                self._ptt.on_release()
            self._client = None

    @property
    def has_client(self) -> bool:
        return self._client is not None

    # ---- outbound TTS (tts_start/send_tts/tts_end run on the speak() WORKER THREAD) ----
    def tts_start(self, rate: int) -> int:
        """Open an utterance; returns its generation, which the sink stamps onto every frame."""
        self._gen += 1
        gen = self._gen
        self._enqueue(("json", gen, {"type": "tts_start", "data": {"rate": rate}}))
        return gen

    def send_tts(self, gen: int, pcm: bytes) -> None:
        self._enqueue(("pcm", gen, pcm))

    def tts_end(self, gen: int) -> None:
        self._enqueue(("json", gen, {"type": "tts_end", "data": {}}))

    def flush_tts(self) -> None:
        """Barge-in: cancel the current utterance's frames and tell the client to clear playback.

        Runs on the loop (from BrowserSpeaker.stop via on_barge). Marking `_flushed_gen` makes the
        drain drop any straggler PCM/tts_end the still-running synth worker enqueues AFTER this —
        those callbacks are pending on the loop and invisible to `_purge()`, so the marker (not the
        purge alone) is what guarantees the clean cut."""
        self._flushed_gen = self._gen
        self._purge()
        self._q.put_nowait(("json", None, {"type": "tts_flush", "data": {}}))

    # ---- internals ----
    def _enqueue(self, item) -> None:
        # Marshal onto the loop; the worker thread must not touch the asyncio.Queue directly.
        self._loop.call_soon_threadsafe(self._q.put_nowait, item)

    def _purge(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except asyncio.QueueEmpty:
            pass

    def _ensure_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain(), name="remote-tts-drain")

    async def _drain(self) -> None:
        while True:
            kind, gen, payload = await self._q.get()
            if gen is not None and gen <= self._flushed_gen:
                continue  # straggler from a barged utterance → drop
            client = self._client
            if client is None:
                continue  # no audio client attached → drop (drain never blocks synthesis)
            try:
                if kind == "pcm":
                    await client.send_bytes(payload)
                else:
                    await client.send_json(payload)
            except Exception:
                pass

    async def _safe_send_json(self, ws, obj) -> None:
        try:
            await ws.send_json(obj)
        except Exception:
            pass

    def stop(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None


class BrowserPTT:
    """Duck-types `voice.ptt.PushToTalk`, driven by browser control messages instead of pynput."""

    def __init__(self, hub: RemoteAudioHub) -> None:
        self._hub = hub
        self._loop = None
        self._down = False
        self._press_waiters: list[asyncio.Future] = []
        self._release_waiters: list[asyncio.Future] = []

    def start(self, loop) -> None:
        self._loop = loop
        self._hub.attach_ptt(self)

    def stop(self) -> None:
        self._hub.detach_ptt()

    @property
    def is_down(self) -> bool:
        return self._down

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

    # ---- resolved by the hub, on the loop ----
    def on_press(self) -> None:
        if self._down:
            return
        self._down = True
        waiters, self._press_waiters = self._press_waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)

    def on_release(self) -> None:
        if not self._down:
            return
        self._down = False
        waiters, self._release_waiters = self._release_waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)


class BrowserListener:
    """Duck-types `voice.listener.Listener` for push-to-talk over the WebSocket.

    No mic thread and no VAD: browser binary PCM (int16 mono 16 kHz) is buffered while PTT is
    held; `stop_ptt()` returns it as float32 in [-1, 1] — exactly what `Transcriber.transcribe`
    expects. Wake-word methods are inert stubs (remote mode is push-to-talk only).
    """

    def __init__(self, hub: RemoteAudioHub, *, frame_length: int = 1600,
                 threshold: float = 0.0, on_level=None) -> None:
        self.sample_rate = 16000
        self.frame_length = frame_length
        self.threshold = threshold          # read by the loop's "voice ready" event
        self.on_level = on_level
        self._hub = hub
        self._mode = "paused"
        self._buf: list[np.ndarray] = []
        self._discard = False

    def start(self, loop) -> None:
        self._loop = loop
        self._hub.attach_listener(self)

    def stop(self) -> None:
        self._hub.detach_listener()

    # ---- push-to-talk ----
    def start_ptt(self) -> None:
        # Idempotent: the hub arms this on the PRESS; the loop calls it again a few hops
        # later — don't clobber the pre-roll frames already captured.
        if self._mode != "ptt":
            self._buf = []
            self._discard = False
            self._mode = "ptt"

    def stop_ptt(self):
        self._mode = "paused"
        buf, self._buf = self._buf, []
        if self._discard:
            self._discard = False
            return None
        if not buf:
            return None
        return np.concatenate(buf).astype(np.float32) / 32768.0

    def mark_discard(self) -> None:
        self._discard = True

    def feed_pcm(self, data: bytes) -> None:
        if self._mode != "ptt":
            return  # server-side mic gate: ignore anything not during a held press
        arr = np.frombuffer(data, dtype=np.int16)
        if arr.size:
            self._buf.append(arr)
            if self.on_level is not None:
                rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
                self.on_level(min(1.0, rms / 4000.0), "mic")

    # ---- wake path: inert (remote mode is PTT-only) ----
    async def wait_for_wake(self) -> bool:
        await self._loop.create_future()   # never resolves
        return True

    def start_monitor(self, on_barge) -> None:
        pass

    def stop_monitor(self) -> None:
        pass

    async def record_utterance(self, *, timeout=None):
        return None


class _WsSink:
    """Speaker sink that streams int16 PCM over the WebSocket instead of to sounddevice.

    `tts_start` (with the engine sample rate) opens the utterance, `write` sends each slice,
    `close` ends it — all through the hub's single ordered queue so the client sees
    start → slices → end in order.
    """

    def __init__(self, hub: RemoteAudioHub, rate: int) -> None:
        self._hub = hub
        self._gen = hub.tts_start(rate)   # generation stamped on every frame of this utterance

    def write(self, slice_bytes: bytes) -> None:
        self._hub.send_tts(self._gen, bytes(slice_bytes))

    def close(self) -> None:
        self._hub.tts_end(self._gen)


class BrowserSpeaker(_PcmSpeaker):
    """Speaks by streaming the real engine's PCM over the WebSocket.

    Reuses the wrapped engine's `_pcm_chunks` (Kokoro / ElevenLabs synth) unchanged; only the
    sink differs. Note: because the WebSocket sink does NOT back-pressure at real time, the
    `_stop` event only prevents the NEXT sentence — an intra-sentence cut is done by the client
    flushing its own playback on barge-in (which `flush_tts` triggers).
    """

    def __init__(self, engine: _PcmSpeaker, hub: RemoteAudioHub, on_level=None) -> None:
        super().__init__(sample_rate=engine._sample_rate, on_level=on_level)
        self._engine = engine
        self._hub = hub

    @property
    def can_speak(self) -> bool:
        """Whether a browser is attached to actually hear speech (used by presence routing)."""
        return self._hub.has_client

    def _pcm_chunks(self, text: str):
        return self._engine._pcm_chunks(text)

    def _open_sink(self):
        return _WsSink(self._hub, self._sample_rate)

    def speak(self, text: str) -> None:
        # No listener attached → nobody to hear it. Skip the (GPU) synth entirely rather than
        # render a reply into the void (e.g. a presence note with no tab connected).
        if not self._hub.has_client:
            return
        super().speak(text)

    def stop(self) -> None:
        super().stop()          # set the threading.Event (stops the next sentence)
        self._hub.flush_tts()   # drop queued PCM + tell the client to clear playback now


def build_browser_transport(hub: RemoteAudioHub, voice_config, *, engine, frame_length: int):
    """Build the three browser-backed transports around an already-built engine speaker.

    `engine` is a normal `_PcmSpeaker` (Kokoro/ElevenLabs) from `build_speaker` — we reuse its
    synth and only swap the sink. Levels are computed in the browser from real playback/mic
    audio, so the Python-side transports pass `on_level=None`.
    """
    listener = BrowserListener(hub, frame_length=frame_length)
    speaker = BrowserSpeaker(engine, hub)
    ptt = BrowserPTT(hub)
    return listener, speaker, ptt
