"""Lifecycle hooks that mirror the agent's activity into the state store.

These are *observers*: they read the tool being used and update the live status,
then return an empty result so they never alter the agent's behaviour. This is what
lets the dashboard (Phase 2) show "using Read…" / "running Bash…" in real time.

Hook callback signature (verified against claude_agent_sdk 0.2.x):
    async def cb(input_data, tool_use_id: str | None, context: HookContext) -> dict
Returning {} is a no-op.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import HookMatcher

from ..context import RuntimeContext


def _field(data: Any, key: str) -> Any:
    """Read a field whether the hook input is a dict or an object."""
    if isinstance(data, dict):
        return data.get(key)
    return getattr(data, key, None)


# For each tool, the input field that best summarises what it's doing — so the live
# "Claude Code" feed reads like a terminal ("Read agent.py", "Bash pytest -q", …).
_SUMMARY_FIELDS = (
    "file_path", "path", "command", "pattern", "query", "url",
    "prompt", "description", "title", "notebook_path", "content",
)


def _short(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_action(tool_name: str, tool_input: Any) -> str:
    """A one-line, human-readable summary of a tool call for the activity feed."""
    name = str(tool_name or "tool")
    # Jarvis's own MCP tools read nicer without the mcp__jarvis__ prefix.
    if name.startswith("mcp__jarvis__"):
        name = name.split("__")[-1]
    if not isinstance(tool_input, dict):
        return name
    for key in _SUMMARY_FIELDS:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            # File-path-like values: show just the tail so the line stays short.
            if key in ("file_path", "path", "notebook_path"):
                val = val.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or val
            return f"{name} · {_short(val)}"
    return name


def build_hooks(ctx: RuntimeContext) -> dict[str, list[HookMatcher]]:
    async def pre_tool_use(input_data: Any, tool_use_id: str | None, context: Any) -> dict:
        tool_name = _field(input_data, "tool_name") or "tool"
        ctx.state.set_tool(tool_name)
        ctx.state.bump_tools()
        ctx.bus.emit(
            "action",
            tool=str(tool_name),
            summary=summarize_action(tool_name, _field(input_data, "tool_input")),
        )
        return {}

    async def post_tool_use(input_data: Any, tool_use_id: str | None, context: Any) -> dict:
        ctx.state.set_tool(None)
        return {}

    return {
        "PreToolUse": [HookMatcher(hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(hooks=[post_tool_use])],
    }
