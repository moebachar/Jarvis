"""Synthesized sound-effects engine — the cinematic "Stark HUD" audio palette.

Every cue is generated procedurally with numpy (no asset files, no downloads, no
copyright) at 16 kHz mono float32 in [-1, 1], matching the rest of the audio path.
A single persistent output stream mixes any number of overlapping cues in its
callback, so action blips can layer over the boot ambience or under Jarvis's voice
without opening a new device stream each time.

Aesthetic: clean sine/triangle partials, quick envelopes, a little detune shimmer
for the "glassy" feel, and short filtered-noise textures for "data"/"transmission".
"""

from __future__ import annotations

import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

SR = 16000


# --------------------------------------------------------------------------- #
# Synthesis primitives  (all return float32 mono in [-1, 1])
# --------------------------------------------------------------------------- #
def _env(n: int, sr: int, attack: float, release: float) -> np.ndarray:
    env = np.ones(n, dtype=np.float32)
    a = min(int(attack * sr), n // 2)
    r = min(int(release * sr), n // 2)
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a)
    if r > 0:
        env[-r:] = np.linspace(1.0, 0.0, r)
    return env


def _norm(sig: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = float(np.max(np.abs(sig))) or 1.0
    return (sig / m * peak).astype(np.float32)


def _tone(
    freq: float,
    dur: float,
    sr: int = SR,
    partials=(1.0,),
    amps=(1.0,),
    decay: float = 0.0,
    attack: float = 0.005,
    release: float = 0.04,
) -> np.ndarray:
    n = int(sr * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    sig = np.zeros(n, dtype=np.float64)
    for p, a in zip(partials, amps):
        sig += a * np.sin(2 * np.pi * freq * p * t)
    if decay:
        sig *= np.exp(-decay * t / dur)
    sig *= _env(n, sr, attack, release)
    return sig.astype(np.float32)


def _glide(
    f0: float,
    f1: float,
    dur: float,
    sr: int = SR,
    decay: float = 2.0,
    attack: float = 0.005,
    release: float = 0.05,
    shimmer: float = 0.3,
) -> np.ndarray:
    n = int(sr * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    freq = np.linspace(f0, f1, n)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    sig = np.sin(phase) + shimmer * np.sin(2 * phase)
    if decay:
        sig *= np.exp(-decay * t / dur)
    sig *= _env(n, sr, attack, release)
    return sig.astype(np.float32)


def _noise(
    dur: float,
    sr: int = SR,
    decay: float = 6.0,
    taps: int = 8,
    seed: int = 1234,
    attack: float = 0.003,
    release: float = 0.03,
) -> np.ndarray:
    n = int(sr * dur)
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    if taps > 1:  # cheap vectorized low-pass for an "airy" rather than hissy texture
        x = np.convolve(x, np.ones(taps) / taps, mode="same")
    t = np.linspace(0.0, dur, n, endpoint=False)
    x *= np.exp(-decay * t / dur) * _env(n, sr, attack, release)
    return x.astype(np.float32)


def _blip(freq: float, dur: float = 0.05, sr: int = SR) -> np.ndarray:
    return _tone(freq, dur, sr, partials=(1.0, 2.0), amps=(1.0, 0.25),
                 decay=4.0, attack=0.004, release=0.02)


def _seq(blips, gap: float = 0.0, sr: int = SR) -> np.ndarray:
    """Concatenate blips with an optional silent gap between them."""
    parts = []
    pad = np.zeros(int(sr * gap), dtype=np.float32)
    for i, b in enumerate(blips):
        if i and pad.size:
            parts.append(pad)
        parts.append(b)
    return np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)


def _place(dst: np.ndarray, src: np.ndarray, at: int, gain: float = 1.0) -> None:
    """Mix `src` into `dst` starting at sample `at` (in place)."""
    end = min(at + src.shape[0], dst.shape[0])
    if end > at:
        dst[at:end] += gain * src[: end - at]


# --------------------------------------------------------------------------- #
# The cue palette
# --------------------------------------------------------------------------- #
def _boot(sr: int = SR) -> np.ndarray:
    """A ~3.2 s power-up bed: rising drone + ascending sparkle arpeggio + resolve."""
    dur = 3.2
    n = int(sr * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)
    out = np.zeros(n, dtype=np.float64)

    # Rising drone (two slightly detuned low voices) swelling in then settling.
    f = np.linspace(110.0, 165.0, n)
    phase = 2 * np.pi * np.cumsum(f) / sr
    drone = np.sin(phase) + 0.6 * np.sin(phase * 1.003) + 0.3 * np.sin(2 * phase)
    swell = np.clip(t / 1.4, 0, 1) * np.clip((dur - t) / 0.9, 0, 1)
    out += 0.5 * drone * swell

    # Airy "systems coming online" texture under the swell.
    bed = _noise(dur, sr, decay=0.6, taps=24, seed=7, attack=0.5, release=0.6)
    out += 0.25 * bed

    # Ascending sparkle arpeggio of glassy blips.
    notes = [392.0, 523.0, 659.0, 784.0, 988.0, 1175.0]
    for i, fr in enumerate(notes):
        b = _tone(fr, 0.22, sr, partials=(1.0, 2.01, 3.0), amps=(1.0, 0.4, 0.15),
                  decay=5.0, attack=0.004, release=0.12)
        _place(out, b, int(sr * (0.5 + i * 0.18)), gain=0.5)

    # A couple of data sweeps for the "informatics" feel.
    _place(out, _glide(700, 1500, 0.25, sr, decay=3, shimmer=0.4), int(sr * 0.35), 0.3)
    _place(out, _glide(900, 1900, 0.30, sr, decay=3, shimmer=0.4), int(sr * 1.5), 0.28)

    # Final resolving bell.
    bell = _tone(1318.5, 0.7, sr, partials=(1.0, 2.0, 3.01, 4.0),
                 amps=(1.0, 0.5, 0.25, 0.12), decay=4.0, attack=0.004, release=0.4)
    _place(out, bell, int(sr * 2.25), gain=0.45)

    return _norm(out, 0.92)


CUES = ("boot", "thinking", "read", "search", "web", "write", "exec", "done", "error", "notify")


def user_sounds_dir() -> Path:
    """Where real (sampled) cue files live, overriding synthesis when present."""
    from ..config import global_dir

    return global_dir() / "sounds"


def _load_wav(path: Path, sr: int = SR) -> np.ndarray | None:
    """Load a 16 kHz mono WAV as float32 in [-1, 1]; None on any problem."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            frames = w.readframes(w.getnframes())
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if w.getframerate() != sr and data.size:  # cheap linear resample
                n = int(round(data.size * sr / w.getframerate()))
                data = np.interp(np.linspace(0, data.size - 1, n),
                                 np.arange(data.size), data).astype(np.float32)
            return data
    except Exception:
        return None


def build_sound_bank(sr: int = SR, sounds_dir: Path | None = None) -> dict[str, np.ndarray]:
    """Build the full palette. A real sample at `<sounds_dir>/<cue>.wav` wins; otherwise
    the cue is synthesized. This lets sampled FUI sounds replace the synth defaults."""
    bank = _synth_bank(sr)
    sdir = sounds_dir if sounds_dir is not None else user_sounds_dir()
    if sdir.is_dir():
        for cue in CUES:
            arr = _load_wav(sdir / f"{cue}.wav", sr)
            if arr is not None and arr.size:
                bank[cue] = arr
    return bank


def _synth_bank(sr: int = SR) -> dict[str, np.ndarray]:
    """Synthesize the full palette once. Keys map to action categories."""
    bank: dict[str, np.ndarray] = {}

    # Soft processing pulse — "thinking".
    think = _seq([
        _tone(330, 0.16, sr, partials=(1, 2), amps=(1, 0.2), decay=3, release=0.08),
        _tone(294, 0.16, sr, partials=(1, 2), amps=(1, 0.2), decay=3, release=0.09),
    ], gap=0.04, sr=sr)
    bank["thinking"] = _norm(think, 0.8)

    # Reading files — quick ascending data blips.
    bank["read"] = _norm(_seq([_blip(720), _blip(960), _blip(1240)], gap=0.012, sr=sr), 0.85)

    # Searching the codebase — a fast scan glide + tick.
    bank["search"] = _norm(_seq([
        _glide(620, 1380, 0.16, sr, decay=2.5, shimmer=0.35), _blip(1500, 0.04)
    ], gap=0.01, sr=sr), 0.85)

    # Web / transmission — uplink sweep with an airy bed.
    web = _glide(1100, 1700, 0.34, sr, decay=1.5, shimmer=0.5)
    _place(web, _noise(0.34, sr, decay=2.0, taps=12, seed=3), 0, 0.25)
    bank["web"] = _norm(web, 0.85)

    # Writing code — a confident two-note "commit" (rising fifth).
    bank["write"] = _norm(_seq([
        _tone(880, 0.10, sr, partials=(1, 2), amps=(1, 0.3), decay=4, release=0.05),
        _tone(1318.5, 0.16, sr, partials=(1, 2, 3), amps=(1, 0.35, 0.12), decay=4, release=0.08),
    ], gap=0.0, sr=sr), 0.85)

    # Executing a command — mechanical thunk into a rising engage tone.
    exe = np.zeros(int(sr * 0.34), dtype=np.float32)
    _place(exe, _tone(140, 0.10, sr, partials=(1, 2, 3), amps=(1, 0.6, 0.3),
                      decay=8, attack=0.001, release=0.04), 0, 0.9)
    _place(exe, _glide(300, 620, 0.22, sr, decay=3, shimmer=0.2), int(sr * 0.08), 0.6)
    bank["exec"] = _norm(exe, 0.85)

    # Done / answer ready — a clean resolving chime.
    bank["done"] = _norm(_seq([
        _tone(1318.5, 0.12, sr, partials=(1, 2, 3), amps=(1, 0.4, 0.15), decay=4, release=0.06),
        _tone(1760.0, 0.30, sr, partials=(1, 2, 3.01), amps=(1, 0.45, 0.18), decay=4, release=0.18),
    ], gap=0.0, sr=sr), 0.85)

    # Error — low detuned descending buzz.
    err = _glide(420, 170, 0.30, sr, decay=2.0, shimmer=0.0)
    err += 0.4 * _glide(426, 173, 0.30, sr, decay=2.0, shimmer=0.0)  # beating detune
    bank["error"] = _norm(err, 0.8)

    # Notify — a gentle attention two-tone (distinct from the wake chime).
    bank["notify"] = _norm(_seq([
        _tone(1760, 0.10, sr, partials=(1, 2), amps=(1, 0.3), decay=5, release=0.05),
        _tone(2349, 0.20, sr, partials=(1, 2), amps=(1, 0.3), decay=5, release=0.12),
    ], gap=0.02, sr=sr), 0.78)

    bank["boot"] = _boot(sr)
    return bank


# --------------------------------------------------------------------------- #
# Mixing player — one persistent stream, callback sums all active cues
# --------------------------------------------------------------------------- #
class SoundPlayer:
    """Low-latency layering player: one output stream, many overlapping cues.

    `play()` is non-blocking (just appends a voice); the PortAudio callback mixes.
    Per-cue `gain` lets quiet SFX and full-volume voice lines share one master.
    """

    def __init__(self, sample_rate: int = SR, device=None, master: float = 0.9, max_voices: int = 16) -> None:
        self.sr = sample_rate
        self.device = device
        self.master = float(master)
        self.max_voices = max_voices
        self._voices: list[dict] = []
        self._lock = threading.Lock()
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.OutputStream(
            samplerate=self.sr,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status) -> None:  # PortAudio thread
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            keep = []
            for v in self._voices:
                buf = v["buf"]
                pos = v["pos"]
                n = min(frames, buf.shape[0] - pos)
                if n > 0:
                    out[:n] += buf[pos:pos + n] * v["gain"]
                    v["pos"] = pos + n
                if v["pos"] < buf.shape[0]:
                    keep.append(v)
            self._voices = keep
        np.multiply(out, self.master, out)
        np.clip(out, -1.0, 1.0, out)
        outdata[:, 0] = out

    def play(self, buf: np.ndarray | None, gain: float = 1.0) -> None:
        """Queue a cue for playback. Safe to call from any thread; never raises."""
        if buf is None or self._stream is None:
            return
        with self._lock:
            if len(self._voices) >= self.max_voices:
                self._voices = self._voices[-(self.max_voices - 1):]
            self._voices.append({"buf": buf, "pos": 0, "gain": float(gain)})

    def stop(self) -> None:
        with self._lock:
            self._voices = []
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
