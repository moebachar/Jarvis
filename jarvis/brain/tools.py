"""Custom in-process tools — the bridge the agent uses to drive the outside world.

Registered as an SDK MCP server, so the agent calls them like any other tool
(`mcp__jarvis__set_status`, `mcp__jarvis__notify_user`). They close over the
RuntimeContext to reach the state store and notification queue.

Phase 0 ships `set_status` and `notify_user`. `show_on_dashboard` (Phase 2/4) and
`sleep_until` (Phase 3) will join them here.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..context import RuntimeContext
from ..state import JarvisState

SERVER_NAME = "jarvis"


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def _inline_image(content: str, project_dir: Path) -> str:
    """For kind='image': if content is a local file path, inline it as a data URL so the
    browser can show it without the server exposing the filesystem. URLs/data pass through."""
    if content.startswith(("data:", "http://", "https://", "/static/")):
        return content
    path = Path(content)
    if not path.is_absolute():
        path = project_dir / content
    try:
        if path.is_file() and path.stat().st_size <= 8_000_000:
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{b64}"
    except OSError:
        pass
    return content  # leave as-is; the page will try to load it directly


def build_tool_server(ctx: RuntimeContext):
    """Return (mcp_server_config, allowed_tool_names) for the given context."""

    @tool(
        "set_status",
        "Update your live status shown on Jarvis's dashboard/HUD. Call this as you start "
        "and finish pieces of work so the user can see what you're doing. "
        "state is one of: idle, listening, thinking, speaking, working, sleeping.",
        {"state": str, "detail": str},
    )
    async def set_status(args: dict[str, Any]) -> dict[str, Any]:
        ctx.state.set_state(args.get("state", "working"), args.get("detail", ""))
        return _text("Status updated.")

    @tool(
        "notify_user",
        "Queue a short spoken/written message to deliver to the user, used when you have "
        "something worth their attention — typically after finishing a task or while they "
        "may be away. Keep it to one or two sentences. importance is: low, normal, or high.",
        {"message": str, "importance": str},
    )
    async def notify_user(args: dict[str, Any]) -> dict[str, Any]:
        ctx.notify(args["message"], args.get("importance", "normal"))
        return _text("Noted — I'll make sure he hears it.")

    @tool(
        "sleep_until",
        "Go quiet and heads-down when you're settling into a longer task and won't have "
        "anything to say for a while — the user won't be disturbed and your live status "
        "shows you're working. A background heartbeat checks in periodically, so you never "
        "go silent forever; when you finish, use notify_user to reach the user (by voice if "
        "they're here, or Telegram if they're away). Give a short reason and roughly how many "
        "minutes you expect to be quiet.",
        {"reason": str, "minutes": int},
    )
    async def sleep_until(args: dict[str, Any]) -> dict[str, Any]:
        reason = args.get("reason") or "Working quietly"
        try:
            minutes = int(args.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        ctx.state.set_state(JarvisState.SLEEPING, reason)
        ctx.bus.emit("sleep", reason=reason, minutes=minutes)
        return _text(f"Understood — working quietly{f' (~{minutes} min)' if minutes else ''}. "
                     "I'll surface when there's something worth your attention.")

    @tool(
        "show_on_dashboard",
        "Display a visual on Jarvis's dashboard Canvas page (at the dashboard URL under "
        "/canvas) when a picture beats a spoken paragraph — a diagram, a chart, key stats, a "
        "screenshot, or formatted notes. kind is one of:\n"
        "  'mermaid'  — content is Mermaid source (e.g. 'graph TD; A[Client]-->B[Server]').\n"
        "  'chart'    — content is JSON: {\"type\":\"bar|line|pie|doughnut\","
        "\"labels\":[..],\"datasets\":[{\"label\":\"..\",\"data\":[..]}]}.\n"
        "  'stats'    — content is a JSON array of {\"label\":\"..\",\"value\":\"..\",\"hint\":\"..\"}.\n"
        "  'image'    — content is a path to an image file (e.g. a screenshot) or a data/http URL.\n"
        "  'markdown' — content is Markdown text.\n"
        "title is a short heading shown above the visual.",
        {"kind": str, "content": str, "title": str},
    )
    async def show_on_dashboard(args: dict[str, Any]) -> dict[str, Any]:
        kind = (args.get("kind") or "markdown").strip().lower()
        content = args.get("content") or ""
        title = args.get("title") or ""
        if kind == "image":
            content = _inline_image(content, ctx.project_dir)
        ctx.bus.emit("canvas", kind=kind, title=title, content=content)
        return _text(f"Shown on the dashboard canvas ({kind}).")

    @tool(
        "browse",
        "Open a web page in a REAL, visible Chrome browser and read its fully-rendered text. Use "
        "this as a FALLBACK when WebFetch or WebSearch can't get what you need — a JavaScript-heavy "
        "page that returns little content, a page that blocks simple fetches, or anything you must "
        "see live and fully rendered. Give the url; optionally set wait_seconds (0-15) to let a slow "
        "page finish loading. Returns the page title, final URL and readable text. A window opens so "
        "the user can watch; a fresh browser profile is used (it is NOT signed in to their accounts). "
        "Try a normal WebFetch/WebSearch first — reach for this when that isn't enough.",
        {"url": str, "wait_seconds": int},
    )
    async def browse(args: dict[str, Any]) -> dict[str, Any]:
        from ..web import BrowserSession

        if ctx.browser is None:
            ctx.browser = BrowserSession()
        try:
            wait = float(args.get("wait_seconds") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        ctx.state.set_state(JarvisState.WORKING, "Browsing the web")
        try:
            res = await ctx.browser.browse(args.get("url", ""), wait_seconds=wait)
        except ImportError:
            return _text("Browser automation isn't installed — add the 'web' extra (playwright).")
        except Exception as exc:  # never let a browser hiccup kill the turn
            return _text(f"I couldn't open the browser: {exc}")
        if not res.get("ok"):
            return _text(f"I couldn't load that page: {res.get('error', 'unknown error')}")
        body = res.get("text") or "(the page had no readable text)"
        suffix = "\n\n[text truncated]" if res.get("truncated") else ""
        return _text(f"Loaded: {res.get('title') or '(untitled)'}\nURL: {res.get('url')}\n\n{body}{suffix}")

    server = create_sdk_mcp_server(
        SERVER_NAME, "0.1.0",
        [set_status, notify_user, sleep_until, show_on_dashboard, browse],
    )
    allowed = [
        f"mcp__{SERVER_NAME}__set_status",
        f"mcp__{SERVER_NAME}__notify_user",
        f"mcp__{SERVER_NAME}__sleep_until",
        f"mcp__{SERVER_NAME}__show_on_dashboard",
        f"mcp__{SERVER_NAME}__browse",
    ]
    return server, allowed
