"""A noise-robust, dependency-free energy voice activity detector.

Consumes fixed-size int16 frames (whatever the wake engine hands us) and decides when
an utterance has ended — speech was heard, then trailing silence followed. Pure logic,
fully unit-testable without a microphone.

Robustness for real (far-field, slightly noisy) rooms:
  * start guard   — discard the first N ms after opening, so the chime echo can't trigger.
  * sustained onset — require speech energy across a short window before declaring speech,
    so a single click/pop can't start (and then prematurely end) a capture.
  * hysteresis    — once speaking, a frame only counts as "silence" below a LOWER threshold,
    so quiet/far syllables aren't mistaken for a pause and cut off.
  * a generous end-of-speech window so natural pauses don't end the turn early.
"""

from __future__ import annotations

from collections import deque

import numpy as np


def frame_rms(frame: np.ndarray) -> float:
    """Root-mean-square energy of an int16 frame (0..~32768)."""
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))


class EnergyVAD:
    def __init__(
        self,
        sample_rate: int,
        frame_length: int,
        *,
        threshold: float,
        silence_timeout: float = 1.0,
        max_utterance_seconds: float = 15.0,
        min_speech_seconds: float = 0.3,
        start_guard_seconds: float = 0.0,
        start_active_seconds: float = 0.3,
        end_threshold_ratio: float = 0.45,
        onset_timeout: float | None = None,
    ) -> None:
        fps = sample_rate / frame_length
        self.threshold = threshold
        # Hysteresis: once speaking, stay "in speech" until energy drops below this.
        self.end_threshold = threshold * end_threshold_ratio
        self._silence_frames = max(1, round(silence_timeout * fps))
        self._max_frames = max(1, round(max_utterance_seconds * fps))
        # Onset timeout: give up only if speech never STARTS within this window. Once it
        # starts, the utterance runs to its natural end — it is never cut mid-sentence.
        self._onset_frames = round(onset_timeout * fps) if onset_timeout else None
        self._min_speech_frames = max(1, round(min_speech_seconds * fps))
        self._guard_frames = max(0, round(start_guard_seconds * fps))
        # Onset: need `start_active` loud frames within a slightly larger window.
        self._start_active = max(1, round(start_active_seconds * fps))
        self._start_window = max(self._start_active, round((start_active_seconds + 0.2) * fps))
        self.reset()

    def reset(self) -> None:
        self._buffer: list[np.ndarray] = []
        self._started = False
        self._silence_run = 0
        self._loud_total = 0
        self._frame_count = 0
        self._guard_left = self._guard_frames
        self._window: deque[int] = deque(maxlen=self._start_window)

    def push(self, frame: np.ndarray) -> tuple[bool, np.ndarray | None]:
        """Feed one int16 frame.

        Returns (finished, audio); `audio` is float32 in [-1, 1], or None if what was
        captured wasn't real speech (too short / just noise).
        """
        if self._guard_left > 0:
            self._guard_left -= 1
            return False, None

        self._buffer.append(frame)
        self._frame_count += 1

        rms = frame_rms(frame)
        loud = rms >= self.threshold
        if loud:
            self._loud_total += 1
        self._window.append(1 if loud else 0)

        # Onset: sustained energy across the window, not one stray frame.
        if not self._started and sum(self._window) >= self._start_active:
            self._started = True

        if self._started:
            # Hysteresis: only the lower threshold counts as silence.
            if rms >= self.end_threshold:
                self._silence_run = 0
            else:
                self._silence_run += 1

        onset_expired = (
            not self._started
            and self._onset_frames is not None
            and self._frame_count >= self._onset_frames
            and sum(self._window) == 0  # nothing brewing — don't cut a turn just beginning
        )
        ended = (
            (self._started and self._silence_run >= self._silence_frames)
            or (self._frame_count >= self._max_frames)
            or onset_expired
        )
        if not ended:
            return False, None

        if self._loud_total >= self._min_speech_frames:
            pcm = np.concatenate(self._buffer).astype(np.float32) / 32768.0
            return True, pcm
        return True, None
