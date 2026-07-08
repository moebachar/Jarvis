"""Telegram bridge — reach the user when away, and let them reply.

Bi-directional via python-telegram-bot (async; runs inside our own asyncio loop, not a
separate one, so it shares the brain and event bus):
  * inbound  — a text message from the allow-listed chat → `on_message(text)` → the returned
               reply is sent straight back to that chat.
  * outbound — `send(text)` pushes a proactive note (e.g. a finished long task) to the chat.

Security: a bot token is effectively public (anyone who finds the bot can message it), so we
only ever honour messages from the configured `chat_id`. If no chat_id is configured yet we
*learn* it from the first message and announce it (used by `jarvis --telegram-id`), but a
mismatched chat is always ignored — a stranger can never drive the brain.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

# Inbound handlers: text → reply, or raw audio bytes → reply (both may return "").
OnMessage = Callable[[str], Awaitable[str]]
OnAudio = Callable[[bytes], Awaitable[str]]
OnEvent = Callable[..., None]


class TelegramBridge:
    def __init__(
        self,
        token: str,
        chat_id: str | int | None = None,
        on_message: OnMessage | None = None,
        on_audio: OnAudio | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        self._token = token
        self._chat_id: Optional[str] = str(chat_id) if chat_id else None
        self._on_message = on_message
        self._on_audio = on_audio
        self._on_event = on_event or (lambda *a, **k: None)
        self._app = None  # telegram.ext.Application, built in start()

    @property
    def chat_id(self) -> str | None:
        return self._chat_id

    @property
    def running(self) -> bool:
        return self._app is not None

    async def start(self) -> None:
        """Connect and begin long-polling for messages (within the current loop)."""
        from telegram.ext import Application, MessageHandler, filters

        app = Application.builder().token(self._token).build()
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle))
        # Voice notes (held-mic) and uploaded audio files → transcribe → answer.
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._handle_audio))
        await app.initialize()
        await app.start()
        # drop_pending_updates: ignore messages that piled up while Jarvis was offline.
        await app.updater.start_polling(drop_pending_updates=True)
        self._app = app
        me = None
        try:
            me = (await app.bot.get_me()).username
        except Exception:
            pass
        self._on_event("telegram", event="ready", username=me, chat_known=bool(self._chat_id))

    def _authorized(self, update) -> bool:
        """True if this chat may drive the brain. Learns the owner on first contact;
        afterwards a mismatched chat is always rejected (the token is effectively public)."""
        chat_id = str(update.effective_chat.id)
        if self._chat_id is None:
            self._chat_id = chat_id
            self._on_event("telegram", event="learned_chat_id", chat_id=chat_id)
            return True
        if chat_id != self._chat_id:
            self._on_event("telegram", event="rejected", chat_id=chat_id)
            return False
        return True

    async def _reply(self, message, reply: str) -> None:
        if not reply:
            return
        try:
            await message.reply_text(reply[:4000])
        except Exception as exc:
            self._on_event("error", where="telegram", message=str(exc))

    async def _handle(self, update, _context) -> None:
        message = getattr(update, "message", None)
        if message is None or not message.text or not self._authorized(update):
            return
        self._on_event("telegram", event="message", text=message.text)
        reply = ""
        if self._on_message is not None:
            try:
                reply = await self._on_message(message.text)
            except Exception as exc:  # never let a bad turn kill the poller
                self._on_event("error", where="telegram", message=str(exc))
                reply = "I ran into trouble handling that, sir."
        await self._reply(message, reply)

    async def _handle_audio(self, update, _context) -> None:
        message = getattr(update, "message", None)
        if message is None or not self._authorized(update):
            return
        media = message.voice or message.audio
        if media is None or self._on_audio is None:
            return
        self._on_event(
            "telegram", event="audio", kind="voice" if message.voice else "audio"
        )
        try:
            tg_file = await media.get_file()
            data = await tg_file.download_as_bytearray()
        except Exception as exc:
            self._on_event("error", where="telegram", message=str(exc))
            await self._reply(message, "I couldn't fetch that audio, sir.")
            return
        try:
            reply = await self._on_audio(bytes(data))
        except Exception as exc:
            self._on_event("error", where="telegram", message=str(exc))
            reply = "I ran into trouble with that audio, sir."
        await self._reply(message, reply)

    async def send(self, text: str) -> bool:
        """Proactively message the owner chat. Returns True if it went out."""
        if self._app is None or not self._chat_id or not text:
            return False
        try:
            await self._app.bot.send_message(chat_id=self._chat_id, text=text[:4000])
            return True
        except Exception as exc:
            self._on_event("error", where="telegram", message=str(exc))
            return False

    async def stop(self) -> None:
        app, self._app = self._app, None
        if app is None:
            return
        try:
            if app.updater is not None:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass  # best-effort shutdown
