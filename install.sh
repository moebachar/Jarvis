#!/usr/bin/env bash
# Jarvis installer (Linux/macOS) — pulls Jarvis into an ISOLATED, auto-managed environment via
# pipx and exposes a global `jarvis` command. No hand-built venv; run once, then `cd` into any
# project and type `jarvis`.
#
# Usage (from the repo root, after git clone / pull):
#     ./install.sh                     # full install: voice + free Kokoro TTS + dashboard + Telegram + web
#     ./install.sh --gpu               # + onnxruntime-gpu (Kokoro/whisper on an NVIDIA GPU)
#     ./install.sh --clone             # + XTTS-v2 voice clone (coqui-tts + a CUDA torch build)
#     ./install.sh --extras voice,dashboard          # pick your own extras instead of the bundle
#     ./install.sh --python /usr/bin/python3.11       # use a specific Python (3.11 recommended for --clone)
#
# Re-run any time to upgrade (force reinstall). Uninstall with:  pipx uninstall jarvis
set -euo pipefail

EXTRAS="all"
GPU=0
CLONE=0
PYTHON=""
CUDA="cu121"
while [ $# -gt 0 ]; do
  case "$1" in
    --extras) EXTRAS="$2"; shift 2 ;;
    --gpu)    GPU=1; shift ;;
    --clone)  CLONE=1; shift ;;
    --python) PYTHON="$2"; shift 2 ;;
    --cuda)   CUDA="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  * %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$1"; }

# 1. Python -------------------------------------------------------------------------------
if [ -n "$PYTHON" ]; then
  PY="$PYTHON"
  [ -x "$(command -v "$PY")" ] || { echo "python '$PY' not found" >&2; exit 1; }
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "No python3 found. Install Python 3.10+ and re-run." >&2; exit 1
fi
info "Using Python: $PY ($("$PY" --version 2>&1))"

# 2. pipx ---------------------------------------------------------------------------------
if "$PY" -m pipx --version >/dev/null 2>&1; then
  ok "pipx already present."
else
  info "Installing pipx (one-time)…"
  "$PY" -m pip install --user --upgrade pipx
  "$PY" -m pipx ensurepath >/dev/null || true
  ok "pipx installed (open a new shell for pipx on PATH; this run calls it via python -m)."
fi

# 3. Install Jarvis from THIS repo --------------------------------------------------------
info "Installing jarvis[$EXTRAS] from $REPO …"
INSTALL_ARGS=(-m pipx install --force "$REPO[$EXTRAS]")
[ -n "$PYTHON" ] && INSTALL_ARGS+=(--python "$PYTHON")
"$PY" "${INSTALL_ARGS[@]}"
ok "jarvis installed."

# 4. Optional GPU runtime -----------------------------------------------------------------
if [ "$GPU" -eq 1 ]; then
  info "Injecting onnxruntime-gpu (Kokoro on CUDA)…"
  "$PY" -m pipx inject jarvis onnxruntime-gpu
  warn "faster-whisper on GPU also needs CUDA + cuDNN on PATH (nvidia-cublas-cu12 / nvidia-cudnn-cu12)."
  ok "Set whisper_device/kokoro_device = \"cuda\" in .jarvis/config.toml."
fi

# 5. Optional voice clone -----------------------------------------------------------------
if [ "$CLONE" -eq 1 ]; then
  info "Injecting the voice-clone engine (coqui-tts) + a CUDA torch build ($CUDA)…"
  [ -z "$PYTHON" ] && warn "coqui-tts prefers Python 3.11 — if this fails, re-run with --python <py3.11>."
  "$PY" -m pipx inject jarvis coqui-tts
  "$PY" -m pipx runpip jarvis install torch torchaudio --index-url "https://download.pytorch.org/whl/$CUDA"
  ok "Set tts_engine=\"xtts\" + xtts_reference in .jarvis/config.toml (see config.voice-clone.example.toml)."
fi

# 6. Claude CLI ---------------------------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  ok "Claude CLI found — the brain uses your subscription via it."
else
  warn "Claude CLI not found. The brain needs it (subscription auth). Install + log in:"
  warn "    npm install -g @anthropic-ai/claude-code"
  warn "    claude    (sign in once)   — or:   claude setup-token"
fi
[ -n "${ANTHROPIC_API_KEY:-}" ] && warn "ANTHROPIC_API_KEY is set; Jarvis unsets it per-run, but consider removing it."

# 7. Done ---------------------------------------------------------------------------------
echo
ok "Installed. Next:"
echo "    cd <any project>"
echo "    jarvis --init      # optional: scaffold .jarvis/ (config + .env.example)"
echo "    jarvis             # text REPL   |   jarvis --voice   |   jarvis --remote"
echo
echo "  Dashboard from another machine (Jarvis stays here, browser elsewhere):"
echo "    ssh -L 8765:localhost:8765 <this-host>   then open http://localhost:8765/"
