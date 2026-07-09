#!/usr/bin/env sh
# Jarvis container entrypoint: wire subscription auth, point at the baked-in models, pick the
# voice, then run the remote (browser-transport) voice loop on the mounted project.
set -e

# Subscription auth ONLY. ANTHROPIC_API_KEY would outrank CLAUDE_CODE_OAUTH_TOKEN and bill the
# API, so make sure it can never leak in.
unset ANTHROPIC_API_KEY

# The browser tab is the mic + speaker; bind the dashboard to all interfaces INSIDE the container.
# The security boundary is the host port map (compose publishes 127.0.0.1:8765), not this bind.
export JARVIS_DASHBOARD_HOST="${JARVIS_DASHBOARD_HOST:-0.0.0.0}"

# Use the models baked into the image — no downloads at run time.
export JARVIS_WHISPER_MODEL="${JARVIS_WHISPER_MODEL:-/opt/jarvis/models/whisper-base.en}"
if [ -d /opt/jarvis/models/xtts ]; then
  export JARVIS_XTTS_MODEL_DIR="${JARVIS_XTTS_MODEL_DIR:-/opt/jarvis/models/xtts}"
fi

# Voice selection (unless the caller pinned JARVIS_TTS_ENGINE): if the project provides a
# reference clip AND the clone engine is installed, clone that voice; otherwise free Kokoro.
REF="/project/jarvis-voice.wav"
if [ -z "$JARVIS_TTS_ENGINE" ]; then
  if [ -f "$REF" ] && python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('TTS') else 1)" 2>/dev/null; then
    export JARVIS_TTS_ENGINE=xtts
    export JARVIS_XTTS_REFERENCE="${JARVIS_XTTS_REFERENCE:-$REF}"
    echo "[jarvis] voice clone enabled — cloning $REF"
  else
    export JARVIS_TTS_ENGINE=kokoro
    if [ -f "$REF" ]; then
      echo "[jarvis] found $REF but the clone engine isn't in this image — using Kokoro. Rebuild with --build-arg WITH_CLONE=1."
    fi
  fi
fi

if [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
  echo "[jarvis] WARNING: CLAUDE_CODE_OAUTH_TOKEN is not set — the brain needs it (Claude subscription)."
  echo "[jarvis]   On your host run:  claude setup-token   then pass it:  -e CLAUDE_CODE_OAUTH_TOKEN=<token>"
fi

# Run on the mounted project (its CLAUDE.md + settings.json + .jarvis/config.toml).
exec jarvis --remote --project /project "$@"
