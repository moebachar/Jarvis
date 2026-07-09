#!/usr/bin/env bash
# Jarvis installer (Linux/macOS) — pulls Jarvis into an ISOLATED, auto-managed environment via
# pipx and exposes a global `jarvis` command. No hand-built venv; run once, then `cd` into any
# project and type `jarvis`.
#
# It reads this machine's profile (~/.jarvis/machine.toml) — or auto-detects your GPU with
# nvidia-smi the first time — so you DON'T pass flags per machine. Just:
#
#     ./install.sh                  # detects GPU/clone from the machine profile (or auto-detects)
#
# Override for this run (also saved back to the profile):
#     ./install.sh --gpu            # force GPU support on   ·   --no-gpu forces it off
#     ./install.sh --clone          # also install the XTTS-v2 voice clone (coqui-tts + CUDA torch)
#     ./install.sh --extras voice,dashboard          # custom pip extras
#     ./install.sh --python /usr/bin/python3.11       # specific Python (3.11 suits --clone)
#
# Re-run any time to upgrade. Uninstall with:  pipx uninstall jarvis
set -euo pipefail

FORCE_GPU=""; CLONE=0; PYTHON=""; EXTRAS=""; CUDA=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu)     FORCE_GPU=1; shift ;;
    --no-gpu)  FORCE_GPU=0; shift ;;
    --clone)   CLONE=1; shift ;;
    --extras)  EXTRAS="$2"; shift 2 ;;
    --python)  PYTHON="$2"; shift 2 ;;
    --cuda)    CUDA="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  * %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$1"; }

# 1. Python -------------------------------------------------------------------------------
if [ -n "$PYTHON" ]; then PY="$PYTHON"; command -v "$PY" >/dev/null || { echo "python '$PY' not found" >&2; exit 1; }
elif command -v python3 >/dev/null 2>&1; then PY="python3"
else echo "No python3 found. Install Python 3.10+ and re-run." >&2; exit 1; fi
info "Using Python: $PY ($("$PY" --version 2>&1))"

# 2. Resolve THIS machine's profile (forced flags > machine.toml > auto-detect) -----------
[ -n "$FORCE_GPU" ] && export JARVIS_FORCE_GPU="$FORCE_GPU"
[ "$CLONE" -eq 1 ] && export JARVIS_FORCE_CLONE=1
[ -n "$EXTRAS" ] && export JARVIS_FORCE_EXTRAS="$EXTRAS"
[ -n "$PYTHON" ] && export JARVIS_FORCE_PYTHON="$PYTHON"
[ -n "$CUDA" ] && export JARVIS_FORCE_CUDA="$CUDA"

info "Resolving machine profile…"
eval "$(cd "$REPO" && "$PY" -m jarvis.machine)"   # sets GPU / CLONE / CUDA / EXTRAS / PYTHON
USE_GPU="$GPU"; USE_CLONE="$CLONE"; USE_CUDA="$CUDA"; USE_EXTRAS="$EXTRAS"; USE_PYTHON="$PYTHON"
ok "Profile: GPU=$USE_GPU  clone=$USE_CLONE  extras=$USE_EXTRAS  cuda=$USE_CUDA"

# 3. pipx ---------------------------------------------------------------------------------
if "$PY" -m pipx --version >/dev/null 2>&1; then ok "pipx already present."
else
  info "Installing pipx (one-time)…"
  # `pip install --user` fails inside an active virtualenv; only pass --user outside one.
  if [ -n "${VIRTUAL_ENV:-}" ]; then
    warn "A virtualenv is active; installing pipx into it (pipx still puts jarvis in ~/.local/bin)."
    "$PY" -m pip install --upgrade pipx
  else
    "$PY" -m pip install --user --upgrade pipx
  fi
  "$PY" -m pipx ensurepath >/dev/null || true
  ok "pipx installed (open a new shell for it on PATH; this run calls it via python -m)."
fi

# 4. Install Jarvis -----------------------------------------------------------------------
info "Installing jarvis[$USE_EXTRAS] from $REPO …"
INSTALL_ARGS=(-m pipx install --force "$REPO[$USE_EXTRAS]")
[ -n "$USE_PYTHON" ] && INSTALL_ARGS+=(--python "$USE_PYTHON")
"$PY" "${INSTALL_ARGS[@]}"
ok "jarvis installed. (Machine profile saved to ~/.jarvis/machine.toml.)"

# 5. GPU runtime --------------------------------------------------------------------------
if [ "$USE_GPU" = "1" ]; then
  info "Injecting GPU runtimes (onnxruntime-gpu for Kokoro; cuBLAS + cuDNN for faster-whisper)…"
  "$PY" -m pipx inject jarvis onnxruntime-gpu nvidia-cublas-cu12 nvidia-cudnn-cu12
  ok "GPU runtimes injected. (If STT still can't find CUDA, Jarvis auto-falls back to CPU.)"
fi

# 6. Voice clone --------------------------------------------------------------------------
if [ "$USE_CLONE" = "1" ]; then
  info "Injecting the voice-clone engine (coqui-tts) + a CUDA torch build ($USE_CUDA)…"
  [ -z "$USE_PYTHON" ] && warn "coqui-tts prefers Python 3.11 — if this fails, re-run with --python <py3.11>."
  "$PY" -m pipx inject jarvis coqui-tts
  "$PY" -m pipx runpip jarvis install torch torchaudio --index-url "https://download.pytorch.org/whl/$USE_CUDA"
fi

# 7. Claude CLI ---------------------------------------------------------------------------
if command -v claude >/dev/null 2>&1; then ok "Claude CLI found — the brain uses your subscription via it."
else
  warn "Claude CLI not found. The brain needs it (subscription auth). Install + log in:"
  warn "    npm install -g @anthropic-ai/claude-code   ;   claude   (sign in once)"
fi
[ -n "${ANTHROPIC_API_KEY:-}" ] && warn "ANTHROPIC_API_KEY is set; Jarvis unsets it per-run, but consider removing it."

# 8. Done ---------------------------------------------------------------------------------
echo
ok "Installed. Next:"
echo "    cd <any project>"
echo "    jarvis --init      # optional: scaffold .jarvis/ (config + .env.example)"
echo "    jarvis             # text REPL   |   jarvis --voice   |   jarvis --remote"
echo
echo "  Dashboard from another machine (Jarvis stays here, browser elsewhere):"
echo "    ssh -L 8765:localhost:8765 <this-host>   then open http://localhost:8765/"
