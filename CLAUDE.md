# CLAUDE.md — Jarvis project

Guidance for any Claude Code (or Jarvis) session working in this repo.

## What this is
Jarvis = a Claude Code agent (Claude Agent SDK, Python) wrapped with a persona and, across
phases, voice + a dashboard + Telegram presence. You pull it into any project and it lives
there, answering from the actual code and acting under that project's `settings.json`.

## Hard rules
- **Brain runs on the Claude subscription**, not the API. The SDK inherits the logged-in
  `claude` CLI auth. `ANTHROPIC_API_KEY` must NOT be set when running Jarvis (it would shadow
  the subscription and bill the API). `cli.py` unsets it at startup; keep it that way.
- **Stay grounded.** Jarvis must never fabricate project facts — see `brain/persona.py`.

## Architecture (one process, asyncio)
`Orchestrator` (`orchestrator.py`) owns an `EventBus` (`eventbus.py`), a `StateStore`
(`state.py`), a `RuntimeContext` (`context.py`), and the `JarvisBrain` (`brain/agent.py`).
The brain holds a long-lived `ClaudeSDKClient`. Custom in-process MCP tools
(`brain/tools.py`) are how the agent drives the outside world; lifecycle hooks
(`brain/hooks.py`) mirror tool use into the state store, which emits `status` events on the
bus. Subscribers (the REPL now; dashboard/Telegram later) react to those events.

### Key SDK wiring (in `brain/agent.py`, verified against claude-agent-sdk 0.2.x)
- `ClaudeAgentOptions(cwd=<project>, setting_sources=["user","project","local"], ...)`
  loads `CLAUDE.md` + `settings.json` — this is the grounding + autonomy mechanism.
- `system_prompt={"type":"preset","preset":"claude_code","append": <persona>}` keeps full
  Claude Code behaviour and layers the persona on top.
- `permission_mode="auto"` (configurable) → autonomous, prompt-free.
- `mcp_servers={"jarvis": create_sdk_mcp_server(...)}` + `allowed_tools=["mcp__jarvis__*", ...]`.
- `hooks={"PreToolUse":[HookMatcher(hooks=[cb])], "PostToolUse":[...]}`;
  callback is `async (input_data, tool_use_id, context) -> {}`.
- Conversation: `client.query(text)` then iterate `client.receive_response()` until
  `ResultMessage` (carries `session_id`, persisted for `--resume`).

## Phases
0 (done) brain + text REPL · 1 (done) voice · 2 (done) dashboard HUD · 3 (done) sleep/wake (15-min
heartbeat) + Telegram presence + voice "are you still here?" check · 4 (done) dashboard canvas
(diagrams/plots/stats/screenshots) · 5 packaging (done — pipx install) + autostart (pending). Full plan:
`~/.claude/plans/linear-tumbling-waterfall.md`.

### Packaging / install (`install.ps1` + `install.sh` + `jarvis/scaffold.py`, Phase 5)
"Pull Jarvis onto any machine with one command." The installers bootstrap **pipx** (if absent) and
`pipx install --force "<repo>[all]"` → Jarvis lands in its OWN isolated env and `jarvis` is a global
command (no hand-built venv — the user's explicit ask). `all` extra = `jarvis[voice,kokoro,dashboard,
presence,web]` (NOT clone — torch/GPU-specific). Installer switches: `-Gpu`/`--gpu` (`pipx inject jarvis
onnxruntime-gpu`), `-Clone`/`--clone` (`pipx inject jarvis coqui-tts` + `pipx runpip jarvis install torch
… --index-url …/whl/<cuda>` — so even the heavy clone env is scripted, not hand-built; warns to use
`-Python <py3.11>` since coqui-tts prefers 3.11), `-Extras`/`--extras`, `-Python`/`--python`. Both check
the `claude` CLI is present (subscription auth) + warn if `ANTHROPIC_API_KEY` is set. `jarvis --init`
(`scaffold.py init_project`) writes a starter `<project>/.jarvis/{config.toml,.env.example,.gitignore}`
(keyless Kokoro default; never overwrites existing). `jarvis --version` via importlib.metadata.
**Per-machine profile (`jarvis/machine.py` + `~/.jarvis/machine.toml`):** the user wanted GPU declared
ONCE per machine in a config file, not via install flags. So there's a `[machine]` profile (`MachineConfig`
in config.py: `gpu`/`voice_clone`/`cuda`/`python`/`extras`) in an installer-owned `~/.jarvis/machine.toml`
that BOTH sides read. Runtime: `_apply_machine_gpu()` (config.py) — when `machine.gpu` is true, it fills the
voice STT/TTS device fields (`whisper_device`/`whisper_compute_type`=int8_float16/`kokoro_device`/
`xtts_device`) with their CUDA values, but ONLY for fields the user didn't set by hand (tracked via
`explicit_voice` from the raw TOMLs); machine.toml is merged BELOW the user's config.toml. Installer:
`python -m jarvis.machine` prints the RESOLVED profile as `KEY=VALUE` lines (`resolve_profile`: forced
`JARVIS_FORCE_*` env > existing machine.toml > `detect_gpu()` via nvidia-smi), which install.ps1/.sh parse
to pick extras + GPU/clone injections, then persist via `jarvis --machine-init` (writes machine.toml;
`--gpu`/`--no-gpu` force, `--clone` marks it). Net: a bare `.\install.ps1` auto-detects the GPU, installs
the right build, records the profile, and the runtime uses CUDA — no per-machine flags.
**Dashboard-from-another-machine:** the HUD frontend already dials `ws://${location.host}/ws`, so an SSH
port-forward "just works" with ZERO code change; `_dashboard_forward_note()` prints the exact
`ssh -L <port>:localhost:<port> <host>` hint on startup whenever the dashboard is on (loopback stays the
secure default; a non-loopback `[dashboard] host` triggers a loud no-auth warning instead). Wheel verified
to include all `dashboard/static/**` assets + the `all` extra resolves. 51/51 tests (added
`tests/test_packaging.py`: init_project create/idempotent/preserve + forward-note loopback/LAN/disabled).

### Dashboard canvas — the infinite board (`jarvis/dashboard/`, Phase 4 — RETIRED 2026-07-05)
NOTE: the full-page board below was retired at the user's request in favour of the HUD's navigable
canvas viewer (see the Dashboard HUD section). The `/canvas` route is gone; `canvas.html`/`canvas.js`/
`board.css` remain on disk but are unused/unlinked (kept so it can be restored). The server-side board
model + persistence + `canvas_focus` still power the viewer. Historical description follows.
Second page at `/canvas` is an **infinite, manipulable whiteboard** (`canvas.html` + `canvas.js`,
classic script using vendored globals; styled by its own `board.css` — deliberately NOT the HUD's
cyan/gold hologram, but a graphite dot-grid "studio" look with per-kind accent cards [mermaid=violet,
chart=teal, stats=amber, image=pink, markdown=blue] and a coral focus highlight). A fixed `#viewport`
holds a transformed `#world`; cards live in world coords (left/top/w/h). **Pan** = drag empty space,
**zoom** = wheel toward cursor (also a bottom dock: −/level/+, fit-to-content, home, clear). Cards
**move** (drag header), **resize** (corner grip), **delete** (×), and **focus** (click → coral ring +
top-bar pill). Smart placement: new cards spiral-search from the current view centre to avoid overlap.
Brain tool `show_on_dashboard(kind, content, title)` (`brain/tools.py`) still emits a `canvas` bus
event; kinds: `mermaid`, `chart` (Chart.js JSON {type,labels,datasets}), `stats` (JSON array of
{label,value,hint}), `image` (file path → `_inline_image` base64 data-URLs it ≤8 MB; data/http/`/static`
pass through), `markdown` (small in-page MD subset).
**The board is now stateful and bidirectional.** `DashboardServer` is the source of truth: it owns a
`_board` (`{items: {id: {...,x,y,w,h,placed}}, order, viewport}`), turns each `canvas` event into a board
item (server-assigned id `c<N>`, fallback diagonal position; the browser does smart placement and reports
it back), and **persists** to `<project>/.jarvis/state/canvas-board.json` (debounced `_saver_loop`,
final flush on stop) so the board survives restarts. The `/ws` is no longer broadcast-only: the browser
sends `canvas_update`/`canvas_remove`/`canvas_clear`/`canvas_viewport`/`canvas_focus` back, handled by
`_handle_client_msg` (mutate board → schedule save → fan the change to OTHER clients). On connect the
server sends a `snapshot` (HUD) + a full `board` payload (Canvas). **Focus → brain awareness:** a
`canvas_focus` sets `ctx.focused_canvas`; `ctx.canvas_focus_hint()` renders a short note that
`JarvisBrain.ask()` prepends to the user's NEXT turn (the one chokepoint, so REPL/voice/Telegram all get
it) — so "make this bigger / explain this / redo as a chart" resolves to the selected card. Persona nudges
him accordingly. Libraries VENDORED (no CDN): `static/vendor/mermaid.min.js` + `chart.umd.min.js`.
CLI `jarvis --demo-canvas` previews one of each kind. package-data `static/*` covers `board.css`.
Verified headless via Playwright: all kinds render with ZERO console/page errors; smart placement,
focus round-trip to the server, zoom, drag-move (+180,+120), resize (+90,+70), disk persistence and
reload-on-fresh-server all confirmed (screenshot confirmed the aesthetic). 27/27 unit tests pass
(8 new in `tests/test_dashboard_board.py` cover the board model + inbound edits + focus hint + persistence).

### Presence layer (`jarvis/presence/`, Phase 3 — done)
Reach the user when away + bi-directional Telegram. `TelegramBridge` (`telegram_bot.py`,
python-telegram-bot v22, runs in our own asyncio loop via `Application.initialize/start` +
`updater.start_polling`, NOT `run_polling`) handles inbound text (allow-listed `chat_id` only →
`on_message` → reply), inbound **voice notes / audio** (`filters.VOICE|AUDIO` → `_handle_audio`
downloads the OGG/Opus bytes → `on_audio` → `Orchestrator._on_telegram_audio` transcribes via the
shared faster-whisper model [`_ensure_transcriber`, lazy, reused by voice mode; PyAV decodes Opus,
no external ffmpeg] → answers like a text turn, echoing the transcript), and outbound (`send`).
`_authorized` is the shared owner-chat gate (learns the id on first contact, rejects strangers).
`PresenceManager` (`manager.py`) is a plain-asyncio heartbeat (every `heartbeat_minutes`, min 60s)
plus a bus `_consume` task; both deliver queued `notify_user` notes PROMPTLY via `_deliver_pending`,
which routes per delivery: if `_maybe_present()` (local activity newer than
`voice.presence_max_seconds`) AND a live voice loop is attached → hand notes to the **VoiceLink**;
if that reports silence (or no loop) → Telegram; if neither works → put the notes back (never lost).
**VoiceLink** (`voice/link.py`) is a one-slot rendezvous: the voice loop owns the mic/speaker and
services a delivery only while idle between turns (`_wait_trigger` races wake/ptt/`link.wait`;
winner "deliver" → `_handle_delivery` → `_presence_deliver`). `_presence_deliver`: spoke within
`voice.presence_fresh_seconds` → just speak the notes; else say `voice.presence_prompt` ("Sir, are
you still here?"), listen `presence_listen_seconds` → answered: speak notes (True); silent: False →
manager falls back to Telegram. The brain is ONE shared `ClaudeSDKClient` session, so
`Orchestrator.ask_lock` serializes every turn (voice `_respond`, REPL, Telegram `ask_text`). Local
presence (`ctx.mark_user_active`/`seconds_since_active`) is set by the voice loop (on heard) + REPL
(on input), NOT by Telegram (remote ≠ "at the machine"). Tool `sleep_until(reason, minutes)` →
SLEEPING state + `sleep` event. Started in `Orchestrator._maybe_start_presence()` when a bot token
is configured OR voice mode is on (so notes can be spoken even with no Telegram). CLI:
`jarvis --telegram-id` (discover chat id), `--no-telegram` (skip the bridge). Keys:
`TELEGRAM_BOT_TOKEN` + `JARVIS_TELEGRAM_CHAT_ID` in `.env`.
Verified headless (routing present/away/silent, link rendezvous, loop check logic, immediate
delivery, failure-retention; 19/19 unit tests). Telegram bridge live-confirmed by the user (text +
replies + voice notes); the voice "are you still here?" check is NOT yet live-tested.

### Dashboard HUD (`jarvis/dashboard/`, Phase 2 — redesigned)
`DashboardServer` (`server.py`) = FastAPI + uvicorn run inside the orchestrator loop; one EventBus
subscription fans every event out to all `/ws` WebSocket clients (each client gets a status
snapshot + the canvas board on connect). Frontend in `static/` (vendored `three.module.js`,
`mermaid.min.js`, `chart.umd.min.js`; no CDN). The HUD (`index.html` + `app.js` + `style.css`) is
now a **four-panel monitoring dashboard, all visible at once** (CSS grid; a `bulb feed / bulb bottom`
area layout that collapses to a single stacked column ≤900px):
- **3D bulb** (tall left) — the audio-reactive particle sphere + reactor rings + core, now rendered
  into its own panel (a `ResizeObserver` on `#bulb-panel` sizes the renderer, no longer fullscreen);
  energy driven by `level` events, colour by state; the big state word + detail overlay the bottom.
- **Live · Claude Code** (top right) — a terminal-style feed of the turn: `prompt` (YOU),
  `action` (TOOL, with a one-line summary), `reply` (JARVIS), `notify` (NOTE), `error`/`voice`/`sleep`
  (SYS). Colour-coded tag chips; auto-scrolls when pinned to the bottom; capped at 220 lines.
- **Canvas viewer** (bottom-left) — a NAVIGABLE mini-viewer of the cards (mermaid / chart / stats /
  image / markdown), one at a time with `‹`/count/`›`/`×` controls; the shown card is auto-`canvas_focus`ed
  so "make this bigger" still resolves to it. Fed by the `board` snapshot + `canvas_add`/`canvas_remove`.
  Replaced the old full-page `/canvas` board (route removed; the board.js/canvas.html files are dormant).
  Bare URLs (feed) and `[text](url)` + bare URLs (canvas markdown) render as `target="_blank"` links.
- **Session** (bottom-right, small) — model, tools-used count, tokens in/out, session id, uptime.

Data plumbing added for the feed/stats: `JarvisBrain.ask()` emits `prompt` (raw user text) at the
start and `reply` (aggregated assistant text) at the end — the one chokepoint, so REPL/voice/Telegram
all feed the HUD; the `PreToolUse` hook emits `action {tool, summary}` (`summarize_action()` in
`hooks.py` picks the most telling input field — file tail / command / pattern / url — and strips the
`mcp__jarvis__` prefix) and calls `state.bump_tools()`; `StatusSnapshot` gained `tools_used`; the
model is surfaced on `brain.start()`; `agent.ask()` emits `reply` PER assistant TextBlock (live, as it
streams — so the feed shows his progress narration and answer as they happen). Audio levels:
`Speaker`/`Listener` `on_level` → `ctx.post_event` (thread-safe) → `level` events. Token usage from
`ResultMessage.usage` → `usage` events. The **Live // Claude Code** feed is a RAW CLI transcript (no
chips/bubbles): `›› prompt` (amber) · `·· TOOL value` (cyan) · `‹‹ reply` · `** note` · `!! err`.
Starts automatically in voice + text modes (config `dashboard.enabled`/`auto_open`);
`jarvis --demo-dashboard` drives it with synthetic `prompt`/`action`/`reply`/`usage`/`canvas` events.
`style.css` is a flat **"mission computer"** aesthetic (user picked it over the old glass/gradient look,
which read as "too AI-ish"): near-black field, `border:3px double` console frames + amber corner ticks
(`.panel::before/::after`) + bracketed titles sitting on the frame (`[ LIVE // CLAUDE CODE ]`) + subtle
CRT scanlines (`body.mission::after`), monospace throughout, amber+cyan, ZERO gradients/glass/rounded/
glow. `server.py` sets `Cache-Control: no-cache` on all responses (browser was serving stale assets).
Verified headless via Playwright: zero console/page errors; feed kinds you/action/reply/note, frames are
`border-style: double`, bracketed titles, preview renders + swaps to newest card, bulb sizes into its
panel (screenshot confirmed the aesthetic). 30/30 unit tests (`tests/test_hud_feed.py`: `bump_tools` +
`summarize_action`).

### Voice layer (`jarvis/voice/`, Phase 1)
One 16 kHz sounddevice input stream in a background thread (`listener.py`) feeds the wake
detector (`wake.py`) and an energy VAD (`vad.py`); results cross back to asyncio via
`call_soon_threadsafe`. Wake engines behind a common interface (`wake.py`, `build_detector`): **Vosk** (default,
keyless single word "jarvis", offline recognizer constrained to a {keyword,[unk]} grammar —
`frame_length` 1600), **openWakeWord** ("hey jarvis", ONNX — 1280), or **Porcupine** ("jarvis",
needs Picovoice key — 512); the listener reads `detector.frame_length` so it adapts. `stt.py` = faster-whisper (`base.en`, local).
**TTS is pluggable behind a shared player (`tts.py`, `tts_engine` config).** A base `_PcmSpeaker`
owns the playback loop (stream raw int16 mono PCM to a `sd.RawOutputStream`, sliced ~64 ms for
barge-in, feeding `on_level` for the bulb); THREE engines subclass it and just implement
`_pcm_chunks(text)`: **`ElevenLabsSpeaker`** (cloud, `text_to_speech.stream(pcm_16000)`; needs a
key + credits), **`KokoroSpeaker`** (local, free, offline — Kokoro-82M via **kokoro-onnx** +
ONNX Runtime + a bundled espeak phonemizer; NO torch/spaCy/compiler, so it installs clean on
Python 3.13), and **`XttsSpeaker`** (`tts_engine="xtts"` — local ZERO-SHOT VOICE CLONE via XTTS-v2
/ **coqui-tts**, added 2026-07-06). XTTS clones the voice in `xtts_reference` (a clean ~6–30 s mono
WAV) with no training; `load_xtts` builds the ~1.8 GB model ONCE and computes the reference's
`get_conditioning_latents` ONCE (an `_XttsBundle`, cached per (ref,device,checkpoint)), then
`_pcm_chunks` streams `model.inference_stream(...)` per sentence (`_float_to_pcm` handles the torch
tensors it yields). Auto-downloads via Coqui (sets `COQUI_TOS_AGREED`); torch-bound + HEAVY, so it
lives in its OWN env (Python 3.11) via `pip install -e .[clone]` and a CUDA torch build — CPU XTTS is
impractically slow. `xtts_device`/`xtts_language`/`xtts_temperature` configure it; `config.voice-clone.example.toml`
is the profile. `run_voice` pre-warms it off-thread + `_report_xtts_device` prints the realized device
([device] line, warns on silent CPU fallback). One caveat baked into the docs: cloning the MOVIE Jarvis
= Paul Bettany's real voice in copyrighted audio → personal-use only, don't distribute. `build_speaker(voice_config)` picks the engine; `Speaker` stays as a back-compat alias
for `ElevenLabsSpeaker`. Kokoro renders float32 @ 24 kHz → int16; the model (~310 MB) + voices
(~27 MB) **auto-download once** to `~/.jarvis/cache/kokoro/` (`load_kokoro`, process-wide cached);
British male voice `bm_george` (also bm_lewis/bm_daniel/bm_fable); `kokoro_device` maps to the ONNX
provider (`auto`|`cuda`|`cpu` — GPU needs `onnxruntime-gpu`). Measured **~0.84x RTF on this laptop's
CPU** (Intel i5-1145G7, no NVIDIA GPU — the user's P4000 is on a SEPARATE desktop), i.e. faster than
real-time, so sentence-streaming stays smooth; int8 quant was *slower* here (no VNNI) so we keep fp32.
`orchestrator.run_voice` pre-warms the Kokoro model off-thread so the first reply isn't stalled ~7 s.
**Device reporting (added 2026-07-06):** on voice start, `_report_stt_device` (reads back faster-whisper's
`ctranslate2` device via `Transcriber.device_report()`) and `_report_kokoro_device`/`_report_xtts_device`
(read the loaded model's realized ONNX providers / torch device via `tts.kokoro_providers`) print a
`[device]` line and, when `*_device="cuda"` but it landed on CPU (onnxruntime falls back SILENTLY without
onnxruntime-gpu; torch without a CUDA build sees no GPU), a `!` warning + `error` event — so a silent CPU
fallback on the desktop is impossible to miss.
`voicebank.py` (boot greeting) + `--check-voice` are engine-aware via `tts.render_utterance` (Kokoro
resampled 24 k→16 k for the cache). Extra: `pip install -e .[kokoro]`. `loop.py` orchestrates
wake→listen→STT→brain→sentence-streamed TTS (`chunker.py`), with a follow-up window and barge-in
(stop TTS + `brain.interrupt()`, then drain the generator so the SDK session ends cleanly).
**TTS-failure degradation:** ElevenLabs' `ApiError` stringifies to a giant `headers/status/body`
dump; `_speak` used to emit that raw per sentence, so an exhausted-credits reply flooded the feed
with one huge `!![tts]` line per chunk while re-hitting the dead quota each time. `tts.describe_tts_error(exc)`
now extracts just the meaningful sentence and flags `is_quota`; on quota exhaustion `loop.py` sets a
one-way `_tts_disabled` flag → reports ONE clean line ("Out of ElevenLabs voice credits — I'll carry on
in text only, sir.") and goes silent for the rest of the session (the reply text still streams to the
feed/REPL, just unspoken). Pure logic
(VAD, chunker, `describe_tts_error`) is unit-tested in `tests/test_voice_logic.py`. `jarvis --check-audio` diagnoses
the mic/speakers with no keys. Keys: with `tts_engine="kokoro"` (local) voice needs **NO keys at
all**; only `tts_engine="elevenlabs"` needs `ELEVENLABS_API_KEY` + `JARVIS_ELEVENLABS_VOICE_ID`
(Picovoice optional). Audio path needs 16 kHz mono int16 (verified on this machine).
**Onset-timeout semantics (important):** `record_utterance(timeout=)` passes `timeout` to the VAD as
an *onset* window (how long to wait for speech to START), NOT a hard cap on the recording. Once you
begin talking the utterance always runs to its natural end (silence_timeout / max_utterance_seconds);
it is never chopped mid-sentence. Onset only expires when the detection window is empty (no recent
above-threshold activity), so a turn that's just beginning is never cut. This fixed the follow-up
"cuts me off mid-sentence" bug. Both `SileroVAD` and `EnergyVAD` take `onset_timeout`.
**BUTTON-ONLY by default (user's choice, 2026-07-05):** `VoiceConfig.wake_enabled=False` — NO wake word
at all; Jarvis is called AND interrupted only with push-to-talk. `build_detector` returns a
`NullDetector` (frame_length 1600, never fires, no model load) when wake is off; `loop.py` gates the
wake task + `start_monitor` voice-barge on `self._wake_enabled` (a PTT press is the only barge-in);
orchestrator raises `VoiceConfigError` if wake is off and ptt is unavailable. Set `wake_enabled=true` to
re-enable the spoken wake word / voice barge-in.
**Push-to-talk (`voice/ptt.py`, pynput):** hold `ptt_key` (default **"ctrl_r"** = Right Ctrl — non-typing,
no Fn needed; was "f9" but the user's laptop needs Fn for F-keys) to talk; press it again to interrupt.
`PushToTalk` runs a global key listener, marshals press/release onto the loop. The loop races
`ptt.wait_press()` (and `wait_for_wake()` only if wake is on); a held-key turn records raw frames
(listener "ptt" mode) press→release. Config `ptt_enabled`.

### Remote / tunneled voice (`jarvis/voice/remote.py`, `voice.transport="browser"`, `jarvis --remote`)
Run the brain + STT + TTS on a GPU desktop; the user sits at a laptop with ONLY the dashboard tab,
which becomes the mic + speaker. **Only the audio TRANSPORT is swapped** — the `VoiceConversation`
loop, `ask_lock`, `SentenceChunker`, `Transcriber`, and the Kokoro/ElevenLabs synth are UNCHANGED —
via three duck-typed objects in `remote.py` selected when `voice.transport == "browser"`:
`BrowserListener` (buffers browser-sent int16 PCM while PTT held → float32 mono 16 k for STT),
`BrowserSpeaker` (subclasses `_PcmSpeaker`, reuses the engine's `_pcm_chunks`, overrides `_open_sink`
→ `_WsSink` streaming PCM over the socket instead of sounddevice), and `BrowserPTT` (press/release
futures resolved by browser control msgs). A `RemoteAudioHub` (on `ctx.remote_audio`) bridges the
dashboard `/ws` to them. **This required extracting the sink from `_PcmSpeaker.speak()`** (`tts.py`:
`_SoundDeviceSink`/`_open_sink`) — local playback is byte-identical. **Protocol** (one `/ws`): binary
frames = raw PCM (inbound=mic 16 k, outbound=TTS at the engine rate); text = JSON control
(`ptt_press`/`ptt_release`/`ping` in; `tts_start{rate}`/`tts_end`/`tts_flush`/`pong` out; plus
`audio_mode{remote}` on connect telling the tab to show the PTT UI). **All outbound audio goes through
ONE ordered `asyncio.Queue` drained by a single task** so `tts_start` can't overtake its PCM (worker
thread → loop via `call_soon_threadsafe`; barge `flush_tts` purges the queue + emits `tts_flush`).
`server.py`'s `/ws` loop now uses `receive()` (text OR binary OR disconnect) and routes binary→
`on_binary`, audio control→`on_control`, disconnect→`on_disconnect`; TTS binary is sent DIRECT to the
one audio client, not via the broadcast fan-out. Fixes folded from an adversarial critique: unified
send-queue (ordering); `mimetypes.add_type(".js","text/javascript")` at server import (Windows serves
`.js` as text/plain → `AudioWorklet.addModule` silently fails otherwise); per-client mic gate
(`on_binary` only from the active client); start_ptt armed on the PRESS + idempotent (pre-roll, so the
first syllable isn't clipped); disconnect discards the partial capture (no phantom turn) + cancels the
drain. **Frontend** (`static/app.js` + NEW `mic-worklet.js`): `getUserMedia` + an AudioWorklet that
resamples the mic from the device's NATIVE rate to 16 k (forcing a 16 k `AudioContext` STALLS
getUserMedia on real hardware) with a muted gain→destination so the worklet is actually pulled; gapless
PCM playback (scheduled `AudioBufferSourceNode`s, `flushPlayback` gain-ramps for barge-in); PTT = the
on-screen button OR the space bar; a secure-context guard. **`getUserMedia` needs a secure context —
`http://localhost` (incl. an SSH-forwarded port) qualifies, a bare LAN IP does NOT** — so the laptop
opens `http://localhost:<port>` through the SSH tunnel. Orchestrator guards: browser transport requires
the dashboard enabled + bound to loopback (`127.0.0.1`) — never exposed on the LAN (a mic-streaming,
brain-triggering socket has no auth). `jarvis --remote` sets transport=browser + dashboard on +
auto_open off + skips `play_boot` (no local speaker). Desktop GPU profile:
`config.remote-desktop.example.toml` (whisper/kokoro `device="cuda"`, `whisper_compute_type=
"int8_float16"` for Pascal/P4000; `pip install onnxruntime-gpu`). Verified headless (Playwright + fake
mic): secure-context true on 127.0.0.1, PTT UI shows, worklet MIME correct, getUserMedia works, PTT
press/release → server control, state listening→speaking, TTS reaches the client, barge flush, ZERO
console errors — the ONLY unverifiable leg is the worklet `process()` (headless has no audio backend so
it never runs; standard Web Audio, works on real hardware). Server transport (`feed_pcm`→float32,
ordering, no-client no-op, disconnect-discard, generation straggler-drop, can_speak) covered by
`tests/test_remote_voice.py` (9). 44/44 tests. A SECOND adversarial review (of the implementation)
confirmed 12 bugs, all folded in: barge stragglers (the still-running synth worker enqueues PCM/end
AFTER `flush_tts`'s purge — pending loop callbacks it can't see — so frames are GENERATION-tagged
[`_gen`/`_flushed_gen`, `_WsSink` stamps each frame] and dropped at drain; client mirrors with a
`ttsFlushed` gate + no gain-restore until the next `tts_start`); the mic resampler used the wrong
interp weight at non-48k rates (`t = 1 - phase/step`, correct for 44.1k, exact at 48k); PTT stuck-down
if released during first-time mic setup (`pttWanted` intent flag); server-speech to a fresh tab dropped
(client sends `audio_hello` on enable + every reconnect to claim the slot; hub created in
`Orchestrator.start()` BEFORE the dashboard so a tab connecting during model load already sees remote
mode); PTT stuck-down on ws drop (`releasePTT` in `onclose`); remote presence "still here?" is
unanswerable (no open mic) → `_presence_deliver` is transport-aware (uses tab connectivity, gates on
`speaker.can_speak`); ack tones silenced in remote mode (desktop-only); no wake detector built in
browser mode. Local `--voice` (sounddevice) path unchanged. NOT yet live-tested by the user on the real
desktop+laptop.

### Audio layer + progress narration (`jarvis/voice/sfx.py` + `voicebank.py` + `audiofx.py`)
DELIBERATELY MINIMAL as of 2026-07-05: the user found the per-tool "blips" and canned "on it, sir"
lines annoying, so both are GONE. `audiofx.py` (`AudioFX`) now only plays the `boot` power-up + a
`notify`/`error` cue (dropped `_TOOL_CUES`/`_PHRASES`/`_narrate`/`_thinking_line`/`_prime` and the
per-tool `_on_status` cue). `sfx.py` still synthesizes the cue palette procedurally with numpy (no
assets/copyright) and a `SoundPlayer` mixes overlapping cues in one persistent output stream.
`voicebank.py` renders the boot greeting through ElevenLabs **once** and caches it as a 16 kHz WAV
under `~/.jarvis/cache/voice/`. **Progress is now narrated by Jarvis HIMSELF** — a persona voice
addendum (`_NARRATE_ADDENDUM`, gated by `narrate_work`; `_SILENT_ADDENDUM` when off) tells him to speak
a short high-level clause before/after each meaningful step in his OWN words ("Searching the web for
X… found the fault; fixing it"), streamed as real assistant text (NOT pre-recorded → uses ElevenLabs
quota). `Orchestrator.play_boot()` runs the launch sequence. Config: `sfx_enabled` (boot + notify/error
only now), `sfx_volume`, `boot_sound`, `boot_line`, `boot_speed`, `narrate_work`, `voice_line_volume`.
CLI: `jarvis --demo-sfx` auditions the palette; `jarvis --no-sfx` disables. Degrades gracefully.

### Web browsing fallback (`jarvis/web.py` + `browse` tool, 2026-07-05)
The user wanted Jarvis to browse live in Chrome (esp. when web search/fetch fail). The Claude-in-Chrome
extension is NOT reachable from the Agent SDK (it's coupled to the interactive CLI via native messaging;
`setting_sources` doesn't load external MCP servers) — confirmed via research. So instead: `jarvis/web.py`
`BrowserSession` drives a real, VISIBLE Chrome with Playwright (lazy singleton; `channel="chrome"` →
bundled Chromium fallback; one reused tab; FRESH profile, so no access to the user's logins). Tool
`mcp__jarvis__browse(url, wait_seconds)` (tools.py, allow-listed) navigates + returns the rendered
title/url/innerText (capped 8000). `ctx.browser` holds the session; `orchestrator.stop()` closes it.
Persona tells Jarvis to try `WebFetch`/`WebSearch` first and ESCALATE to `browse` only when they can't
get the page. Optional dep: pyproject `web = ["playwright"]` (channel=chrome needs no `playwright install`).

## Conventions
- Phase 0 stays dependency-light (stdlib + `claude-agent-sdk`). Voice/dashboard/presence deps
  live in `pyproject.toml` extras and are installed per phase.
- Config is layered: defaults < `~/.jarvis` < `<project>/.jarvis` < env. Don't hardcode secrets.
- New agent→world capabilities = a new `@tool` in `brain/tools.py` (+ allow-list it).

## Run / verify
```powershell
.\.venv\Scripts\python.exe -m pip install -e .
jarvis                      # text REPL in the current project
```
Smoke test the SDK/auth path with a one-shot `query(...)` if a turn ever fails to respond.
