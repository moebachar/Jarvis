"""A cache of short, pre-rendered Jarvis voice lines.

The boot greeting and the "minimal progress" phrases are a small, fixed vocabulary.
Rendering them through ElevenLabs on every use would burn the (free-tier ~10k
chars/month) quota and add latency, so we render each line **once**, store it as a
16 kHz mono WAV under ~/.jarvis/cache/voice/, and thereafter replay it straight from
disk — instant and free. Files are keyed by (voice id, model, text), so switching the
voice or editing a line transparently regenerates only what changed.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from .tts import render_utterance

SR = 16000


class VoiceBank:
    def __init__(self, voice_config, cache_dir: Path, sample_rate: int = SR) -> None:
        self.cfg = voice_config
        self.sr = sample_rate
        self.cache_dir = Path(cache_dir)
        self._mem: dict[str, np.ndarray] = {}  # text -> float32 waveform

    @property
    def _engine(self) -> str:
        return getattr(self.cfg, "tts_engine", "elevenlabs")

    @property
    def available(self) -> bool:
        if self._engine == "kokoro":
            return True  # local model auto-downloads; no key needed
        if self._engine == "xtts":
            return False  # skip the boot greeting: don't block startup on the ~1.8 GB clone load
        return bool(self.cfg.elevenlabs_api_key and self.cfg.elevenlabs_voice_id)

    # -- internals ---------------------------------------------------------- #
    def _speed(self, speed: float | None) -> float:
        return self.cfg.tts_speed if speed is None else speed

    def _path(self, text: str, speed: float | None = None) -> Path:
        if self._engine == "kokoro":
            voice, model = self.cfg.kokoro_voice, "kokoro"
        else:
            voice, model = self.cfg.elevenlabs_voice_id, self.cfg.elevenlabs_model_id
        sig = (f"{self._engine}|{voice}|{model}|"
               f"{self.sr}|{self._speed(speed):.3f}|{text}")
        digest = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.wav"

    @staticmethod
    def _to_float(pcm: np.ndarray) -> np.ndarray:
        return (pcm.astype(np.float32) / 32768.0).astype(np.float32)

    def _save(self, path: Path, pcm: np.ndarray) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(pcm.tobytes())

    def _load(self, path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as w:
            frames = w.readframes(w.getnframes())
        return self._to_float(np.frombuffer(frames, dtype=np.int16))

    def _render(self, text: str, speed: float | None = None) -> np.ndarray:
        # Route to the configured engine (ElevenLabs cloud or local Kokoro); Kokoro is
        # rendered at 24 kHz and resampled down to self.sr for the cached WAV.
        return render_utterance(self.cfg, text, speed=speed, sample_rate=self.sr)

    # -- public API --------------------------------------------------------- #
    def get(self, text: str, speed: float | None = None) -> np.ndarray | None:
        """Return the cached waveform from memory or disk. No network. None if absent."""
        key = self._path(text, speed)
        skey = str(key)
        if skey in self._mem:
            return self._mem[skey]
        if key.is_file():
            try:
                arr = self._load(key)
            except Exception:
                return None
            self._mem[skey] = arr
            return arr
        return None

    def ensure(self, text: str, speed: float | None = None) -> np.ndarray | None:
        """Return the waveform, rendering + caching it first if needed. Blocking; never raises."""
        cached = self.get(text, speed)
        if cached is not None:
            return cached
        if not self.available:
            return None
        path = self._path(text, speed)
        try:
            pcm = self._render(text, speed)
            self._save(path, pcm)
        except Exception:
            return None
        arr = self._to_float(pcm)
        self._mem[str(path)] = arr
        return arr
