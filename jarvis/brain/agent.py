"""JarvisBrain — a long-lived Claude Agent SDK session wearing the Jarvis persona.

Responsibilities:
  * Build ClaudeAgentOptions that (a) ground the agent in the user's project
    (cwd + setting_sources load CLAUDE.md and settings.json), (b) apply the persona,
    (c) honour the user's autonomy settings, (d) expose Jarvis's custom tools and hooks.
  * Maintain one persistent multi-turn session (ClaudeSDKClient) for natural back-and-forth.
  * Persist the session id so a future run can resume.
  * Stream the assistant's text out as it arrives (consumed by the REPL now, TTS later).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from ..config import state_dir
from ..context import RuntimeContext
from .hooks import build_hooks
from .persona import build_system_prompt
from .tools import build_tool_server


class JarvisBrain:
    def __init__(self, ctx: RuntimeContext, resume: bool = False, voice_mode: bool = False) -> None:
        self.ctx = ctx
        self._resume = resume
        self._voice_mode = voice_mode
        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = None
        self._session_file = state_dir(ctx.project_dir) / "last_session.json"

    # ----------------------------------------------------------------- options
    def _build_options(self) -> ClaudeAgentOptions:
        cfg = self.ctx.config
        server, allowed = build_tool_server(self.ctx)
        hooks = build_hooks(self.ctx)

        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": build_system_prompt(cfg, voice_mode=self._voice_mode),
        }

        resume_id = self._load_session_id() if self._resume else None

        return ClaudeAgentOptions(
            cwd=str(self.ctx.project_dir),
            setting_sources=cfg.claude.setting_sources,   # loads CLAUDE.md + settings.json
            system_prompt=system_prompt,
            permission_mode=cfg.claude.permission_mode,    # "auto" => autonomous
            model=cfg.claude.model,
            mcp_servers={"jarvis": server},
            allowed_tools=[*allowed, *cfg.claude.extra_allowed_tools],
            hooks=hooks,
            resume=resume_id,
            include_partial_messages=False,                # full blocks suffice for text mode
        )

    # ------------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        options = self._build_options()
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        # Surface the configured model on the HUD (the SDK otherwise defaults it silently).
        if self.ctx.config.claude.model:
            self.ctx.state.set_model(self.ctx.config.claude.model)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    # ------------------------------------------------------------------- interaction
    async def ask(self, text: str) -> AsyncIterator[str]:
        """Send a user turn and yield assistant text chunks as they arrive."""
        if self._client is None:
            raise RuntimeError("JarvisBrain.start() must be called before ask().")

        # NOTE: conversational state (thinking/speaking/listening/idle) is owned by the
        # caller (the voice loop / REPL), not the brain — so we don't fight it here.
        state = self.ctx.state

        # If the user has a canvas card selected on the board, prepend a short context
        # block so "this"/"it" resolves to that card. Injected here (the one chokepoint)
        # so it works for the REPL, voice, Telegram and presence turns alike.
        hint = self.ctx.canvas_focus_hint()
        query = f"[Dashboard canvas context — not user text]\n{hint}\n\n{text}" if hint else text

        # Mirror the turn onto the bus for the dashboard's live "Claude Code" feed.
        # We emit the user's *raw* text (not the canvas-context wrapper), then each of the
        # assistant's text blocks as it arrives — including the brief progress narration he
        # emits between tool calls — so the feed reads like the live CLI transcript.
        if text and text.strip():
            self.ctx.bus.emit("prompt", text=text.strip())

        await self._client.query(query)
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        line = block.text.strip()
                        if line:
                            self.ctx.bus.emit("reply", text=line)
                        yield block.text
            elif isinstance(message, ResultMessage):
                self._session_id = getattr(message, "session_id", None)
                if self._session_id:
                    state.set_session(self._session_id)
                    self._save_session_id(self._session_id)
                self._emit_usage(message)

        state.set_tool(None)

    def _emit_usage(self, message) -> None:
        """Surface token usage (if present) on the bus for the dashboard."""
        usage = getattr(message, "usage", None)
        if usage is None:
            return

        def field(name):
            if isinstance(usage, dict):
                return usage.get(name)
            return getattr(usage, name, None)

        data = {"input_tokens": field("input_tokens"), "output_tokens": field("output_tokens")}
        if any(v is not None for v in data.values()):
            self.ctx.bus.emit("usage", **data)

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    # ------------------------------------------------------------------- session io
    def _save_session_id(self, session_id: str) -> None:
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_file.write_text(
                json.dumps({"session_id": session_id}), encoding="utf-8"
            )
        except OSError:
            pass  # session persistence is best-effort

    def _load_session_id(self) -> str | None:
        try:
            data = json.loads(self._session_file.read_text(encoding="utf-8"))
            return data.get("session_id")
        except (OSError, json.JSONDecodeError):
            return None
