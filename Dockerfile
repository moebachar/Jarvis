# syntax=docker/dockerfile:1
# Jarvis — one container: brain + speech-to-text + text-to-speech. The browser tab (dashboard)
# is the microphone and speaker, streamed over the exposed port, so the container needs NO audio
# devices. Run it, forward port 8765, open http://localhost:8765/.
#
# Build:   docker compose build           (or: docker build -t jarvis .)
# Run:     docker compose up              (see docker-compose.yml)
#
# EVERY large download here goes through resumable curl, never through Python. On a link that
# throttles long transfers (as this build's network does — fast for ~128 MB, then ~80 kB/s), pip
# is fatal: it has no resume, so each retry restarts a 600 MB+ wheel from zero and it never lands.
# curl resumes with an HTTP Range request, and the big fetches live in BuildKit cache mounts, so a
# build that dies mid-download continues from where it stopped instead of starting over.

FROM python:3.11-slim

# ---- build knobs -------------------------------------------------------------------------
# WITH_CLONE=1 also installs the XTTS-v2 voice clone (coqui-tts + a torch build). It's what lets
# Jarvis speak in a cloned voice; it's heavy (~4 GB) and wants a GPU. Set 0 for a lean CPU image.
ARG WITH_CLONE=1
# Torch wheel index: a CUDA build (cu121) for GPU boxes, or .../whl/cpu for CPU-only.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu121
# GPU=1 additionally installs onnxruntime-gpu (Kokoro on CUDA) + faster-whisper CUDA libs.
ARG GPU=1
# curl profile for every big download: resume (-C -), retry hard, and — crucially — abandon a
# connection the shaper has throttled below 100 kB/s for 20 s so the retry starts a FRESH (fast)
# connection instead of crawling. This is what makes the multi-hundred-MB fetches finish here.
# (Quoted single ENV so the spaces stay one value; RUNs use it unquoted to word-split into flags.)
ENV CURL_OPTS="-fL --retry 100 --retry-delay 5 --retry-all-errors --speed-limit 100000 --speed-time 20 -C -"

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HUB_DOWNLOAD_TIMEOUT=30 \
    # Let the lighter pip steps ride out the shaper: a long read timeout (don't die when a mid-size
    # wheel crawls) and generous retries. The giant wheels don't rely on this — they use curl.
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
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
# pip's HTTP cache is a mount, not --no-cache-dir: it stays out of the image but a network hiccup
# no longer discards everything already fetched. These deps are all well under the throttle
# threshold, so plain pip handles them.
COPY pyproject.toml README.md ./
COPY jarvis ./jarvis
RUN --mount=type=cache,id=jarvis-pip,target=/root/.cache/pip \
    pip install ".[all]"

# ---- the heavy CUDA/torch/clone stack: resolve → curl-with-resume → install offline -------
# This is the set that kept dying on the throttled link (torch, cuDNN 665 MB, cuBLAS, coqui-tts).
# docker-fetch-wheels.sh routes every wheel through resumable curl; /wheels is a cache mount so a
# failed build keeps its downloaded bytes. See that script for the full rationale.
COPY docker-fetch-wheels.sh /usr/local/bin/jarvis-fetch-wheels
RUN chmod +x /usr/local/bin/jarvis-fetch-wheels
# If the link drops mid-fetch this step fails on purpose (no soft fallback) — the downloaded bytes
# stay in the /wheels cache mount, so just re-run `docker compose up --build` and it resumes.
RUN --mount=type=cache,id=jarvis-pip,target=/root/.cache/pip \
    --mount=type=cache,id=jarvis-wheels,target=/wheels \
    set -e; \
    HEAVY=""; \
    [ "$WITH_CLONE" = "1" ] && HEAVY="$HEAVY torch torchaudio coqui-tts"; \
    [ "$GPU" = "1" ] && HEAVY="$HEAVY onnxruntime-gpu nvidia-cublas-cu12 nvidia-cudnn-cu12"; \
    if [ -n "$HEAVY" ]; then jarvis-fetch-wheels "$TORCH_INDEX" $HEAVY; fi

# ---- pre-fetch models with curl (no Python downloads → no hangs at first run) -------------
# Models curl into a cache mount (resumable, survives a failed build) then copy into the image
# layer. JARVIS_*_MODEL* envs point the runtime here, so first launch never waits on a download.
# A complete cached file is skipped (re-running `curl -C -` on a finished file would 416 under -f);
# a partial resumes via its .part, which is only renamed to the final name once curl succeeds.
# Kokoro (local British TTS) → the cache dir load_kokoro reads by default.
RUN --mount=type=cache,id=jarvis-models,target=/dl \
    set -e; mkdir -p /root/.jarvis/cache/kokoro; \
    base=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0; \
    for f in kokoro-v1.0.onnx voices-v1.0.bin; do \
      [ -f "/dl/$f" ] || { curl $CURL_OPTS -o "/dl/$f.part" "$base/$f" && mv "/dl/$f.part" "/dl/$f"; }; \
      cp "/dl/$f" "/root/.jarvis/cache/kokoro/$f"; \
    done

# faster-whisper base.en → a plain local model dir (JARVIS_WHISPER_MODEL points here at runtime).
RUN --mount=type=cache,id=jarvis-models,target=/dl \
    set -e; mkdir -p /opt/jarvis/models/whisper-base.en; \
    base=https://huggingface.co/Systran/faster-whisper-base.en/resolve/main; \
    for f in config.json model.bin tokenizer.json vocabulary.txt; do \
      [ -f "/dl/whisper-$f" ] || { curl $CURL_OPTS -o "/dl/whisper-$f.part" "$base/$f" && mv "/dl/whisper-$f.part" "/dl/whisper-$f"; }; \
      cp "/dl/whisper-$f" "/opt/jarvis/models/whisper-base.en/$f"; \
    done

# XTTS-v2 checkpoint (only when the clone is built) → a local dir (JARVIS_XTTS_MODEL_DIR).
# model.pth is ~1.8 GB — the single biggest fetch in the build, and exactly why every curl here
# resumes. hash.md5 isn't always published, so only that one file is allowed to fail.
RUN --mount=type=cache,id=jarvis-models,target=/dl \
    set -e; \
    if [ "$WITH_CLONE" = "1" ]; then \
      mkdir -p /opt/jarvis/models/xtts; \
      base=https://huggingface.co/coqui/XTTS-v2/resolve/main; \
      for f in config.json model.pth vocab.json speakers_xtts.pth dvae.pth mel_stats.pth; do \
        [ -f "/dl/xtts-$f" ] || { curl $CURL_OPTS -o "/dl/xtts-$f.part" "$base/$f" && mv "/dl/xtts-$f.part" "/dl/xtts-$f"; }; \
        cp "/dl/xtts-$f" "/opt/jarvis/models/xtts/$f"; \
      done; \
      if [ ! -f /dl/xtts-hash.md5 ]; then curl $CURL_OPTS -o /dl/xtts-hash.md5.part "$base/hash.md5" && mv /dl/xtts-hash.md5.part /dl/xtts-hash.md5 || true; fi; \
      [ -f /dl/xtts-hash.md5 ] && cp /dl/xtts-hash.md5 /opt/jarvis/models/xtts/hash.md5 || true; \
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
