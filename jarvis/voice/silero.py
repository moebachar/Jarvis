"""Neural speech endpointing via Silero VAD (ONNX, no torch).

Reuses the silero_vad.onnx model that openWakeWord already downloads. Unlike the
energy VAD, this classifies *speech vs non-speech*, so background noise scores low
(it won't keep the recording open) and quiet/far syllables still score high (they
won't be cut off). Same push()/reset() interface as EnergyVAD.

The model is Silero v4: inputs input[batch,seq] + sr + LSTM state h,c[2,batch,64];
outputs speech-probability + new state. We re-chunk whatever frames we're given into
512-sample windows and carry the state across them.
"""

from __future__ import annotations

import glob
import os
from collections import deque

import numpy as np

_CHUNK = 512  # samples per inference window at 16 kHz


def find_silero_model() -> str | None:
    """Locate the silero_vad.onnx bundled with openWakeWord, if present."""
    try:
        import openwakeword

        base = os.path.dirname(openwakeword.__file__)
        hits = glob.glob(os.path.join(base, "**", "silero_vad*.onnx"), recursive=True)
        return hits[0] if hits else None
    except Exception:
        return None


class SileroVAD:
    def __init__(
        self,
        model_path: str,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        silence_timeout: float = 0.7,
        max_utterance_seconds: float = 15.0,
        min_speech_seconds: float = 0.2,
        start_active_seconds: float = 0.15,
        onset_timeout: float | None = None,
    ) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(
            model_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(sample_rate, dtype=np.int64)
        self._threshold = threshold

        cps = sample_rate / _CHUNK
        self._silence_chunks = max(1, round(silence_timeout * cps))
        self._max_chunks = max(1, round(max_utterance_seconds * cps))
        # Onset timeout: end an empty turn only if speech never STARTS in this window;
        # once it starts, the utterance is never cut mid-sentence (silence/max govern it).
        self._onset_chunks = round(onset_timeout * cps) if onset_timeout else None
        self._min_speech_chunks = max(1, round(min_speech_seconds * cps))
        self._start_active = max(1, round(start_active_seconds * cps))
        self._start_window = max(self._start_active, round((start_active_seconds + 0.2) * cps))
        self.reset()

    def reset(self) -> None:
        self._raw: list[np.ndarray] = []
        self._pending = np.zeros(0, dtype=np.int16)
        self._started = False
        self._silence_run = 0
        self._speech_chunks = 0
        self._chunk_count = 0
        self._window: deque[int] = deque(maxlen=self._start_window)
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def _speech_prob(self, chunk: np.ndarray) -> float:
        audio = (chunk.astype(np.float32) / 32768.0).reshape(1, -1)
        out, self._h, self._c = self._sess.run(
            None, {"input": audio, "sr": self._sr, "h": self._h, "c": self._c}
        )
        return float(np.asarray(out).reshape(-1)[0])

    def push(self, frame: np.ndarray) -> tuple[bool, np.ndarray | None]:
        frame = np.asarray(frame, dtype=np.int16).reshape(-1)
        self._raw.append(frame)
        self._pending = np.concatenate([self._pending, frame])

        ended = False
        while len(self._pending) >= _CHUNK:
            chunk = self._pending[:_CHUNK]
            self._pending = self._pending[_CHUNK:]
            self._chunk_count += 1

            speech = self._speech_prob(chunk) >= self._threshold
            if speech:
                self._speech_chunks += 1
            self._window.append(1 if speech else 0)

            if not self._started and sum(self._window) >= self._start_active:
                self._started = True
            if self._started:
                self._silence_run = 0 if speech else self._silence_run + 1

            onset_expired = (
                not self._started
                and self._onset_chunks is not None
                and self._chunk_count >= self._onset_chunks
                and sum(self._window) == 0  # nothing brewing — don't cut a turn just beginning
            )
            if (
                (self._started and self._silence_run >= self._silence_chunks)
                or (self._chunk_count >= self._max_chunks)
                or onset_expired
            ):
                ended = True
                break

        if not ended:
            return False, None
        if self._speech_chunks >= self._min_speech_chunks:
            audio = np.concatenate(self._raw).astype(np.float32) / 32768.0
            return True, audio
        return True, None
