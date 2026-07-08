"""`jarvis` command — Phase 0 text REPL.

Boots the orchestrator with cwd = the project you're in, then lets you converse
with Jarvis by typing. Live tool-use and notifications stream in dimmed lines so you
can see him work. Voice mode arrives in Phase 1; this REPL stays as a fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import webbrowser
from pathlib import Path

from .config import load_config
from .context import RuntimeContext
from .eventbus import EventBus
from .orchestrator import Orchestrator, VoiceConfigError
from .state import JarvisState, StateStore

DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
BOLD = "\033[1m"
GOLD = "\033[33m"

BANNER = f"""{GOLD}
   +------------------------------------------------+
   |   J . A . R . V . I . S .                      |
   |   Just A Rather Very Intelligent System        |
   +------------------------------------------------+{RESET}"""


def _enable_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _ensure_subscription_auth() -> bool:
    """Remove ANTHROPIC_API_KEY so the SDK uses the Claude subscription (CLI login).

    Returns True if a key was present and removed (worth telling the user).
    """
    return os.environ.pop("ANTHROPIC_API_KEY", None) is not None


def _dashboard_forward_note(config) -> str | None:
    """A one-liner telling the user how to reach the dashboard from ANOTHER machine.

    When the dashboard is on loopback (the secure default), that means an SSH port-forward:
    Jarvis + the project stay on this box; the browser tab lives on the laptop. Same tunnel the
    VS Code Remote-SSH session already uses. Returns None when there's nothing useful to say.
    """
    d = config.dashboard
    if not d.enabled:
        return None
    if d.host in ("127.0.0.1", "localhost", "::1"):
        return (f"{DIM}   View it from another machine: "
                f"{RESET}ssh -L {d.port}:localhost:{d.port} <this-host>{DIM}"
                f" → open http://localhost:{d.port}/ there.{RESET}")
    return (f"{DIM}   Dashboard is bound to {d.host}:{d.port} — reachable on the network with NO "
            f"auth; keep it on a trusted LAN only.{RESET}")


async def _event_printer(orchestrator: Orchestrator, stop: asyncio.Event) -> None:
    """Surface live status (tool use) and notifications as dimmed lines."""
    queue = orchestrator.bus.subscribe()
    last_tool: str | None = None
    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if event.type == "status":
                tool = event.data.get("current_tool")
                if tool and tool != last_tool:
                    print(f"\n{DIM}   . {tool}...{RESET}", flush=True)
                last_tool = tool
            elif event.type == "notify":
                print(f"\n{DIM}   (!) {event.data.get('message')}{RESET}", flush=True)
    finally:
        orchestrator.bus.unsubscribe(queue)


async def _read_line(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def _voice_printer(orchestrator: Orchestrator, stop: asyncio.Event) -> None:
    """Mirror the spoken conversation and live state into the terminal."""
    queue = orchestrator.bus.subscribe()
    last_state: str | None = None
    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if event.type == "heard":
                print(f"\n{CYAN}You{RESET} (heard) > {event.data.get('text')}", flush=True)
            elif event.type == "said":
                print(f"{BOLD}{GOLD}JARVIS{RESET} > {event.data.get('text')}", flush=True)
            elif event.type == "voice" and event.data.get("event") == "ready":
                thr = event.data.get("threshold")
                print(f"{DIM}   (mic ready; speech threshold ~{thr:.0f}){RESET}", flush=True)
            elif event.type == "voice" and event.data.get("event") == "missed":
                print(f"{DIM}   (heard the wake word, but didn't catch anything){RESET}", flush=True)
            elif event.type == "status":
                state = event.data.get("state")
                detail = event.data.get("detail") or ""
                if state in {"listening", "idle"} and state != last_state:
                    print(f"{DIM}   [{state}] {detail}{RESET}", flush=True)
                last_state = state
            elif event.type == "error":
                print(f"{DIM}   [error/{event.data.get('where')}] {event.data.get('message')}{RESET}", flush=True)
    finally:
        orchestrator.bus.unsubscribe(queue)


async def run_voice_session(config, project_dir: Path, resume: bool) -> None:
    orchestrator = Orchestrator(config, project_dir, resume=resume, voice_mode=True)
    title = config.user_title

    print(f"{DIM}Booting the brain in {project_dir} ...{RESET}", flush=True)
    await orchestrator.start()

    dashboard_url = None
    if config.dashboard.enabled:
        try:
            dashboard_url = await orchestrator.start_dashboard()
            if config.dashboard.auto_open:
                webbrowser.open(dashboard_url)
        except Exception as exc:
            print(f"{DIM}(dashboard failed to start: {exc!r}){RESET}", flush=True)

    stop = asyncio.Event()
    printer = asyncio.create_task(_voice_printer(orchestrator, stop))

    print(BANNER)
    remote = config.voice.transport == "browser"
    if remote:
        port = config.dashboard.port
        print(f"   {GOLD}Remote voice online, {title}.{RESET} This machine is doing STT + TTS; "
              f"the browser tab is the mic + speaker.")
        print(f"   From the laptop, forward the port and open the tab:")
        print(f"     {DIM}ssh -L {port}:localhost:{port} <this-desktop>{RESET}")
        print(f"     {GOLD}http://localhost:{port}/{RESET}  (must be localhost — a LAN IP blocks the mic)")
        print(f"   Hold the on-screen {GOLD}HOLD TO TALK{RESET} button (or the space bar) to speak; "
              f"press again to interrupt. Ctrl+C to stand down.\n")
    else:
        if dashboard_url:
            print(f"   Dashboard: {GOLD}{dashboard_url}{RESET}")
            note = _dashboard_forward_note(config)
            if note:
                print(note)
        if config.voice.wake_enabled:
            how = f"Say '{config.voice.wake_phrase}' or hold {config.voice.ptt_key} to talk"
        else:
            how = f"Hold {config.voice.ptt_key} to talk (press it again to interrupt me)"
        print(
            f"   Voice mode online, {title}. {how}; "
            f"I'll keep listening briefly after each reply. Ctrl+C to stand down.\n"
        )
    if not remote:  # the boot power-up/greeting plays on THIS machine's speakers; skip when remote
        await orchestrator.play_boot()
    try:
        await orchestrator.run_voice()
    except VoiceConfigError as exc:
        print(f"\n{GOLD}I'm afraid I can't speak yet, {title}.{RESET} Missing:")
        for item in exc.missing:
            print(f"   - {item}")
        print(
            f"\n{DIM}Add them to {project_dir}\\.jarvis\\.env (see .env.example), then rerun "
            f"`jarvis --voice`. Meanwhile, `jarvis` (text mode) works fully.{RESET}"
        )
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        await printer
        await orchestrator.stop()
        print(f"\n{DIM}Standing down, {title}.{RESET}")


async def run_repl(config, project_dir: Path, resume: bool) -> None:
    orchestrator = Orchestrator(config, project_dir, resume=resume)
    title = config.user_title

    print(f"{DIM}Booting the brain in {project_dir} ...{RESET}", flush=True)
    await orchestrator.start()

    dashboard_url = None
    if config.dashboard.enabled:
        try:
            dashboard_url = await orchestrator.start_dashboard()
            if config.dashboard.auto_open:
                webbrowser.open(dashboard_url)
        except Exception as exc:
            print(f"{DIM}(dashboard failed to start: {exc!r}){RESET}", flush=True)

    stop = asyncio.Event()
    printer = asyncio.create_task(_event_printer(orchestrator, stop))

    print(BANNER)
    if dashboard_url:
        print(f"   Dashboard: {GOLD}{dashboard_url}{RESET}")
        note = _dashboard_forward_note(config)
        if note:
            print(note)
    print(f"   At your service, {title}. Type your message, or 'exit' to dismiss me.\n")
    await orchestrator.play_boot()

    try:
        while True:
            try:
                text = (await _read_line(f"\n{CYAN}You{RESET} > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text.lower() in {"exit", "quit", ":q"}:
                break

            orchestrator.ctx.mark_user_active()  # local presence: you're at the terminal
            state = orchestrator.ctx.state
            print(f"{BOLD}{GOLD}JARVIS{RESET} > ", end="", flush=True)
            try:
                async with orchestrator.ask_lock:  # serialize vs. any Telegram turn
                    # Drive the HUD state machine for the turn (the REPL owns conversational
                    # state in text mode, just as the voice loop does in voice mode) so the
                    # dashboard bulb doesn't sit on IDLE / "standing by" while he works.
                    state.set_state(JarvisState.THINKING, "composing a reply")
                    async for chunk in orchestrator.ask(text):
                        print(chunk, end="", flush=True)
                print()
            except Exception as exc:  # keep the REPL alive on a single failed turn
                print(f"\n{DIM}[the brain raised: {exc!r}]{RESET}", flush=True)
            finally:
                state.set_state(JarvisState.IDLE, "standing by")
    finally:
        stop.set()
        await printer
        await orchestrator.stop()
        print(f"\n{DIM}Until next time, {title}.{RESET}")


async def _demo_feed(ctx: RuntimeContext) -> None:
    """Emit synthetic events so the dashboard can be previewed/debugged without audio."""
    state = ctx.state
    bus = ctx.bus
    state.set_model("claude-opus-4-8")
    heard = [
        "jarvis what's the project status",
        "show me the failing test",
        "deploy the dashboard to staging",
        "thank you, that's all",
    ]
    replies = [
        "Right away, sir. The build is green and all tests pass.",
        "I've isolated the failure to the auth middleware; shall I patch it?",
        "Deploying now. I'll let you know the moment it's live.",
        "Very good, sir. Standing by.",
    ]
    actions = [
        ("Grep", "Grep · project status"),
        ("Read", "Read · orchestrator.py"),
        ("Bash", "Bash · pytest -q"),
    ]
    # A sample visual so the "latest canvas" preview panel has something to show.
    bus.emit("canvas", kind="mermaid", title="Architecture",
             content="graph TD; U[You] -->|jarvis| V(Voice); V --> B[Brain]; "
                     "B --> T[Tools]; B --> D[Dashboard]")
    tick = 0
    i = 0
    while True:
        state.set_state("idle", "listening for 'jarvis'")
        await asyncio.sleep(1.6)

        state.set_state("listening", "listening")
        for _ in range(16):
            tick += 1
            bus.emit("level", value=0.25 + 0.5 * abs(math.sin(tick * 0.4)), source="mic")
            await asyncio.sleep(0.07)
        prompt = heard[i % len(heard)]
        bus.emit("heard", text=prompt)
        bus.emit("prompt", text=prompt)

        state.set_state("thinking", "composing a reply")
        for tool, summary in actions:
            state.set_tool(tool)
            state.bump_tools()
            bus.emit("action", tool=tool, summary=summary)
            await asyncio.sleep(0.5)
        state.set_tool(None)
        bus.emit("usage", input_tokens=1200 + i * 90, output_tokens=280 + i * 40)

        reply = replies[i % len(replies)]
        state.set_state("speaking", reply[:42])
        bus.emit("said", text=reply)
        bus.emit("reply", text=reply)
        for _ in range(26):
            tick += 1
            bus.emit("level", value=0.3 + 0.6 * abs(math.sin(tick * 0.55)), source="speaking")
            await asyncio.sleep(0.07)
        i += 1


async def run_dashboard_demo(config, project_dir: Path) -> None:
    from .dashboard.server import DashboardServer

    bus = EventBus()
    state = StateStore(bus)
    ctx = RuntimeContext(config, bus, state, project_dir)
    ctx.loop = asyncio.get_running_loop()

    server = DashboardServer(ctx, config.dashboard.host, config.dashboard.port)
    await server.start()
    print(f"{BANNER}")
    print(f"   Dashboard demo running at {GOLD}{server.url}{RESET}  (Ctrl+C to stop)\n")
    if config.dashboard.auto_open:
        webbrowser.open(server.url)

    feeder = asyncio.create_task(_demo_feed(ctx))
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        feeder.cancel()
        await server.stop()


async def run_canvas_demo(config, project_dir: Path) -> None:
    """Push one of each canvas kind to the dashboard Canvas page, to preview/debug it."""
    from .dashboard.server import DashboardServer

    import tempfile

    bus = EventBus()
    state = StateStore(bus)
    # Use a throwaway project dir so the preview doesn't persist demo cards into the
    # user's real board (<project>/.jarvis/state/canvas-board.json).
    ctx = RuntimeContext(config, bus, state, Path(tempfile.mkdtemp(prefix="jarvis-demo-")))
    ctx.loop = asyncio.get_running_loop()

    server = DashboardServer(ctx, config.dashboard.host, config.dashboard.port)
    await server.start()
    print(f"{BANNER}")
    print(f"   Canvas demo running at {GOLD}{server.url}{RESET}  "
          f"(browse the cards in the CANVAS panel; Ctrl+C to stop)\n")
    if config.dashboard.auto_open:
        webbrowser.open(server.url)

    # Buffered server-side, so it doesn't matter that the browser connects a moment later.
    samples = [
        ("mermaid", "Architecture",
         "graph TD; U[You] -->|jarvis| V(Voice); V --> B[Brain]; B --> T[Tools]; "
         "B --> P[Presence]; P --> TG[Telegram]; B --> D[Dashboard]"),
        ("stats", "Project at a glance", json.dumps([
            {"label": "Phase", "value": "4", "hint": "canvas"},
            {"label": "Tools", "value": "4", "hint": "mcp__jarvis__*"},
            {"label": "Tests", "value": "19", "hint": "passing"},
        ])),
        ("chart", "Tokens per turn", json.dumps({
            "type": "line", "labels": ["t1", "t2", "t3", "t4", "t5"],
            "datasets": [{"label": "in", "data": [1200, 1450, 1300, 1700, 1600]},
                         {"label": "out", "data": [280, 320, 300, 410, 360]}],
        })),
        ("markdown", "Notes",
         "# Canvas\nRenders **diagrams**, charts, stats, images and `markdown`.\n"
         "- on his initiative\n- or when you ask to *show* something"),
    ]

    feeder_done = asyncio.Event()

    async def feed():
        await asyncio.sleep(0.8)
        for kind, title, content in samples:
            bus.emit("canvas", kind=kind, title=title, content=content)
            await asyncio.sleep(0.25)
        feeder_done.set()

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        feeder.cancel()
        await server.stop()


def run_sfx_demo(config) -> None:
    """Audition the synthesized action-sound palette (no keys, no network)."""
    import time

    from .voice.sfx import SoundPlayer, build_sound_bank

    order = [
        ("boot", "power-up / launch ambience"),
        ("thinking", "thinking / processing"),
        ("read", "reading files"),
        ("search", "searching the codebase"),
        ("web", "querying the web"),
        ("write", "writing / editing code"),
        ("exec", "running a command"),
        ("done", "answer ready"),
        ("notify", "attention / notify"),
        ("error", "something went wrong"),
    ]
    bank = build_sound_bank(16000)
    player = SoundPlayer(sample_rate=16000, device=config.voice.output_device, master=0.9)
    player.start()
    print(f"{BANNER}")
    print(f"   Auditioning the sound palette (volume {config.voice.sfx_volume}).\n")
    try:
        for name, desc in order:
            print(f"   {GOLD}*{RESET} {name:<9} {DIM}{desc}{RESET}", flush=True)
            gain = config.voice.sfx_volume * (1.3 if name == "boot" else 1.0)
            player.play(bank.get(name), gain=gain)
            time.sleep(4.0 if name == "boot" else 1.1)
        time.sleep(0.4)
    finally:
        player.stop()
    print(f"\n{DIM}Tell me which cues to change and I'll retune them.{RESET}")


def run_samples_demo(config) -> None:
    """Audition every raw FUI candidate clip so you can pick/remap cues."""
    import time

    from .voice.sfx import SoundPlayer, _load_wav, user_sounds_dir

    cand = user_sounds_dir() / "candidates"
    files = sorted(cand.glob("*.wav")) if cand.is_dir() else []
    if not files:
        print(f"{DIM}No candidate clips staged yet (looked in {cand}).{RESET}")
        return
    player = SoundPlayer(device=config.voice.output_device, master=0.9)
    player.start()
    print(f"{BANNER}")
    print(f"   Auditioning {len(files)} raw FUI candidates — note the ones you like.\n")
    try:
        for f in files:
            arr = _load_wav(f)
            dur = (arr.size / 16000) if arr is not None else 0.0
            print(f"   {GOLD}*{RESET} {f.stem:<12} {DIM}{dur:4.2f}s{RESET}", flush=True)
            player.play(arr, gain=config.voice.sfx_volume)
            time.sleep(max(1.0, dur + 0.6))
        time.sleep(0.3)
    finally:
        player.stop()
    print(f"\n{DIM}Tell me e.g. 'boot=binary_00, exec=hud_14' and I'll rewire the cues.{RESET}")


async def run_telegram_id(config) -> None:
    """Discover your Telegram chat id: start the bot, message it once, print the id."""
    token = config.telegram.bot_token
    if not token:
        print(
            f"{GOLD}No Telegram bot token configured.{RESET}\n"
            f"   1. In Telegram, message {BOLD}@BotFather{RESET}, send /newbot, follow the prompts.\n"
            f"   2. Put the token in .jarvis\\.env as  TELEGRAM_BOT_TOKEN=...\n"
            f"   3. Rerun `jarvis --telegram-id`."
        )
        return

    from .presence.telegram_bot import TelegramBridge

    got = asyncio.Event()
    found: dict[str, str | None] = {"chat_id": None, "username": None}

    def on_event(event_type: str, **data) -> None:
        if event_type != "telegram":
            return
        ev = data.get("event")
        if ev == "ready":
            found["username"] = data.get("username")
            print(
                f"   Bot {BOLD}@{data.get('username')}{RESET} is online. "
                f"Open Telegram, find it, and send it any message...\n"
            )
        elif ev == "learned_chat_id":
            found["chat_id"] = data.get("chat_id")
            got.set()

    async def on_message(_text: str) -> str:
        return f"Hello, sir. Your chat id is {found['chat_id']}. I'm wired in."

    # chat_id=None so it learns from your first message regardless of any configured id.
    bridge = TelegramBridge(token, None, on_message=on_message, on_event=on_event)
    print(BANNER)
    print(f"   {DIM}Connecting to Telegram...{RESET}")
    await bridge.start()
    try:
        await asyncio.wait_for(got.wait(), timeout=120)
        cid = found["chat_id"]
        print(f"\n   {GOLD}Your chat id:{RESET} {BOLD}{cid}{RESET}")
        print(f"   {DIM}Add it to .jarvis\\.env as:{RESET}  JARVIS_TELEGRAM_CHAT_ID={cid}")
        print(f"   {DIM}Then `jarvis` or `jarvis --voice` can reach you there when you're away.{RESET}")
    except asyncio.TimeoutError:
        print(f"\n   {DIM}No message received in 120s. Rerun and send the bot a message.{RESET}")
    finally:
        await bridge.stop()


def run_ptt_check(config) -> None:
    """Diagnose push-to-talk: name every key you press and flag the configured PTT key.

    Push-to-talk only runs in `jarvis --voice`; this lets you confirm your key fires at all
    (and, if the configured name is wrong, shows the real name to put in [voice] ptt_key).
    """
    key_name = (config.voice.ptt_key or "").strip().lower()
    print(BANNER)
    print(f"   Push-to-talk check. Configured key: {GOLD}{key_name}{RESET}")
    print(f"   {DIM}Press/hold the key you want to use. Every key is named below; the configured")
    print(f"   one is flagged. If it never flags, copy the printed name into [voice] ptt_key.{RESET}")
    print(f"   {DIM}Press Esc (or Ctrl+C) to stop.{RESET}\n")
    try:
        from pynput import keyboard
    except Exception as exc:
        print(f"   {GOLD}pynput is unavailable{RESET} ({exc!r}).")
        print(f"   Install the voice extra:  {BOLD}pip install -e .[voice]{RESET}")
        return

    target = getattr(keyboard.Key, key_name, None)

    def name_of(key) -> str:
        if hasattr(key, "name"):
            return key.name                       # special key, e.g. "ctrl_r"
        ch = getattr(key, "char", None)
        return repr(ch) if ch is not None else str(key)

    def matches(key) -> bool:
        if target is not None:
            return key == target
        ch = getattr(key, "char", None)
        return bool(key_name) and ch == key_name[:1]

    def on_press(key) -> None:
        flag = f"  {GOLD}<<< this is your ptt_key{RESET}" if matches(key) else ""
        print(f"   press    {name_of(key)}{flag}")

    def on_release(key):
        if matches(key):
            print(f"   release  {name_of(key)}")
        if key == keyboard.Key.esc:
            print(f"\n   {DIM}Stopped.{RESET}")
            return False

    try:
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
    except KeyboardInterrupt:
        pass


def main() -> None:
    _enable_utf8()
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="A voice-driven, autonomous Claude Code companion (Phase 0: text REPL).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project directory Jarvis should live in (default: current directory).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the previous session for this project, if one exists.",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Voice mode: wake word 'jarvis', speech in and out (Phase 1).",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Remote/tunneled voice: a browser tab (the dashboard) is the mic + speaker while "
             "THIS machine does STT + TTS (ideally on a GPU). Run on a desktop, SSH-forward the "
             "dashboard port to a laptop, open http://localhost:<port> there, and talk.",
    )
    parser.add_argument(
        "--check-audio",
        action="store_true",
        help="Diagnose mic/speakers (no keys needed); run this before --voice.",
    )
    parser.add_argument(
        "--check-voice",
        action="store_true",
        help="Validate the ElevenLabs key + voice id by speaking a test phrase.",
    )
    parser.add_argument(
        "--check-ptt",
        action="store_true",
        help="Test the push-to-talk key: names every key you press so you can confirm it fires.",
    )
    parser.add_argument(
        "--demo-dashboard",
        action="store_true",
        help="Run the dashboard with synthetic events (no audio) to preview/debug it.",
    )
    parser.add_argument(
        "--demo-canvas",
        action="store_true",
        help="Preview the dashboard Canvas page with one of each visual kind.",
    )
    parser.add_argument(
        "--demo-sfx",
        action="store_true",
        help="Audition the action-sound palette (no keys needed).",
    )
    parser.add_argument(
        "--demo-samples",
        action="store_true",
        help="Audition every raw FUI sample candidate (to pick/remap cues).",
    )
    parser.add_argument(
        "--no-sfx",
        action="store_true",
        help="Disable the cinematic sound effects + spoken progress.",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the live dashboard in voice mode.",
    )
    parser.add_argument(
        "--telegram-id",
        action="store_true",
        help="Discover your Telegram chat id: start the bot, message it once, print the id.",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Run without the Telegram presence bridge, even if a token is configured.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Text REPL mode (the default).",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Scaffold a starter .jarvis/ (config.toml + .env.example) in this project, then exit.",
    )
    parser.add_argument(
        "--machine-init",
        action="store_true",
        help="Record this machine's profile in ~/.jarvis/machine.toml (auto-detects the GPU). "
             "Combine with --gpu/--no-gpu to force it, and --clone to mark the voice clone.",
    )
    parser.add_argument(
        "--gpu",
        dest="gpu",
        action="store_const",
        const=True,
        default=None,
        help="With --machine-init: force GPU on (skip auto-detect).",
    )
    parser.add_argument(
        "--no-gpu",
        dest="gpu",
        action="store_const",
        const=False,
        help="With --machine-init: force GPU off.",
    )
    parser.add_argument(
        "--clone",
        action="store_true",
        help="With --machine-init: mark that the XTTS-v2 voice clone is installed here.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed Jarvis version and exit.",
    )
    args = parser.parse_args()

    if args.version:
        try:
            from importlib.metadata import version
            print(f"jarvis {version('jarvis')}")
        except Exception:
            print("jarvis (version unknown — not installed as a package)")
        return

    if args.machine_init:
        from .machine import resolve_gpu, write_machine_profile
        gpu = resolve_gpu(args.gpu)
        path = write_machine_profile(gpu=gpu, voice_clone=args.clone)
        how = "forced" if args.gpu is not None else "auto-detected"
        print(f"{GOLD}Machine profile written{RESET} → {path}")
        print(f"   GPU: {GOLD}{gpu}{RESET} ({how})"
              + (f"   ·   voice clone: {GOLD}on{RESET}" if args.clone else ""))
        if gpu:
            print(f"{DIM}   Runtime will use CUDA for STT + TTS on this machine "
                  f"(override per-field in [voice] if you ever need to).{RESET}")
        return

    project_dir = Path(args.project or os.getcwd()).resolve()

    if args.init:
        from .scaffold import init_project
        created, skipped = init_project(project_dir)
        for path in created:
            print(f"{GOLD}created{RESET} {path}")
        for path in skipped:
            print(f"{DIM}kept   {path} (already present){RESET}")
        print(f"\n{DIM}Edit .jarvis/config.toml to taste, copy .env.example → .env for any keys, "
              f"then run `jarvis`.{RESET}")
        return
    config = load_config(project_dir)
    if args.remote:
        # The browser tab is the audio endpoint; the dashboard is the transport, so it must be
        # on, and we don't auto-open a browser here (the tab lives on the far side of the tunnel).
        config.voice.transport = "browser"
        config.dashboard.enabled = True
        config.dashboard.auto_open = False
        # The chime/barge tones play through THIS machine's sounddevice — inaudible to the remote
        # user and needing an output device the desktop may not have. Turn them off.
        config.voice.ack_sound = False
    if args.no_dashboard:
        config.dashboard.enabled = False
    if args.no_sfx:
        config.voice.sfx_enabled = False
        config.voice.boot_sound = False
        config.voice.narrate_work = False
    if args.no_telegram:
        config.telegram.enabled = False

    if args.check_audio:
        from .voice.diagnostics import check_audio
        check_audio(config)
        return

    if args.check_voice:
        from .voice.diagnostics import check_voice
        check_voice(config)
        return

    if args.check_ptt:
        run_ptt_check(config)
        return

    if args.demo_dashboard:
        try:
            asyncio.run(run_dashboard_demo(config, project_dir))
        except KeyboardInterrupt:
            pass
        return

    if args.demo_canvas:
        try:
            asyncio.run(run_canvas_demo(config, project_dir))
        except KeyboardInterrupt:
            pass
        return

    if args.demo_sfx:
        try:
            run_sfx_demo(config)
        except KeyboardInterrupt:
            pass
        return

    if args.demo_samples:
        try:
            run_samples_demo(config)
        except KeyboardInterrupt:
            pass
        return

    if args.telegram_id:
        try:
            asyncio.run(run_telegram_id(config))
        except KeyboardInterrupt:
            pass
        return

    removed_key = _ensure_subscription_auth()

    if removed_key:
        print(
            f"{DIM}(note) ANTHROPIC_API_KEY was set; unset for this run so the brain "
            f"uses your Claude subscription rather than billing the API.{RESET}"
        )

    runner = run_voice_session if (args.voice or args.remote) else run_repl
    try:
        asyncio.run(runner(config, project_dir, args.resume))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
