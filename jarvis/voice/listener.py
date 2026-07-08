"""The microphone listener: one 16 kHz input stream driving wake-word + capture.

A single background thread owns the only mic stream (you can't read one device from
two places). Every frame is fed to the wake detector and, when we're capturing, to the
energy VAD. Results cross back to the asyncio loop via `call_soon_threadsafe`, so the
conversation loop can `await` wake words and utterances naturally.

Modes (set by the async side):
  paused   – do nothing with frames
  wake     – detect the wake word -> resolve wait_for_wake()
  record   – capture an utterance via VAD -> resolve record_utterance()
  monitor  – detect the wake word -> fire the barge-in callback (used while speaking)
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import sounddevice as sd

from .vad import EnergyVAD, frame_rms


class ListenerError(RuntimeError):
    pass


class Listener:
    def __init__(
        self,
        detector,
        *,
        input_device=None,
        silence_timeout: float = 0.8,
        max_utterance_seconds: float = 15.0,
        energy_threshold: float | None = None,
        input_gain: float = 1.0,
        start_guard_seconds: float = 0.25,
        start_active_seconds: float = 0.3,
        end_threshold_ratio: float = 0.45,
        vad_engine: str = "silero",
        silero_model_path: str | None = None,
        silero_threshold: float = 0.5,
        on_level=None,
    ) -> None:
        self._detector = detector
        self.sample_rate = detector.sample_rate     # 16000
        self.frame_length = detector.frame_length   # engine-specific (512 / 1280 / 1600)
        self._input_device = input_device
        self._gain = float(input_gain)
        self._silence_timeout = silence_timeout
        self._max_utterance_seconds = max_utterance_seconds
        self._start_guard_seconds = start_guard_seconds
        self._start_active_seconds = start_active_seconds
        self._end_threshold_ratio = end_threshold_ratio
        self._vad_engine = vad_engine
        self._silero_model_path = silero_model_path
        self._silero_threshold = silero_threshold
        self._on_level = on_level
        self._level_counter = 0

        self.threshold = energy_threshold  # None => auto-calibrate in the thread
        self._mode = "paused"
        self._vad: EnergyVAD | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._ready = threading.Event()

        self._wake_future: asyncio.Future | None = None
        self._utt_future: asyncio.Future | None = None
        self._barge_cb = None
        self._error: Exception | None = None
        self._ptt_buffer: list[np.ndarray] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._running = True
        self._thread = threading.Thread(target=self._run, name="jarvis-listener", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            if self._error:
                raise ListenerError(f"microphone failed to start: {self._error!r}")
            raise ListenerError("microphone did not become ready in time")
        if self._error:
            raise ListenerError(f"microphone failed to start: {self._error!r}")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._detector.delete()

    # --------------------------------------------------------------------- thread
    def _run(self) -> None:
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.frame_length,
                device=self._input_device,
            )
            stream.start()
        except Exception as exc:
            self._error = exc
            self._ready.set()
            return

        prev_mode = None
        try:
            if self.threshold is None:
                self.threshold = self._calibrate(stream)
            self._ready.set()
            while self._running:
                frame = self._grab(stream)
                mode = self._mode

                # Clear detector state when (re)arming wake detection.
                if mode != prev_mode:
                    if mode in ("wake", "monitor"):
                        self._detector.reset()
                        if hasattr(self._detector, "set_strict"):
                            # stricter matching for barge-in (monitor) than for waking
                            self._detector.set_strict(mode == "monitor")
                    prev_mode = mode

                if mode in ("wake", "monitor"):
                    if self._detector.process(frame):
                        self._post(self._fire_wake)
                elif mode == "record" and self._vad is not None:
                    finished, audio = self._vad.push(frame)
                    if finished:
                        self._mode = "paused"
                        self._post(lambda a=audio: self._fire_utterance(a))
                elif mode == "ptt":
                    self._ptt_buffer.append(frame)

                # Feed the dashboard a mic level while actively capturing/monitoring.
                if self._on_level is not None and mode in ("record", "monitor", "ptt"):
                    self._level_counter += 1
                    if self._level_counter % 2 == 0:
                        level = min(1.0, frame_rms(frame) / 4000.0)
                        self._on_level(level, "mic")
        except Exception as exc:
            self._error = exc
            self._post(lambda e=exc: self._fail(e))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _grab(self, stream) -> np.ndarray:
        """Read one frame and apply software gain (with clipping)."""
        data, _ = stream.read(self.frame_length)
        frame = np.asarray(data).reshape(-1)
        if self._gain != 1.0:
            frame = np.clip(frame.astype(np.int32) * self._gain, -32768, 32767).astype(np.int16)
        return frame

    def _calibrate(self, stream, seconds: float = 0.4) -> float:
        frames = max(1, int(seconds * self.sample_rate / self.frame_length))
        readings = [frame_rms(self._grab(stream)) for _ in range(frames)]
        noise_floor = float(np.median(readings)) if readings else 100.0
        return max(noise_floor * 3.0, 120.0)

    def _post(self, fn) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(fn)

    # ----------------------------------------------------------- loop-thread side
    def _fire_wake(self) -> None:
        if self._wake_future is not None and not self._wake_future.done():
            self._wake_future.set_result(True)
        elif self._barge_cb is not None:
            self._barge_cb()

    def _fire_utterance(self, audio) -> None:
        if self._utt_future is not None and not self._utt_future.done():
            self._utt_future.set_result(audio)

    def _fail(self, exc: Exception) -> None:
        for fut in (self._wake_future, self._utt_future):
            if fut is not None and not fut.done():
                fut.set_exception(ListenerError(str(exc)))

    # ------------------------------------------------------------------- async API
    async def wait_for_wake(self) -> bool:
        assert self._loop is not None
        fut = self._loop.create_future()
        self._wake_future = fut
        self._barge_cb = None
        self._mode = "wake"
        try:
            return await fut
        finally:
            self._wake_future = None
            self._mode = "paused"

    def _make_vad(self, onset_timeout: float | None = None):
        if self._vad_engine == "silero":
            try:
                from .silero import SileroVAD, find_silero_model

                path = self._silero_model_path or find_silero_model()
                if path:
                    return SileroVAD(
                        path,
                        threshold=self._silero_threshold,
                        silence_timeout=self._silence_timeout,
                        max_utterance_seconds=self._max_utterance_seconds,
                        start_active_seconds=self._start_active_seconds,
                        onset_timeout=onset_timeout,
                    )
            except Exception:
                pass  # fall back to energy below
        return EnergyVAD(
            self.sample_rate,
            self.frame_length,
            threshold=self.threshold or 200.0,
            silence_timeout=self._silence_timeout,
            max_utterance_seconds=self._max_utterance_seconds,
            start_guard_seconds=self._start_guard_seconds,
            start_active_seconds=self._start_active_seconds,
            end_threshold_ratio=self._end_threshold_ratio,
            onset_timeout=onset_timeout,
        )

    async def record_utterance(self, *, timeout: float | None = None):
        """Capture one utterance. `timeout` only bounds how long to wait for speech to
        START (the onset window); once you begin talking, the utterance always runs to its
        natural end and is never cut mid-sentence. A backstop of max_utterance_seconds
        guarantees termination if no speech is ever heard."""
        assert self._loop is not None
        self._vad = self._make_vad(onset_timeout=timeout)
        fut = self._loop.create_future()
        self._utt_future = fut
        self._mode = "record"
        try:
            return await fut
        finally:
            self._utt_future = None
            self._mode = "paused"

    # --------------------------------------------------------------- push-to-talk
    def start_ptt(self) -> None:
        """Begin buffering raw audio for a push-to-talk capture (held key)."""
        self._ptt_buffer = []
        self._mode = "ptt"

    def stop_ptt(self):
        """End a push-to-talk capture; return the held audio as float32, or None."""
        self._mode = "paused"
        buf = self._ptt_buffer
        self._ptt_buffer = []
        if not buf:
            return None
        return np.concatenate(buf).astype(np.float32) / 32768.0

    def start_monitor(self, on_barge) -> None:
        """Listen for the wake word while Jarvis is speaking (barge-in)."""
        self._barge_cb = on_barge
        self._mode = "monitor"

    def stop_monitor(self) -> None:
        self._barge_cb = None
        self._mode = "paused"
