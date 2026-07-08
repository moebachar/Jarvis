# J.A.R.V.I.S.

A voice-driven, autonomous Claude Code companion you can pull into any project.

Jarvis is a normal Claude Code session — same engine (the Claude Agent SDK), same tools,
same respect for your `settings.json` — wrapped with a persona and, across phases, voice,
a cinematic dashboard, and Telegram presence. He lives inside whatever project you launch
him from and answers strictly from what's actually there.

## Status: Phases 0–2 complete — brain + voice + dashboard

- ✅ Runs on your **Claude subscription** (no per-token bills); uses the logged-in `claude` CLI.
- ✅ **Grounded** in the current project (loads `CLAUDE.md` + `settings.json`, uses real tools).
- ✅ **Autonomous** per your `settings.json` (`permission_mode: auto`).
- ✅ **In character** — British, addresses you as "sir", concise (built for speaking aloud).
- ✅ Custom tools (`set_status`, `notify_user`) + activity hooks feed a live event bus.
- ✅ **Voice** — **push-to-talk**: hold a key (default **Right Ctrl**) to talk, press it again to
  interrupt. Button-only by default (no wake word); local speech-to-text (faster-whisper),
  sentence-streamed for low latency, with brief follow-up listening after each reply. As he works he
  narrates his own progress out loud, briefly ("Searching the web for that… found it"). The **voice
  is pluggable**: **Kokoro** runs a British male voice **fully local, free, and offline** (no key, no
  credits — recommended), or **ElevenLabs** for the premium cloud voice when you have credits.
- ✅ **Dashboard** — a four-panel monitoring HUD, everything visible at once: the audio-reactive
  Three.js **core**, a **live Claude Code** feed rendered as a raw CLI transcript (your prompts, his
  tool calls, and replies as they happen), a mini-render of the **latest canvas** (click to open the
  board), and **session** stats (model, tools used, tokens, uptime). A flat "mission computer" look —
  double-line console frames, amber+cyan, monospace, no gradients. Served locally over WebSocket;
  opens automatically with `jarvis` / `jarvis --voice`.
- ✅ **Cinematic audio** — a synthesized "Stark HUD" sound palette (a distinct cue per action:
  reading, searching, writing, running, thinking) plus a power-up boot greeting, and minimal
  spoken progress ("Searching the codebase, sir.") so he keeps you posted while he works. Sounds
  are generated procedurally (no asset files); spoken lines render once and cache. Audition the
  palette with `jarvis --demo-sfx`.
- ✅ **Presence + Telegram** (Phase 3) — Jarvis can work and then *reach you*. Anything he decides
  is worth saying (`notify_user`) is routed by presence: if you're at the machine he **speaks** it
  (asking "Sir, are you still here?" first when he's unsure, and listening); if the room's silent
  he sends it to **Telegram**, where you can reply by text *or* voice note to drive him from your
  phone. A 15-min heartbeat means he never goes silent forever. Only your own chat can talk to him.
  Set it up with `jarvis --telegram-id`.

- ✅ **Canvas board** (Phase 4) — a second page (`/canvas`) that's an **infinite whiteboard**. Jarvis
  renders visuals on demand or his own initiative — Mermaid diagrams ("show me the architecture"),
  charts, stat cards, screenshots, Markdown — and you arrange them: **pan, zoom, drag to move, resize,
  delete**, and **click a card to "focus" it** so Jarvis knows what *"this"* means ("make this bigger",
  "redo it as a chart"). The board is **saved per project** and reloads when you return. A modern
  graphite/dot-grid design, distinct from the HUD. Via the `show_on_dashboard` tool; libraries vendored (no CDN).

- ✅ **Live web fallback** — when a normal `WebFetch`/`WebSearch` can't load a page (JavaScript-heavy,
  blocked, or one he must see rendered), Jarvis escalates to a **real, visible Chrome** he drives with
  Playwright (`browse` tool) and reads the live page. Uses a fresh browser profile (not your logins).
  Optional: install with the `web` extra (`pip install -e .[web]`).

- ✅ **Packaging** (Phase 5) — one-command install with `install.ps1` / `install.sh`: Jarvis goes
  into its own isolated `pipx` environment and becomes a global `jarvis` command you can run from any
  project (`jarvis --init` scaffolds a project's `.jarvis/`). The dashboard can be watched from another
  machine over an SSH tunnel.

Roadmap: remaining Phase 5 — Windows boot **autostart** + restart/resume polish. See `.claude/plans/` for the full plan.

## Requirements

- Python ≥ 3.10 (tested on 3.13)
- The `claude` CLI installed and **logged in** (`claude` once, complete the login) — the brain
  runs on your Claude subscription through it.

## Install — pull Jarvis onto any machine

One command. The installer drops Jarvis into its **own isolated environment** (via `pipx`) and
exposes a global `jarvis` command — no hand-built venv to manage. Clone/pull the repo, then from
its root:

```powershell
# Windows
.\install.ps1                 # detects this machine's GPU automatically; installs the right build
.\install.ps1 -Gpu            # force GPU support on   (-NoGpu forces it off)
.\install.ps1 -Clone          # also install the XTTS-v2 voice clone (coqui-tts + a CUDA torch build)
```

```bash
# Linux / macOS
./install.sh                  # same — auto-detects the GPU
./install.sh --gpu            # force GPU on   (--no-gpu forces off)
./install.sh --clone          # voice clone
```

**One profile per machine.** The installer records this box in `~/.jarvis/machine.toml` (auto-detecting
the GPU via `nvidia-smi`), and **both** the installer and the runtime read it — so you set GPU *once*
and never pass flags or hand-edit device fields again. On the GPU desktop it installs `onnxruntime-gpu`
and the runtime uses CUDA for STT + TTS; on the laptop it stays CPU. Refresh it anytime with:

```powershell
jarvis --machine-init            # re-detect (or --gpu / --no-gpu to force, --clone to mark the clone)
```

The installer also bootstraps `pipx` if missing, checks the `claude` CLI is present, and prints the run
steps. Re-run it any time to upgrade; remove with `pipx uninstall jarvis`.

> Prefer a plain editable dev install instead? `python -m venv .venv && .\.venv\Scripts\python.exe
> -m pip install -e .[all]` still works.

## Run — from any project

Once installed, `cd` into **any** project and go. Jarvis loads that project's `CLAUDE.md` +
`settings.json` and answers from its real files.

```powershell
cd C:\some\project
jarvis --init               # optional: scaffold .jarvis/ (config.toml + .env.example)
jarvis                      # text REPL, lives in the current folder
jarvis --project C:\path    # or point it at a project without cd-ing
jarvis --resume             # resume this project's previous session
jarvis --voice              # voice (push-to-talk)   ·   jarvis --remote (tunneled voice)
```

Type to talk; `exit` to dismiss. Dimmed lines show his live tool use.

## Run (Phase 1 — voice)

By default the voice runs **fully local and free** with **Kokoro** — no account, no key, no credits.
Install the extra and it just works (the ~340 MB model auto-downloads once to `~/.jarvis/cache/kokoro/`):

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[kokoro]
```

`[voice] tts_engine = "kokoro"` (the default in this project's `.jarvis/config.toml`) selects it;
pick the British male voice with `kokoro_voice` (`bm_george`, `bm_lewis`, `bm_daniel`, `bm_fable`).
On a machine with an NVIDIA GPU, add `onnxruntime-gpu` and set `[voice] kokoro_device = "cuda"` for
near-instant synthesis. Preview it with `jarvis --check-voice`.

Prefer the **premium ElevenLabs cloud voice** instead? Set `[voice] tts_engine = "elevenlabs"` and add
keys in `<project>\.jarvis\.env` (see `.env.example`):

```
ELEVENLABS_API_KEY=...
JARVIS_ELEVENLABS_VOICE_ID=...      # a British male voice id (e.g. George / Daniel)
```

First, check your mic/speakers (no keys needed):

```powershell
jarvis --check-audio
```

Then:

```powershell
jarvis --voice
```

**Hold Right Ctrl** and talk (push-to-talk), release when done. He keeps listening for a few seconds
after each reply so you can continue naturally; **press Right Ctrl again while he's talking to cut him
off**. There's no wake word by default — the key is the only way in. First run downloads the `base.en`
speech model (~150 MB).

> Push-to-talk uses **Right Ctrl** by default (non-typing, no Fn needed). Change it with `[voice]
> ptt_key` (e.g. `"alt_r"`, `"pause"`, `"f9"`) in `.jarvis/config.toml`. To also enable the spoken
> wake word "jarvis" (and voice barge-in), set `[voice] wake_enabled = true`.

> Mic tip: ensure your microphone is unmuted, set as the default input, and that Windows
> "Let desktop apps access your microphone" is **on** — otherwise the wake word won't hear you.

## Remote voice (desktop brain, laptop dashboard)

Run Jarvis on a powerful machine (e.g. a GPU desktop) and talk to it from another (a laptop) with
**nothing but a browser tab** on the laptop. The desktop does the brain, speech-to-text, and
text-to-speech (ideally on its GPU); the browser tab is the microphone and speaker.

```powershell
# on the DESKTOP, once: the STT + dashboard + local-TTS extras (and onnxruntime-gpu for the GPU)
.\.venv\Scripts\python.exe -m pip install -e .[voice,dashboard,kokoro]
.\.venv\Scripts\python.exe -m pip install onnxruntime-gpu
jarvis --remote        # on the DESKTOP (inside the project)
```

The dashboard *is* the audio transport, so link the two machines with an SSH port-forward and open
the tab at **localhost** — that's required, because a browser only grants microphone access in a
"secure context" (https or `http://localhost`); a bare LAN IP is blocked.

```bash
# on the LAPTOP — forward the desktop's dashboard port to laptop localhost (VS Code Remote-SSH
# usually does this for you):
ssh -L 8765:localhost:8765 <desktop-host>
# then open  http://localhost:8765/  in Chrome/Edge and hold "HOLD TO TALK" (or the space bar).
```

Hold the on-screen **HOLD TO TALK** button (or the space bar) to speak; press again to interrupt.
The browser streams your mic to the desktop, which transcribes → thinks → speaks, streaming the reply
back to play in the tab. Put STT + TTS on the desktop's GPU with a config profile — see
[`config.remote-desktop.example.toml`](config.remote-desktop.example.toml) (copy it to the desktop's
`.jarvis/config.toml`): `whisper_device = "cuda"`, `kokoro_device = "cuda"` (`pip install onnxruntime-gpu`),
and `whisper_compute_type = "int8_float16"` for older (Pascal, e.g. Quadro P4000) cards. The dashboard
stays bound to loopback (`127.0.0.1`) — it's reached only through your tunnel, never exposed on the LAN.

## Dashboard

The HUD opens automatically at `http://127.0.0.1:8765/` when you run `jarvis` or `jarvis --voice`.
Preview/debug it without audio:

```powershell
jarvis --demo-dashboard      # synthetic events, opens the HUD
jarvis --demo-canvas         # preview the Canvas page (diagram, chart, stats, notes)
jarvis --voice --no-dashboard  # run voice without the dashboard
jarvis --demo-sfx            # audition the action-sound palette (no keys)
jarvis --voice --no-sfx      # run voice without the cinematic audio
```

It's a four-panel dashboard, all visible at once: the audio-reactive **bulb** (state + detail), a
live **Claude Code** feed (your prompts, his tool actions with summaries, and his replies — your
live debug console), a mini-render of the **latest canvas** (click it to open the board and edit),
and **session** stats (model, tools used, tokens in/out, session id, uptime). The **Board** page
(top-bar link, or `/canvas`) is an infinite whiteboard where he renders diagrams, charts, stat
cards, screenshots, and notes when a picture beats a paragraph — ask him to "show" or "diagram"
something. **Drag** to pan, **scroll** to zoom; cards can be moved, resized, and removed. **Click a
card to focus it** and Jarvis will treat "this"/"it" in your next request as that card. Your layout
is saved per project and restored next time.

### Watching the dashboard from another machine

Run Jarvis (and its project) on one box, watch the dashboard on another. The HUD stays bound to
**loopback** — it is never exposed on the network (the feed shows your code and prompts, and in
`--remote` the same socket carries your mic) — so you reach it over an **SSH port-forward**, the
same tunnel VS Code Remote-SSH already opens:

```bash
# on the viewing machine (e.g. your laptop):
ssh -L 8765:localhost:8765 <jarvis-host>
# then open  http://localhost:8765/  locally — it's the dashboard running on the other box.
```

Jarvis prints this exact hint on startup whenever the dashboard is on. (Advanced/trusted-LAN only:
set `[dashboard] host = "0.0.0.0"` to bind the network directly — Jarvis warns loudly because
there's no auth. SSH-forwarding is the recommended way.)

> Jarvis unsets `ANTHROPIC_API_KEY` at startup so the brain always uses your subscription.
> If you keep it set for other tools, that's fine — it's only removed for this process.

## Presence (Telegram)

So Jarvis can reach you when you've stepped away — and you can reply from your phone.

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. Copy the token.
2. Put it in `<project>\.jarvis\.env`:
   ```
   TELEGRAM_BOT_TOKEN=7283...:AAH...
   ```
3. Find your chat id (so he can message you first):
   ```powershell
   jarvis --telegram-id      # starts the bot; message it once; it prints your chat id
   ```
   Add the printed id to `.env` as `JARVIS_TELEGRAM_CHAT_ID=...`.

Now `jarvis` / `jarvis --voice` start the bridge automatically. When Jarvis finishes something
worth telling you, he delivers it by **presence**: if you're at the machine he speaks it (in
voice mode he'll first ask "Sir, are you still here?" and listen if he's unsure you're there);
if the room is silent it goes to **Telegram**. Reply there by text or voice note and it goes
straight to the brain (shared session, so voice and Telegram are one continuous conversation).
A **voice note** is transcribed locally (faster-whisper) and answered like a message, echoing
back what he heard. Only your configured chat is honoured — the bot ignores everyone else. Run
with `--no-telegram` to skip the bridge for a session.

Presence tuning lives under `[voice]` in `config.toml`: `presence_fresh_seconds` (how recently
you must have spoken for him to skip the "still here?" question), `presence_max_seconds` (after
this long with no local activity he won't try voice at all — straight to Telegram), and
`presence_prompt` (what he asks).

## Configuration

Layered, lowest priority first: built-in defaults → `~/.jarvis/config.toml` (global) →
`<project>/.jarvis/config.toml` (per-project) → environment variables (secrets).
Secrets may also go in `~/.jarvis/.env` or `<project>/.jarvis/.env`. See `.env.example`.

Phase 0 needs no configuration. Later phases add `voice`, `telegram`, and `dashboard` sections
and their keys (ElevenLabs, Picovoice, Telegram).

## Project layout

```
jarvis/
  config.py         layered config (defaults < global < project < env)
  eventbus.py       async pub/sub
  state.py          state machine + status snapshot
  context.py        shared runtime context (config, bus, state, notes)
  orchestrator.py   wires everything; exposes ask()
  cli.py            the `jarvis` text REPL
  brain/
    persona.py      the Jarvis system-prompt append
    tools.py        in-process MCP tools (set_status, notify_user)
    hooks.py        tool-use hooks -> live status
    agent.py        ClaudeSDKClient wrapper (grounding, autonomy, session resume)
```
