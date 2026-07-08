"""Unit tests for the remote/tunneled voice transport (jarvis/voice/remote.py).

Hardware-free: a fake WebSocket client records the messages the hub sends, and a fake engine
stands in for Kokoro/ElevenLabs. We verify the duck-typed transports behave as the voice loop
expects and that the critical wire invariants hold (ordering, mic gating, pre-roll, no-op with
no client).
"""

import asyncio

import numpy as np

from jarvis.voice.remote import (
    BrowserListener,
    BrowserPTT,
    BrowserSpeaker,
    RemoteAudioHub,
    build_browser_transport,
)


class FakeClient:
    """Records everything the hub sends, in order, as ('json', obj) / ('bytes', data)."""

    def __init__(self):
        self.msgs = []

    async def send_json(self, obj):
        self.msgs.append(("json", obj))

    async def send_bytes(self, data):
        self.msgs.append(("bytes", bytes(data)))


class FakeEngine:
    """Stands in for a Kokoro/ElevenLabs _PcmSpeaker: yields a fixed PCM blob per call."""

    _sample_rate = 24000

    def __init__(self, samples=6000):
        self.calls = 0
        self._pcm = np.zeros(samples, dtype=np.int16).tobytes()

    def _pcm_chunks(self, text):
        self.calls += 1
        yield self._pcm


class FakeCtx:
    def __init__(self, loop):
        self.loop = loop


def _types(msgs):
    return [m[1].get("type") if m[0] == "json" else "pcm" for m in msgs]


def test_ptt_press_release_and_capture():
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        ptt = BrowserPTT(hub); ptt.start(loop)
        listener = BrowserListener(hub); listener.start(loop)
        client = FakeClient()

        wp = asyncio.ensure_future(ptt.wait_press())
        hub.on_control(client, {"type": "ptt_press"})   # claims client, arms listener, presses
        await asyncio.wait_for(wp, 1.0)
        assert ptt.is_down is True

        # feed two 1600-sample frames from the active client...
        frame = (np.ones(1600, dtype=np.int16) * 1000).tobytes()
        hub.on_binary(client, frame)
        hub.on_binary(client, frame)
        # ...and one from a DIFFERENT socket, which must be ignored (per-client mic gate).
        hub.on_binary(object(), (np.ones(1600, dtype=np.int16) * 9000).tobytes())

        wr = asyncio.ensure_future(ptt.wait_release())
        hub.on_control(client, {"type": "ptt_release"})
        await asyncio.wait_for(wr, 1.0)
        assert ptt.is_down is False

        audio = listener.stop_ptt()
        assert audio is not None
        assert audio.dtype == np.float32
        assert len(audio) == 3200                     # only the 2 client frames, not the stray one
        assert float(np.max(np.abs(audio))) <= 1.0
        hub.stop()

    asyncio.run(main())


def test_start_ptt_is_idempotent_preroll():
    # The hub arms start_ptt on the press; the loop calls it again later — the pre-roll frames
    # captured in between must survive (not be cleared by the second start_ptt).
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        listener = BrowserListener(hub); listener.start(loop)
        ptt = BrowserPTT(hub); ptt.start(loop)
        client = FakeClient()

        hub.on_control(client, {"type": "ptt_press"})     # arms + presses
        listener.feed_pcm((np.ones(1600, dtype=np.int16) * 1000).tobytes())  # pre-roll
        listener.start_ptt()                              # loop's later call — must NOT clear
        listener.feed_pcm((np.ones(1600, dtype=np.int16) * 1000).tobytes())
        audio = listener.stop_ptt()
        assert audio is not None and len(audio) == 3200   # both frames kept
        hub.stop()

    asyncio.run(main())


def test_speaker_streams_start_pcm_end_in_order():
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        ptt = BrowserPTT(hub); ptt.start(loop)            # starts the drain task
        client = FakeClient()
        hub.on_control(client, {"type": "audio_hello", "rate": 16000})  # claim slot, no press

        engine = FakeEngine(samples=6000)                  # 12000 bytes > slice size → many slices
        speaker = BrowserSpeaker(engine, hub)
        await asyncio.to_thread(speaker.speak, "hello there")  # blocking synth off-thread
        await asyncio.sleep(0.1)                            # let the drain flush the queue

        types = _types(client.msgs)
        assert types[0] == "tts_start"
        assert types[-1] == "tts_end"
        assert "pcm" in types
        # every PCM frame sits strictly between the start and the end (no reordering)
        assert types.index("tts_start") < types.index("pcm") < types.index("tts_end")
        assert client.msgs[0][1]["data"]["rate"] == 24000
        assert engine.calls == 1
        hub.stop()

    asyncio.run(main())


def test_speaker_noop_without_client():
    # A reply with no browser attached must not run the (GPU) synth into the void.
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        BrowserPTT(hub).start(loop)
        engine = FakeEngine()
        speaker = BrowserSpeaker(engine, hub)
        await asyncio.to_thread(speaker.speak, "nobody is listening")
        await asyncio.sleep(0.02)
        assert engine.calls == 0
        hub.stop()

    asyncio.run(main())


def test_barge_in_flush_emits_tts_flush():
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        speaker_hub_ptt = BrowserPTT(hub); speaker_hub_ptt.start(loop)
        client = FakeClient()
        hub.on_control(client, {"type": "audio_hello"})
        speaker = BrowserSpeaker(FakeEngine(), hub)
        speaker.stop()                    # barge-in: purge + flush (runs on the loop)
        await asyncio.sleep(0.05)
        assert ("json", {"type": "tts_flush", "data": {}}) in client.msgs
        hub.stop()

    asyncio.run(main())


def test_disconnect_discards_partial_capture():
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        listener = BrowserListener(hub); listener.start(loop)
        ptt = BrowserPTT(hub); ptt.start(loop)
        client = FakeClient()

        hub.on_control(client, {"type": "ptt_press"})
        listener.feed_pcm((np.ones(1600, dtype=np.int16) * 1000).tobytes())
        hub.on_disconnect(client)                     # tab drops mid-turn
        assert ptt.is_down is False                   # release was forced
        assert listener.stop_ptt() is None            # partial buffer discarded (no phantom turn)
        hub.stop()

    asyncio.run(main())


def test_barge_drops_straggler_frames_by_generation():
    # After a barge-in flush, straggler PCM/end from the cancelled utterance (which the still-
    # running synth worker enqueues AFTER the flush) must be dropped; the next utterance passes.
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        BrowserPTT(hub).start(loop)
        client = FakeClient()
        hub.on_control(client, {"type": "audio_hello"})

        gen1 = hub.tts_start(24000)
        hub.send_tts(gen1, b"\x0a\x0a")
        hub.flush_tts()                       # barge: cancels gen1
        hub.send_tts(gen1, b"\x0b\x0b")       # straggler slice from gen1's worker
        hub.tts_end(gen1)                     # straggler end from gen1
        gen2 = hub.tts_start(16000)           # a fresh utterance after the barge
        hub.send_tts(gen2, b"\x0c\x0c")
        hub.tts_end(gen2)
        await asyncio.sleep(0.05)

        sent_bytes = [m[1] for m in client.msgs if m[0] == "bytes"]
        assert b"\x0b\x0b" not in sent_bytes                       # gen1 straggler dropped
        assert b"\x0c\x0c" in sent_bytes                           # gen2 delivered
        assert any(m[0] == "json" and m[1].get("type") == "tts_flush" for m in client.msgs)
        # the only tts_end that reaches the client belongs to gen2 (gen1's was dropped)
        ends = [m for m in client.msgs if m[0] == "json" and m[1].get("type") == "tts_end"]
        assert len(ends) == 1
        hub.stop()

    asyncio.run(main())


def test_browser_speaker_can_speak_tracks_client():
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        BrowserPTT(hub).start(loop)
        speaker = BrowserSpeaker(FakeEngine(), hub)
        assert speaker.can_speak is False                # no tab attached
        client = FakeClient()
        hub.on_control(client, {"type": "audio_hello"})
        assert speaker.can_speak is True
        hub.on_disconnect(client)
        assert speaker.can_speak is False                # tab gone → route notes to Telegram
        hub.stop()

    asyncio.run(main())


def test_build_browser_transport_shapes():
    async def main():
        loop = asyncio.get_running_loop()
        hub = RemoteAudioHub(FakeCtx(loop))
        listener, speaker, ptt = build_browser_transport(
            hub, object(), engine=FakeEngine(), frame_length=1600
        )
        assert type(listener).__name__ == "BrowserListener"
        assert type(speaker).__name__ == "BrowserSpeaker"
        assert type(ptt).__name__ == "BrowserPTT"
        assert speaker._sample_rate == 24000          # inherited from the engine
        assert listener.frame_length == 1600

    asyncio.run(main())
