"""Text-to-speech via ElevenLabs (streaming PCM) with stop-for-barge-in support.

We request raw `pcm_16000` so playback is a straight write to a sounddevice output
stream — no mp3 decoder needed. Audio is written in small slices and the stop flag is
checked between them, so a barge-in cuts Jarvis off within a few tens of milliseconds.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

# ElevenLabs is imported lazily (inside the ElevenLabs paths) so a machine using only the
# local Kokoro engine needn't have the `elevenlabs` package installed.

_SLICE_BYTES = 2048  # ~64 ms at 16 kHz mono int16; sets barge-in cut latency


def describe_tts_error(exc: Exception) -> tuple[str, bool]:
    """Turn a raw ElevenLabs/HTTP failure into a short, human-readable message.

    ElevenLabs' `ApiError` stringifies to a full `headers: {...}, status_code, body: {...}`
    dump — dozens of lines of HTTP noise that flood the dashboard feed when we surface it
    verbatim. This extracts just the meaningful sentence. Returns `(message, is_quota)`;
    `is_quota` flags credit/quota exhaustion so the caller can stop retrying (every sentence
    of a reply would otherwise re-hit the dead quota) for the rest of the session.
    """
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    code = message = ""
    if isinstance(detail, dict):
        code = str(detail.get("code") or detail.get("status") or "")
        message = str(detail.get("message") or "")
    elif isinstance(detail, str):
        message = detail
    blob = f"{code} {message}".lower()
    is_quota = "quota" in blob or "credits remaining" in blob or "exceeds your quota" in blob
    if is_quota:
        return ("Out of ElevenLabs voice credits — I'll carry on in text only, sir.", True)
    if message:
        short = message if len(message) <= 160 else message[:157] + "…"
        prefix = f"Voice unavailable ({status})" if status else "Voice unavailable"
        return (f"{prefix}: {short}", False)
    text = (str(exc) or exc.__class__.__name__).replace("\n", " ")
    if len(text) > 160:
        text = text[:157] + "…"
    return (f"Voice error: {text}", False)


def build_voice_settings(voice_config, speed: float | None = None):
    """Map our config's delivery dials to an ElevenLabs VoiceSettings.

    `speed` overrides the configured `tts_speed` for one-off renders (e.g. a slower,
    more deliberate boot greeting) without disturbing normal reply delivery.
    """
    from elevenlabs.types.voice_settings import VoiceSettings

    return VoiceSettings(
        stability=voice_config.tts_stability,
        similarity_boost=voice_config.tts_similarity_boost,
        style=voice_config.tts_style,
        use_speaker_boost=voice_config.tts_speaker_boost,
        speed=voice_config.tts_speed if speed is None else speed,
    )


def _envelope(n: int, sample_rate: int, attack: float, release: float) -> np.ndarray:
    env = np.ones(n)
    a = min(int(attack * sample_rate), n // 2)
    r = min(int(release * sample_rate), n // 2)
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a)
    if r > 0:
        env[-r:] = np.linspace(1.0, 0.0, r)
    return env


def synth_activation_chime(sample_rate: int = 16000, volume: float = 0.45) -> np.ndarray:
    """A short, cinematic 'listening' sound: a rising sweep into a glassy bell ping.

    Wordless, synthesized — no asset files. Returns an int16 mono waveform.
    """
    sr = sample_rate

    # Part A — quick upward sweep ("powering up / attentive").
    dur_a = 0.14
    n_a = int(sr * dur_a)
    t_a = np.linspace(0, dur_a, n_a, endpoint=False)
    freq = np.linspace(500.0, 1200.0, n_a)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    sweep = np.sin(phase) + 0.3 * np.sin(2 * phase)
    sweep *= np.exp(-3.0 * t_a / dur_a) * _envelope(n_a, sr, 0.008, 0.03)

    gap = np.zeros(int(sr * 0.012))

    # Part B — glassy bell "ping" (inharmonic partials) that resolves and decays.
    dur_b = 0.22
    n_b = int(sr * dur_b)
    t_b = np.linspace(0, dur_b, n_b, endpoint=False)
    f_b = 1500.0
    bell = (
        np.sin(2 * np.pi * f_b * t_b)
        + 0.5 * np.sin(2 * np.pi * f_b * 2.0 * t_b)
        + 0.25 * np.sin(2 * np.pi * f_b * 3.01 * t_b)
    )
    bell *= np.exp(-7.0 * t_b / dur_b) * _envelope(n_b, sr, 0.004, 0.08)

    sig = np.concatenate([sweep, gap, bell])
    peak = float(np.max(np.abs(sig))) or 1.0
    sig = sig / peak * max(0.0, min(volume, 1.0))
    return (sig * 32767).astype(np.int16)


def play_activation_chime(sample_rate: int = 16000, volume: float = 0.45, device=None) -> None:
    """Play the 'I'm listening' chime. Best-effort; never raises."""
    try:
        wave = synth_activation_chime(sample_rate, volume)
        sd.play(wave, samplerate=sample_rate, device=device)
        sd.wait()
    except Exception:
        pass


def play_tone(
    frequency: float = 880.0,
    duration: float = 0.12,
    sample_rate: int = 16000,
    volume: float = 0.25,
    device=None,
) -> None:
    """Play a short sine tone (used for the barge-in acknowledgement). Never raises."""
    try:
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
        wave = (np.sin(2 * np.pi * frequency * t) * volume * 32767).astype(np.int16)
        fade = min(256, wave.size // 4)
        if fade:
            ramp = np.linspace(0, 1, fade)
            wave[:fade] = (wave[:fade] * ramp).astype(np.int16)
            wave[-fade:] = (wave[-fade:] * ramp[::-1]).astype(np.int16)
        sd.play(wave, samplerate=sample_rate, device=device)
        sd.wait()
    except Exception:
        pass


class _SoundDeviceSink:
    """The default speaker sink: a sounddevice RawOutputStream. `.write(bytes)` back-pressures
    at real time (which is what lets a barge-in cut within ~64 ms)."""

    def __init__(self, sample_rate: int, output_device) -> None:
        self._out = sd.RawOutputStream(
            samplerate=sample_rate, channels=1, dtype="int16", device=output_device
        )
        self._out.start()

    def write(self, slice_bytes: bytes) -> None:
        self._out.write(slice_bytes)

    def close(self) -> None:
        try:
            self._out.stop()
            self._out.close()
        except Exception:
            pass


class _PcmSpeaker:
    """Base speaker: stream raw int16 mono PCM to a SINK, sliced so a barge-in (`stop()`)
    cuts within a few tens of ms, feeding `on_level` for the dashboard's audio-reactive bulb.
    Engines subclass this and implement `_pcm_chunks()`; the audio destination is `_open_sink()`
    (sounddevice by default, a WebSocket in remote/browser mode). Everything else — barge-in,
    level metering, the slice loop — is shared and engine/transport-agnostic.
    """

    def __init__(self, *, sample_rate: int = 16000, output_device=None, on_level=None) -> None:
        self._sample_rate = sample_rate
        self._output_device = output_device
        self._on_level = on_level  # callback(level 0..1) for the dashboard's audio reaction
        self._stop = threading.Event()

    def stop(self) -> None:
        """Signal the current speak() to abort as soon as possible (barge-in)."""
        self._stop.set()

    def _pcm_chunks(self, text: str):
        """Yield raw int16 mono PCM bytes at self._sample_rate for `text`. Override."""
        raise NotImplementedError

    def _open_sink(self):
        """Return the audio sink (an object with .write(bytes) and .close()). Overridable."""
        return _SoundDeviceSink(self._sample_rate, self._output_device)

    def _emit_level(self, slice_bytes: bytes) -> None:
        if self._on_level is None:
            return
        samples = np.frombuffer(slice_bytes, dtype=np.int16)
        if samples.size:
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            self._on_level(min(1.0, rms / 4000.0))

    def speak(self, text: str) -> None:
        """Synthesize and play `text`. Blocking — call via asyncio.to_thread."""
        text = text.strip()
        if not text:
            return
        self._stop.clear()
        sink = self._open_sink()
        try:
            buffer = b""
            for chunk in self._pcm_chunks(text):
                if self._stop.is_set():
                    break
                if not chunk:
                    continue
                buffer += chunk
                while len(buffer) >= _SLICE_BYTES:
                    if self._stop.is_set():
                        buffer = b""
                        break
                    slice_bytes = buffer[:_SLICE_BYTES]
                    sink.write(slice_bytes)
                    buffer = buffer[_SLICE_BYTES:]
                    self._emit_level(slice_bytes)
            if not self._stop.is_set() and buffer:
                tail = buffer[: len(buffer) - (len(buffer) % 2)]  # keep even bytes
                if tail:
                    sink.write(tail)
                    self._emit_level(tail)
        finally:
            sink.close()


class ElevenLabsSpeaker(_PcmSpeaker):
    """Premium cloud voice — streams pcm_<rate> straight from ElevenLabs."""

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model_id: str = "eleven_turbo_v2_5",
        sample_rate: int = 16000,
        output_device=None,
        voice_settings=None,
        on_level=None,
    ) -> None:
        super().__init__(sample_rate=sample_rate, output_device=output_device, on_level=on_level)
        from elevenlabs.client import ElevenLabs

        self._client = ElevenLabs(api_key=api_key)
        self._voice_id = voice_id
        self._model_id = model_id
        self._voice_settings = voice_settings

    def _pcm_chunks(self, text: str):
        yield from self._client.text_to_speech.stream(
            self._voice_id,
            text=text,
            model_id=self._model_id,
            output_format=f"pcm_{self._sample_rate}",
            voice_settings=self._voice_settings,
        )


# Back-compat alias: existing callers that did `from .tts import Speaker`.
Speaker = ElevenLabsSpeaker


_KOKORO_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_KOKORO_MODEL_URL = f"{_KOKORO_RELEASE}/kokoro-v1.0.onnx"
_KOKORO_VOICES_URL = f"{_KOKORO_RELEASE}/voices-v1.0.bin"

# One loaded Kokoro model per (paths, provider) — shared by every speaker + the boot bank,
# so the ~310 MB ONNX graph loads at most once per process.
_kokoro_models: dict[tuple, object] = {}
_kokoro_lock = threading.Lock()


def _kokoro_paths(voice_config) -> tuple[str, str]:
    from ..config import global_dir

    d = global_dir() / "cache" / "kokoro"
    model = getattr(voice_config, "kokoro_model_path", None) or str(d / "kokoro-v1.0.onnx")
    voices = getattr(voice_config, "kokoro_voices_path", None) or str(d / "voices-v1.0.bin")
    return model, voices


def _ensure_kokoro_models(model_path: str, voices_path: str) -> None:
    """Download the ONNX model + voices to the cache on first use (once). Blocking.

    Uses a socket timeout + a couple of retries so a stalled connection FAILS instead of hanging
    forever (a no-timeout download can't even be Ctrl+C'd since it blocks in native I/O). If the
    download can't reach GitHub, place the two files in the cache dir by hand — see the error.
    """
    import shutil
    import time
    import urllib.request

    for path, url in ((model_path, _KOKORO_MODEL_URL), (voices_path, _KOKORO_VOICES_URL)):
        p = Path(path)
        if p.is_file() and p.stat().st_size > 0:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                print(f"   Downloading {p.name} (attempt {attempt + 1}/3)…", flush=True)
                req = urllib.request.Request(url, headers={"User-Agent": "jarvis"})
                with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as fh:  # noqa: S310
                    shutil.copyfileobj(r, fh, length=1 << 20)
                tmp.replace(p)
                last_err = None
                break
            except Exception as exc:  # network stall / DNS / HTTP error
                last_err = exc
                tmp.unlink(missing_ok=True)
                time.sleep(2)
        if last_err is not None:
            raise RuntimeError(
                f"Couldn't download the Kokoro model '{p.name}' from {url} ({last_err}). "
                f"If this machine can't reach GitHub, download the file elsewhere and place it at "
                f"{p} (also kokoro-v1.0.onnx + voices-v1.0.bin), then rerun."
            ) from last_err


def load_kokoro(voice_config):
    """Return a cached kokoro-onnx `Kokoro` model for the configured voice/device.

    Device → ONNX provider: 'cuda' forces CUDAExecutionProvider (needs onnxruntime-gpu),
    'cpu' forces CPU, 'auto' lets kokoro-onnx pick (GPU if onnxruntime-gpu is installed).
    """
    model_path, voices_path = _kokoro_paths(voice_config)
    device = getattr(voice_config, "kokoro_device", "auto")
    if device == "cuda":
        os.environ.setdefault("ONNX_PROVIDER", "CUDAExecutionProvider")
    elif device == "cpu":
        os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"
    key = (model_path, voices_path, os.environ.get("ONNX_PROVIDER", "auto"))
    with _kokoro_lock:
        inst = _kokoro_models.get(key)
        if inst is None:
            _ensure_kokoro_models(model_path, voices_path)
            from kokoro_onnx import Kokoro

            inst = Kokoro(model_path, voices_path)
            _kokoro_models[key] = inst
    return inst


def kokoro_providers(inst) -> list[str]:
    """The ONNX Runtime execution providers Kokoro actually loaded, for a startup log.

    onnxruntime SILENTLY falls back to CPUExecutionProvider when CUDAExecutionProvider is asked
    for but onnxruntime-gpu (or a matching CUDA/cuDNN) isn't present — the single most likely
    reason a 'cuda' config still runs on the CPU. Reading the session's providers back makes that
    fallback visible instead of a mystery. Best-effort: the session attribute name can vary.
    """
    for attr in ("sess", "session", "model"):
        sess = getattr(inst, attr, None)
        get = getattr(sess, "get_providers", None)
        if callable(get):
            try:
                return list(get())
            except Exception:
                pass
    return []


class KokoroSpeaker(_PcmSpeaker):
    """Local, free, offline neural TTS (Kokoro-82M via kokoro-onnx). No key, no quota.

    The model + voices load lazily on the first spoken line (and are cached process-wide).
    Kokoro renders float32 audio at 24 kHz; we convert to int16 PCM and hand it to the
    shared player, so barge-in and the dashboard bulb work exactly as with ElevenLabs.
    """

    def __init__(self, *, voice_config, on_level=None) -> None:
        super().__init__(
            sample_rate=voice_config.kokoro_sample_rate,
            output_device=voice_config.output_device,
            on_level=on_level,
        )
        self._cfg = voice_config
        self._kokoro = None

    def _ensure(self):
        if self._kokoro is None:
            self._kokoro = load_kokoro(self._cfg)
        return self._kokoro

    def _pcm_chunks(self, text: str):
        kokoro = self._ensure()
        audio, _ = kokoro.create(
            text,
            voice=self._cfg.kokoro_voice,
            speed=self._cfg.kokoro_speed,
            lang=self._cfg.kokoro_lang,
        )
        yield _kokoro_audio_to_pcm(audio)


def _kokoro_audio_to_pcm(audio) -> bytes:
    """Kokoro segment (numpy float32 in -1..1) -> int16 mono PCM bytes."""
    arr = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return (arr * 32767.0).astype(np.int16).tobytes()


# --------------------------------------------------------------------------- #
# XTTS-v2 voice cloning (zero-shot) — tts_engine == "xtts"
# --------------------------------------------------------------------------- #
def _float_to_pcm(chunk) -> bytes:
    """A float waveform chunk (numpy array OR a torch tensor, mono, in -1..1) -> int16 PCM.

    Handles torch tensors (XTTS streams them) by moving to CPU/numpy first, so the shared
    _PcmSpeaker slice loop can treat every engine's output identically.
    """
    if hasattr(chunk, "detach"):  # a torch tensor
        chunk = chunk.detach().to("cpu").numpy()
    arr = np.clip(np.asarray(chunk, dtype=np.float32).ravel(), -1.0, 1.0)
    return (arr * 32767.0).astype(np.int16).tobytes()


class _XttsBundle:
    """A loaded XTTS-v2 model plus the conditioning latents for ONE reference voice.

    The latents are the expensive part of cloning; computing them once here (not per sentence)
    is what keeps per-utterance latency low.
    """

    def __init__(self, model, gpt_cond_latent, speaker_embedding, sample_rate, device) -> None:
        self.model = model
        self.gpt_cond_latent = gpt_cond_latent
        self.speaker_embedding = speaker_embedding
        self.sample_rate = sample_rate
        self.device = device


_xtts_models: dict[tuple, object] = {}
_xtts_lock = threading.Lock()


def _build_xtts(voice_config, ref: str, device: str) -> _XttsBundle:
    # Accepting the Coqui model licence is required for the auto-download of XTTS-v2. The user
    # opted into this engine explicitly; we don't set it for any other path.
    os.environ.setdefault("COQUI_TOS_AGREED", "1")
    try:
        import torch
    except ImportError as exc:  # torch ships with coqui-tts; a clear message beats a stack trace
        raise RuntimeError(
            "The voice-clone engine needs coqui-tts (which brings torch). Install it in its own "
            "environment: `pip install coqui-tts` (Python 3.11 recommended), and for GPU install "
            "a CUDA torch build. See config.voice-clone.example.toml."
        ) from exc
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model_dir = getattr(voice_config, "xtts_model_dir", None)
    if model_dir:  # user-supplied checkpoint directory (config.json + model.pth + vocab.json)
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import Xtts

        config = XttsConfig()
        config.load_json(str(Path(model_dir) / "config.json"))
        model = Xtts.init_from_config(config)
        model.load_checkpoint(config, checkpoint_dir=str(model_dir), use_deepspeed=False)
        model.to(device)
    else:  # let Coqui manage the download + checkpoint dir, then reach the underlying Xtts model
        try:
            from TTS.api import TTS
        except ImportError as exc:
            raise RuntimeError(
                "The voice-clone engine needs coqui-tts: `pip install coqui-tts` "
                "(Python 3.11 recommended). See config.voice-clone.example.toml."
            ) from exc
        api = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
        model = api.synthesizer.tts_model

    gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(audio_path=[ref])
    sample_rate = int(getattr(model.config, "output_sample_rate", 0) or
                      getattr(voice_config, "xtts_sample_rate", 24000))
    return _XttsBundle(model, gpt_cond_latent, speaker_embedding, sample_rate, device)


def load_xtts(voice_config) -> _XttsBundle:
    """Return a cached XTTS-v2 model + conditioning latents for the configured reference clip.

    The reference clip (`xtts_reference`) is a clean ~6–30 s mono WAV of the voice to imitate;
    XTTS clones it zero-shot (no training). Cached per (reference, device, checkpoint) so the
    ~1.8 GB model loads at most once per process.
    """
    ref = getattr(voice_config, "xtts_reference", None)
    if not ref:
        raise RuntimeError(
            "tts_engine='xtts' needs xtts_reference — a path to a clean ~15 s mono WAV of the "
            "voice to clone. Set it in .jarvis/config.toml under [voice]."
        )
    ref = str(Path(ref).expanduser())
    if not Path(ref).is_file():
        raise RuntimeError(f"xtts_reference clip not found: {ref}")
    device = getattr(voice_config, "xtts_device", "auto")
    key = (ref, device, getattr(voice_config, "xtts_model_dir", None))
    with _xtts_lock:
        inst = _xtts_models.get(key)
        if inst is None:
            inst = _build_xtts(voice_config, ref, device)
            _xtts_models[key] = inst
    return inst


class XttsSpeaker(_PcmSpeaker):
    """Local zero-shot voice CLONE (XTTS-v2 via coqui-tts). Speaks in the timbre of a reference
    clip — no training. Streams per sentence with cached conditioning latents for low latency;
    barge-in and the dashboard bulb work exactly as with the other engines.
    """

    def __init__(self, *, voice_config, on_level=None) -> None:
        super().__init__(
            sample_rate=voice_config.xtts_sample_rate,
            output_device=voice_config.output_device,
            on_level=on_level,
        )
        self._cfg = voice_config
        self._bundle = None

    def _ensure(self) -> _XttsBundle:
        if self._bundle is None:
            self._bundle = load_xtts(self._cfg)
            self._sample_rate = self._bundle.sample_rate  # trust the model's real rate
        return self._bundle

    def _pcm_chunks(self, text: str):
        b = self._ensure()
        stream = b.model.inference_stream(
            text,
            self._cfg.xtts_language,
            b.gpt_cond_latent,
            b.speaker_embedding,
            temperature=self._cfg.xtts_temperature,
        )
        for chunk in stream:
            yield _float_to_pcm(chunk)


def build_speaker(voice_config, *, on_level=None) -> _PcmSpeaker:
    """Construct the speaker for the configured `tts_engine` (elevenlabs | kokoro)."""
    engine = getattr(voice_config, "tts_engine", "elevenlabs")
    if engine == "kokoro":
        return KokoroSpeaker(voice_config=voice_config, on_level=on_level)
    if engine == "xtts":
        return XttsSpeaker(voice_config=voice_config, on_level=on_level)
    return ElevenLabsSpeaker(
        api_key=voice_config.elevenlabs_api_key,
        voice_id=voice_config.elevenlabs_voice_id,
        model_id=voice_config.elevenlabs_model_id,
        sample_rate=16000,
        output_device=voice_config.output_device,
        voice_settings=build_voice_settings(voice_config),
        on_level=on_level,
    )


def _resample_int16(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap linear resample of an int16 mono waveform (for the cached boot line)."""
    if src_rate == dst_rate or pcm.size == 0:
        return pcm
    duration = pcm.size / float(src_rate)
    dst_n = max(1, int(round(duration * dst_rate)))
    src_t = np.linspace(0.0, duration, num=pcm.size, endpoint=False)
    dst_t = np.linspace(0.0, duration, num=dst_n, endpoint=False)
    out = np.interp(dst_t, src_t, pcm.astype(np.float32))
    return out.astype(np.int16)


def render_utterance(voice_config, text: str, speed: float | None = None,
                     sample_rate: int = 16000) -> np.ndarray:
    """Blocking, non-streaming synth of one line -> int16 mono PCM at `sample_rate`.

    Routes to the configured engine; used for the cached boot greeting (VoiceBank).
    Kokoro renders at 24 kHz and is resampled down to `sample_rate` for the cache.
    """
    engine = getattr(voice_config, "tts_engine", "elevenlabs")
    if engine == "kokoro":
        kokoro = load_kokoro(voice_config)
        spd = voice_config.kokoro_speed if speed is None else speed
        audio, _ = kokoro.create(
            text, voice=voice_config.kokoro_voice, speed=spd, lang=voice_config.kokoro_lang
        )
        pcm = np.frombuffer(_kokoro_audio_to_pcm(audio), dtype=np.int16)
        return _resample_int16(pcm, voice_config.kokoro_sample_rate, sample_rate)
    if engine == "xtts":
        b = load_xtts(voice_config)
        out = b.model.inference(
            text, voice_config.xtts_language, b.gpt_cond_latent, b.speaker_embedding,
            temperature=voice_config.xtts_temperature,
        )
        pcm = np.frombuffer(_float_to_pcm(out["wav"]), dtype=np.int16)
        return _resample_int16(pcm, b.sample_rate, sample_rate)
    # ElevenLabs
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=voice_config.elevenlabs_api_key)
    audio = client.text_to_speech.convert(
        voice_config.elevenlabs_voice_id,
        text=text,
        model_id=voice_config.elevenlabs_model_id,
        output_format=f"pcm_{sample_rate}",
        voice_settings=build_voice_settings(voice_config, speed=speed),
    )
    raw = b"".join(chunk for chunk in audio if chunk)
    return np.frombuffer(raw, dtype=np.int16)
