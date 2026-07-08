"""Configuration: layered defaults < global (~/.jarvis) < per-project (.jarvis) < env.

Kept dependency-free (stdlib dataclasses + tomllib) so Phase 0 stays lean. Later
phases simply read the already-present fields (voice / telegram / dashboard).

Lookup order, lowest priority first:
  1. Dataclass defaults below.
  2. ~/.jarvis/config.toml          (global, applies to every project)
  3. <project>/.jarvis/config.toml  (per-project override)
  4. Environment variables for secrets (e.g. ELEVENLABS_API_KEY).

Secrets may also live in ~/.jarvis/.env or <project>/.jarvis/.env (KEY=VALUE lines);
those are loaded into the environment first, then picked up by the env override step.
"""

# NOTE: deliberately NOT using `from __future__ import annotations` here — _from_dict()
# relies on dataclasses.field().type being the real class object (not a string) to
# detect and recursively build nested config sections.

import copy
import dataclasses
import os
import tomllib
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Config schema
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class ClaudeConfig:
    # None means "defer to the loaded settings.json" (most faithful to requirement #3).
    # We default to "auto" to match the user's existing settings.json (defaultMode: auto)
    # and guarantee autonomous, prompt-free operation in this headless context.
    permission_mode: str | None = "auto"
    setting_sources: list[str] = dataclasses.field(
        default_factory=lambda: ["user", "project", "local"]
    )
    model: str | None = None  # None => inherit from settings.json / SDK default
    # Extra built-in tools to pre-approve (belt-and-suspenders alongside permission_mode).
    extra_allowed_tools: list[str] = dataclasses.field(
        default_factory=lambda: ["Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "TodoWrite"]
    )


@dataclasses.dataclass
class VoiceConfig:
    # Wake word. Jarvis is BUTTON-ONLY by default: you call him and interrupt him with
    # the push-to-talk key (below), never by voice. Set wake_enabled = true to also allow
    # the spoken wake word / voice barge-in via the engine below.
    wake_enabled: bool = False            # False => no wake word at all (push-to-talk only)
    # Wake word engine (only used when wake_enabled = true):
    #   "vosk"        -> keyless single word "jarvis" (offline recognizer)
    #   "openwakeword"-> keyless, but only the phrase "hey jarvis"
    #   "porcupine"   -> single word "jarvis", needs a free Picovoice access key
    wake_engine: str = "vosk"
    wake_word: str = "jarvis"             # the word to wake/interrupt on
    wake_phrase: str = "jarvis"           # what we display to the user

    # Vosk (default engine):
    vosk_model_path: str | None = None    # None => auto-download the small en-us model

    # openWakeWord (when wake_engine == "openwakeword"):
    oww_model: str = "hey_jarvis"
    oww_threshold: float = 0.5

    # Porcupine (when wake_engine == "porcupine"):
    wake_sensitivity: float = 0.5
    picovoice_access_key: str | None = None

    # Text-to-speech engine:
    #   "elevenlabs" -> premium cloud voice (needs a key + credits)
    #   "kokoro"     -> local, free, offline neural TTS (82M model; runs on CPU or GPU)
    #   "xtts"       -> local zero-shot VOICE CLONE (XTTS-v2): speaks in the timbre of a
    #                   reference clip you provide (xtts_reference). Best on a GPU.
    tts_engine: str = "elevenlabs"

    # ElevenLabs (when tts_engine == "elevenlabs").
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model_id: str = "eleven_turbo_v2_5"  # low latency, multilingual
    # Voice delivery — tuned for a composed, crisp Jarvis (not a narrator):
    tts_speed: float = 1.15           # 0.7..1.2; higher = faster speech
    tts_stability: float = 0.6        # higher = more even/controlled (less theatrical)
    tts_style: float = 0.0            # higher = more expressive/narrative; 0 = neutral
    tts_similarity_boost: float = 0.8
    tts_speaker_boost: bool = True

    # Kokoro (when tts_engine == "kokoro") — local, free, no key (kokoro-onnx).
    kokoro_voice: str = "bm_george"   # British male; also bm_lewis, bm_daniel, bm_fable
    kokoro_lang: str = "en-gb"       # "en-gb" British, "en-us" American
    kokoro_speed: float = 1.0        # 0.5..2.0; Kokoro's own speed dial
    kokoro_device: str = "auto"      # "auto" (gpu if onnxruntime-gpu present) | "cuda" | "cpu"
    kokoro_sample_rate: int = 24000  # Kokoro's native output rate (do not change)
    # Model files (None => auto-download to ~/.jarvis/cache/kokoro/ on first use).
    kokoro_model_path: str | None = None
    kokoro_voices_path: str | None = None

    # XTTS-v2 voice cloning (when tts_engine == "xtts") — zero-shot: no training, it clones
    # the voice in `xtts_reference` on the fly. Needs `pip install coqui-tts` (its own env;
    # Python 3.11 recommended) and, for real-time, a CUDA GPU. First use downloads ~1.8 GB.
    xtts_reference: str | None = None      # path to a clean ~6–30 s mono WAV of the target voice
    xtts_language: str = "en"
    xtts_temperature: float = 0.7          # lower = steadier delivery; higher = more variation
    xtts_device: str = "auto"              # "auto" (gpu if torch sees CUDA) | "cuda" | "cpu"
    xtts_sample_rate: int = 24000          # XTTS-v2 native output rate (do not change)
    xtts_model_dir: str | None = None      # None => auto-download the XTTS-v2 checkpoint (Coqui)

    # Speech-to-text (faster-whisper, local).
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"          # "cpu" | "cuda" | "auto"
    whisper_compute_type: str = "int8"   # "int8" | "float16" | "default"
    whisper_beam_size: int = 1           # 1 = fastest; raise for accuracy

    # Audio devices (None => system default). Index or name substring.
    input_device: str | int | None = None
    output_device: str | int | None = None

    # Software mic gain — multiply incoming audio so a quiet/far mic is "heard" better
    # (helps wake detection, end-of-speech, and transcription). Raise if you must lean in;
    # lower if loud speech distorts. 1.0 = no change.
    input_gain: float = 1.0

    # End-of-utterance / conversation timing.
    # End-of-speech detection. "silero" = neural speech/non-speech (robust to noise,
    # recommended); "energy" = the simple loudness fallback.
    vad_engine: str = "silero"
    silero_model_path: str | None = None   # None => use the model openWakeWord downloaded
    silero_threshold: float = 0.5          # speech probability needed to count as speech
    silence_timeout: float = 0.7           # trailing silence (s) that ends an utterance
    max_utterance_seconds: float = 15.0
    start_guard_seconds: float = 0.2       # (energy fallback) ignore mic this long after chime
    vad_start_active_seconds: float = 0.2  # sustained speech needed to START a capture
    vad_end_threshold_ratio: float = 0.45  # (energy fallback) hysteresis ratio
    follow_up_seconds: float = 6.0       # how long to keep listening after Jarvis replies

    # Push-to-talk: hold a key to talk (and press it again to interrupt) — this is the
    # primary and, by default, ONLY way to reach Jarvis. Defaults to Right Ctrl: a
    # non-typing key that never needs an Fn modifier and never inserts characters.
    # Any pynput key name works: "ctrl_r", "alt_r", "space", "f9", "pause", a letter, etc.
    ptt_enabled: bool = True
    ptt_key: str = "ctrl_r"

    # Audio transport: where the mic/speaker live.
    #   "local"   -> this machine's sounddevice mic + speakers (the default).
    #   "browser" -> a remote/tunneled browser tab (the dashboard) is the mic + speaker;
    #                this process does STT + TTS (ideally on a GPU). Used with `--remote`:
    #                run Jarvis on a desktop, SSH-forward the dashboard port to a laptop,
    #                open http://localhost:<port> there and talk through the browser.
    transport: str = "local"

    energy_threshold: float | None = None  # None => auto-calibrate from ambient noise
    ack_sound: bool = True               # play the activation chime when the wake word fires
    ack_volume: float = 0.45             # loudness of the chime (0..1)

    # Phase 3 presence check (voice mode). When Jarvis has a queued note to deliver and isn't
    # certain you're still at the machine, he asks `presence_prompt` aloud and listens
    # (presence_listen_seconds): answered → he speaks the note; silence → it goes to Telegram.
    #   presence_fresh_seconds: if you spoke this recently he KNOWS you're here and skips the
    #     question, speaking the note straight away.
    #   presence_max_seconds: if your last local activity is older than this he won't bother
    #     trying voice at all — he just uses Telegram (you're clearly away).
    presence_fresh_seconds: float = 45.0
    presence_max_seconds: float = 600.0
    presence_prompt: str = "Sir, are you still here?"

    # Cinematic audio layer — the "movie Jarvis" feel. NOTE: the annoying per-tool "blips"
    # and canned "on it, sir" lines are gone; sfx_enabled now only covers the boot power-up
    # and the notify/error cues. Progress is narrated by Jarvis himself (see narrate_work).
    sfx_enabled: bool = True             # boot power-up + notify/error cues (no per-tool blips)
    sfx_volume: float = 0.35             # loudness of cues (0..1); sits under the voice
    boot_sound: bool = True              # power-up ambience + spoken welcome at launch
    boot_line: str = "Welcome back, sir. All systems will be online in a moment."
    boot_speed: float = 0.85             # intro is spoken slower/more deliberate than normal replies
    # narrate_work: in voice mode, have Jarvis SPEAK brief, dynamic, high-level progress in
    # his own words as he works ("Searching the web for X… found promising leads, editing Y").
    # This drives a persona instruction — not pre-recorded lines. Off => he works silently.
    narrate_work: bool = True
    voice_line_volume: float = 0.9       # loudness of cached voice lines (welcome)


@dataclasses.dataclass
class TelegramConfig:
    # Presence/away channel (Phase 3). Auto-activates when a bot_token is present; set
    # enabled=false to keep the token configured but turn the bridge off for a run.
    enabled: bool = True
    bot_token: str | None = None      # from @BotFather; set via TELEGRAM_BOT_TOKEN in .env
    chat_id: str | None = None        # your chat id; discover it with `jarvis --telegram-id`


@dataclasses.dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    auto_open: bool = True


@dataclasses.dataclass
class JarvisConfig:
    user_title: str = "sir"
    heartbeat_minutes: int = 15
    presence_listen_seconds: int = 20
    claude: ClaudeConfig = dataclasses.field(default_factory=ClaudeConfig)
    voice: VoiceConfig = dataclasses.field(default_factory=VoiceConfig)
    telegram: TelegramConfig = dataclasses.field(default_factory=TelegramConfig)
    dashboard: DashboardConfig = dataclasses.field(default_factory=DashboardConfig)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def global_dir() -> Path:
    return Path.home() / ".jarvis"


def project_jarvis_dir(project_dir: Path) -> Path:
    return Path(project_dir) / ".jarvis"


def state_dir(project_dir: Path) -> Path:
    return project_jarvis_dir(project_dir) / "state"


# --------------------------------------------------------------------------- #
# Loading / merging
# --------------------------------------------------------------------------- #
def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: set KEY=VALUE into os.environ if not already present.

    Handles `export ` prefixes, surrounding quotes, and inline `# comments`
    (a `#` preceded by whitespace ends the value) so a commented line like
    `KEY=value   # note` yields just `value`.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = val.strip()

        if val[:1] in ("'", '"'):
            quote = val[0]
            end = val.find(quote, 1)
            val = val[1:end] if end != -1 else val[1:]
        else:
            # An inline comment must be preceded by whitespace (so '#' inside a
            # token, e.g. a colour hex, is preserved).
            for i in range(1, len(val)):
                if val[i] == "#" and val[i - 1] in " \t":
                    val = val[:i]
                    break
            val = val.strip()

        os.environ.setdefault(key, val)


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _from_dict(cls: type, data: dict) -> Any:
    """Build a (possibly nested) dataclass from a plain dict, ignoring unknown keys."""
    if not dataclasses.is_dataclass(cls):
        return data
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if field.name not in data:
            continue
        value = data[field.name]
        if dataclasses.is_dataclass(field.type) and isinstance(value, dict):
            kwargs[field.name] = _from_dict(field.type, value)
        else:
            kwargs[field.name] = value
    return cls(**kwargs)


# Environment variable -> (section, field) overrides for secrets.
_ENV_OVERRIDES = {
    "ELEVENLABS_API_KEY": ("voice", "elevenlabs_api_key"),
    "JARVIS_ELEVENLABS_API_KEY": ("voice", "elevenlabs_api_key"),
    "JARVIS_ELEVENLABS_VOICE_ID": ("voice", "elevenlabs_voice_id"),
    "PICOVOICE_ACCESS_KEY": ("voice", "picovoice_access_key"),
    "JARVIS_PICOVOICE_ACCESS_KEY": ("voice", "picovoice_access_key"),
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "JARVIS_TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "JARVIS_TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "JARVIS_VOICE_TRANSPORT": ("voice", "transport"),
}


def _apply_env_overrides(merged: dict) -> None:
    for env_key, (section, field) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val:
            merged.setdefault(section, {})[field] = val


def load_config(project_dir: str | Path) -> JarvisConfig:
    project_dir = Path(project_dir)

    # 1. Load .env files into the environment. _load_dotenv uses setdefault, so the FIRST
    #    file to define a variable wins — load most-specific first:
    #    <project>/.jarvis/.env  >  <project>/.env (root)  >  ~/.jarvis/.env (global).
    _load_dotenv(project_jarvis_dir(project_dir) / ".env")
    _load_dotenv(project_dir / ".env")
    _load_dotenv(global_dir() / ".env")

    # 2. Merge defaults < global toml < project toml.
    merged = dataclasses.asdict(JarvisConfig())
    merged = _deep_merge(merged, _read_toml(global_dir() / "config.toml"))
    merged = _deep_merge(merged, _read_toml(project_jarvis_dir(project_dir) / "config.toml"))

    # 3. Secrets from environment win.
    _apply_env_overrides(merged)

    return _from_dict(JarvisConfig, merged)
