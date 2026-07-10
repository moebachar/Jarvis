# Jarvis — one container: brain + speech-to-text + text-to-speech. The browser tab (dashboard)
# is the microphone and speaker, streamed over the exposed port, so the container needs NO audio
# devices. Run it, forward port 8765, open http://localhost:8765/.
#
# Build:   docker compose build           (or: docker build -t jarvis .)
# Run:     docker compose up              (see docker-compose.yml)
#
# Models are pre-fetched at build time with curl (robust on flaky networks — no Python download
# hangs), so first launch never waits on a download.

FROM python:3.11-slim

# ---- build knobs -------------------------------------------------------------------------
# WITH_CLONE=1 also installs the XTTS-v2 voice clone (coqui-tts + a torch build). It's what lets
# Jarvis speak in a cloned voice; it's heavy (~4 GB) and wants a GPU. Set 0 for a lean CPU image.
ARG WITH_CLONE=1
# Torch wheel index: a CUDA build (cu121) for GPU boxes, or .../whl/cpu for CPU-only.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu121
# GPU=1 additionally installs onnxruntime-gpu (Kokoro on CUDA) + faster-whisper CUDA libs.
ARG GPU=1

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HUB_DOWNLOAD_TIMEOUT=30 \
    COQUI_TOS_AGREED=1 \
    # The Agent SDK caches subscription credentials + session state here; must be writable.
    CLAUDE_CONFIG_DIR=/root/.claude

# ---- system deps -------------------------------------------------------------------------
# git (repos/MCP), curl (model fetch), ffmpeg + libsndfile (audio decode for coqui/soundfile),
# libportaudio2 (the `sounddevice` import loads it even though the browser is the real mic),
# nodejs + npm (the Claude Code CLI the Agent SDK drives for subscription auth).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates ffmpeg libsndfile1 libportaudio2 nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && mkdir -p /root/.claude && chmod 700 /root/.claude \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/jarvis

# ---- python deps (layer-cached: install deps before copying the source) ------------------
COPY pyproject.toml README.md ./
COPY jarvis ./jarvis
RUN pip install --no-cache-dir ".[all]" \
    && if [ "$GPU" = "1" ]; then \
         pip install --no-cache-dir onnxruntime-gpu nvidia-cublas-cu12 nvidia-cudnn-cu12 || \
         echo "WARN: GPU runtimes not installed; Jarvis will run STT/TTS on CPU"; \
       fi \
    && if [ "$WITH_CLONE" = "1" ]; then \
         pip install --no-cache-dir torch torchaudio --index-url "$TORCH_INDEX" \
         && pip install --no-cache-dir coqui-tts; \
       fi

# ---- pre-fetch models with curl (no Python downloads → no hangs at first run) -------------
# Kokoro (local British TTS) → the cache dir load_kokoro reads by default.
RUN mkdir -p /root/.jarvis/cache/kokoro \
    && curl -fSL -o /root/.jarvis/cache/kokoro/kokoro-v1.0.onnx \
       https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx \
    && curl -fSL -o /root/.jarvis/cache/kokoro/voices-v1.0.bin \
       https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# faster-whisper base.en → a plain local model dir (JARVIS_WHISPER_MODEL points here at runtime).
RUN mkdir -p /opt/jarvis/models/whisper-base.en \
    && for f in config.json model.bin tokenizer.json vocabulary.txt; do \
         curl -fSL -o "/opt/jarvis/models/whisper-base.en/$f" \
           "https://huggingface.co/Systran/faster-whisper-base.en/resolve/main/$f"; \
       done

# XTTS-v2 checkpoint (only when the clone is built) → a local dir (JARVIS_XTTS_MODEL_DIR).
RUN if [ "$WITH_CLONE" = "1" ]; then \
      mkdir -p /opt/jarvis/models/xtts && cd /opt/jarvis/models/xtts \
      && for f in config.json model.pth vocab.json speakers_xtts.pth dvae.pth mel_stats.pth hash.md5; do \
           curl -fSL -o "$f" "https://huggingface.co/coqui/XTTS-v2/resolve/main/$f" || true; \
         done; \
    fi

# Bake the voice-clone reference clip into the image (the `[v]` glob makes it optional — the
# build still works if the repo has no clip). The entrypoint prefers a clip in the mounted
# project, then falls back to this one, so the cloned voice works for ANY project.
COPY jarvis-voice.wa[v] /opt/jarvis/

COPY docker-entrypoint.sh /usr/local/bin/jarvis-entrypoint
RUN chmod +x /usr/local/bin/jarvis-entrypoint

# The dashboard IS the audio transport; expose its port.
EXPOSE 8765
# Jarvis runs on the project mounted here (its CLAUDE.md + settings.json + .jarvis/config.toml).
WORKDIR /project
VOLUME ["/project", "/root/.claude"]

ENTRYPOINT ["jarvis-entrypoint"]
