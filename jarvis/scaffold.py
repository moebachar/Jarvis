"""Project scaffolding for `jarvis --init`.

Writes a starter `<project>/.jarvis/` so someone who just pulled Jarvis into a fresh project
can see and tweak the common knobs in one place. Stdlib-only (no config import), never
overwrites existing files, and returns what it created so the CLI can report it.
"""

from __future__ import annotations

from pathlib import Path

# A friendly, keyless-by-default starter. Kokoro (local, free, offline) means a fresh pull
# speaks with NO API keys at all; swap tts_engine to "elevenlabs" or "xtts" if you want.
_CONFIG_TEMPLATE = """\
# Jarvis — per-project config. Layered: built-in defaults < ~/.jarvis < THIS file < env.
# Everything here is optional; delete what you don't need. Secrets go in .env, never here.

[voice]
# TTS engine: "kokoro" (local, free, no key) | "elevenlabs" (cloud, needs a key) |
#             "xtts" (local zero-shot voice clone; needs the [clone] extra + a GPU).
tts_engine   = "kokoro"
kokoro_voice = "bm_george"     # British male; also bm_lewis, bm_daniel, bm_fable

# Push-to-talk only by default (no wake word): hold this key to talk, press again to interrupt.
wake_enabled = false
ptt_enabled  = true
ptt_key      = "ctrl_r"        # any pynput key name: ctrl_r, alt_r, space, f9, pause, a letter…

# Speech-to-text (local faster-whisper). On a GPU box, set whisper_device = "cuda".
whisper_model  = "base.en"
whisper_device = "auto"

[dashboard]
enabled = true
host    = "127.0.0.1"          # loopback only. To view from another machine, SSH-forward the
port    = 8765                 #   port (see README) — don't expose this on an untrusted network.
auto_open = true               # open the HUD in a browser on start (set false on a headless box).

[telegram]
# Reach you when you're away + reply from your phone. Needs TELEGRAM_BOT_TOKEN +
# JARVIS_TELEGRAM_CHAT_ID in .env (run `jarvis --telegram-id` to discover your chat id).
enabled = false
"""

_ENV_TEMPLATE = """\
# Jarvis secrets for THIS project. Loaded automatically; keep it out of version control.
# (Everything is optional — with tts_engine="kokoro" Jarvis needs NO keys at all.)

# --- ElevenLabs cloud voice (only if tts_engine = "elevenlabs") ---
# ELEVENLABS_API_KEY=
# JARVIS_ELEVENLABS_VOICE_ID=

# --- Telegram presence (only if [telegram] enabled = true) ---
# TELEGRAM_BOT_TOKEN=
# JARVIS_TELEGRAM_CHAT_ID=

# --- Picovoice (only if you switch to wake_engine = "porcupine") ---
# PICOVOICE_ACCESS_KEY=

# NOTE: do NOT set ANTHROPIC_API_KEY here — the brain runs on your Claude subscription via the
# logged-in `claude` CLI, and an API key would shadow it and bill the API. Jarvis unsets it anyway.
"""

_GITIGNORE_LINE = ".env\nstate/\ncache/\n"


def init_project(project_dir: Path) -> tuple[list[Path], list[Path]]:
    """Create `<project>/.jarvis/{config.toml,.env.example,.gitignore}` if absent.

    Returns (created, skipped) lists of paths so the caller can report what happened. Existing
    files are never overwritten — pulling Jarvis into a configured project is safe.
    """
    jarvis_dir = Path(project_dir) / ".jarvis"
    jarvis_dir.mkdir(parents=True, exist_ok=True)

    files = {
        jarvis_dir / "config.toml": _CONFIG_TEMPLATE,
        jarvis_dir / ".env.example": _ENV_TEMPLATE,
        jarvis_dir / ".gitignore": _GITIGNORE_LINE,
    }
    created: list[Path] = []
    skipped: list[Path] = []
    for path, content in files.items():
        if path.exists():
            skipped.append(path)
            continue
        path.write_text(content, encoding="utf-8")
        created.append(path)
    return created, skipped
