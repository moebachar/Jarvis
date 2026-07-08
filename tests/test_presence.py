"""Unit tests for the Phase 3 presence layer: note routing + heartbeat delivery.

No network and no Telegram account needed — the bridge is faked. Async paths are driven
with asyncio.run() so we don't pull in pytest-asyncio.
"""

import asyncio
from pathlib import Path

from jarvis.config import JarvisConfig
from jarvis.context import PendingNote, RuntimeContext
from jarvis.eventbus import EventBus
from jarvis.presence.manager import PresenceManager
from jarvis.state import StateStore


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail = False
        self.running = True

    async def send(self, text: str) -> bool:
        if self.fail:
            return False
        self.sent.append(text)
        return True


class FakeLink:
    """Stand-in for VoiceLink: records the request and returns a canned delivered/silent."""
    def __init__(self, attached=True, result=True) -> None:
        self.attached = attached
        self.result = result
        self.requested = None

    async def request(self, notes) -> bool:
        self.requested = [n.message for n in notes]
        return self.result


class _Orch:
    def __init__(self, cfg, ctx, bus) -> None:
        self.config = cfg
        self.ctx = ctx
        self.bus = bus


def _make(voice_link=None):
    cfg = JarvisConfig()
    bus = EventBus()
    ctx = RuntimeContext(cfg, bus, StateStore(bus), Path("."))
    tg = FakeTelegram()
    pm = PresenceManager(_Orch(cfg, ctx, bus), telegram=tg, voice_link=voice_link)
    return ctx, tg, pm


def test_context_presence_and_peek():
    ctx, _tg, _pm = _make()
    assert ctx.seconds_since_active() is None  # nobody's interacted yet
    ctx.mark_user_active()
    assert 0 <= ctx.seconds_since_active() < 1.0
    ctx.notify("a")
    assert ctx.peek_notes() and ctx.peek_notes()  # peek does not consume
    assert len(ctx.drain_notes()) == 1 and ctx.peek_notes() == []


def test_format_single_and_multi():
    assert PresenceManager._format([PendingNote("solo")]) == "solo"
    assert PresenceManager._format([PendingNote("a"), PendingNote("b")]) == "• a\n• b"


def test_heartbeat_delivers_and_drains():
    ctx, tg, pm = _make()
    asyncio.run(pm._tick())                      # nothing queued -> nothing sent
    assert tg.sent == []
    ctx.notify("build is green")
    ctx.notify("staging is live")
    asyncio.run(pm._tick())
    assert tg.sent == ["• build is green\n• staging is live"]
    assert ctx.peek_notes() == []                # delivered notes are consumed


def test_failed_delivery_is_retained_then_retried():
    ctx, tg, pm = _make()
    tg.fail = True
    ctx.notify("important result")
    asyncio.run(pm._tick())
    assert ctx.peek_notes() and tg.sent == []    # kept, not lost
    tg.fail = False
    asyncio.run(pm._tick())
    assert ctx.peek_notes() == [] and tg.sent == ["important result"]


def test_voice_delivers_when_present_no_telegram():
    # User active recently → manager routes to the voice loop; if it reports delivered,
    # Telegram is NOT used.
    link = FakeLink(attached=True, result=True)
    ctx, tg, pm = _make(voice_link=link)
    ctx.mark_user_active()                 # recent → maybe present
    ctx.notify("the build is green")
    asyncio.run(pm._tick())
    assert link.requested == ["the build is green"]   # delivered by voice
    assert tg.sent == []                               # not duplicated to Telegram
    assert ctx.peek_notes() == []


def test_voice_silence_falls_back_to_telegram():
    # Voice loop tried but the room was silent → Telegram picks it up.
    link = FakeLink(attached=True, result=False)
    ctx, tg, pm = _make(voice_link=link)
    ctx.mark_user_active()
    ctx.notify("done, sir")
    asyncio.run(pm._tick())
    assert link.requested == ["done, sir"]   # voice was attempted
    assert tg.sent == ["done, sir"]          # then fell back
    assert ctx.peek_notes() == []


def test_voice_skipped_when_away():
    # No local activity (e.g. asked from Telegram, or long gone) → don't bother speaking to an
    # empty room; go straight to Telegram.
    link = FakeLink(attached=True, result=True)
    ctx, tg, pm = _make(voice_link=link)
    # deliberately NOT marking active → seconds_since_active() is None → away
    ctx.notify("heads up")
    asyncio.run(pm._tick())
    assert link.requested is None            # voice not attempted
    assert tg.sent == ["heads up"]


def test_presence_deliver_loop_logic():
    # The voice loop's own check: present → speak with no question; away+answered → ask then
    # speak; away+silent → return False so the manager can fall back.
    from jarvis.voice.loop import VoiceConversation

    cfg = JarvisConfig()
    bus = EventBus()
    ctx = RuntimeContext(cfg, bus, StateStore(bus), Path("."))

    class O:
        pass
    orch = O()
    orch.state = ctx.state
    orch.bus = bus
    orch.ctx = ctx
    conv = VoiceConversation(orch, None, None, None, cfg, ptt=None, voice_link=None)

    spoken = []
    answer = {"text": ""}

    async def fake_speak(s):
        spoken.append(s)

    async def fake_listen(timeout=None):
        return answer["text"]

    conv._speak = fake_speak
    conv._listen = fake_listen
    prompt = cfg.voice.presence_prompt

    # 1) present (spoke just now) → speaks the note, never asks
    spoken.clear()
    ctx.mark_user_active()
    ok = asyncio.run(conv._presence_deliver([PendingNote("file created")]))
    assert ok is True and spoken == ["file created"]

    # 2) away but answers the prompt → asks first, then speaks
    spoken.clear()
    ctx._last_active = None          # force "not fresh"
    answer["text"] = "yes I'm here"
    ok = asyncio.run(conv._presence_deliver([PendingNote("file created")]))
    assert ok is True and spoken == [prompt, "file created"]

    # 3) away and silent → asks, hears nothing, reports not-delivered (note NOT spoken)
    spoken.clear()
    ctx._last_active = None
    answer["text"] = ""
    ok = asyncio.run(conv._presence_deliver([PendingNote("file created")]))
    assert ok is False and spoken == [prompt]


def test_notes_deliver_immediately():
    # notify_user is a deliberate "worth your attention" signal, so ANY importance is
    # delivered promptly (not held for the up-to-15-min heartbeat) — this is the fix for
    # "I asked Jarvis to tell me when done and nothing reached Telegram".
    ctx, tg, pm = _make()

    async def scenario():
        await pm.start()
        try:
            for i, importance in enumerate(("normal", "high")):
                ctx.notify(f"note {i}", importance)
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if len(tg.sent) == i + 1:
                        break
                assert tg.sent[-1] == f"note {i}", (importance, tg.sent)
                assert ctx.peek_notes() == []
        finally:
            await pm.stop()

    asyncio.run(scenario())
