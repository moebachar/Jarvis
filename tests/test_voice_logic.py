"""Unit tests for the hardware-free voice logic: energy VAD and sentence chunker."""

import numpy as np

from jarvis.config import JarvisConfig
from jarvis.voice import tts as tts_mod
from jarvis.voice.chunker import SentenceChunker
from jarvis.voice.tts import describe_tts_error
from jarvis.voice.vad import EnergyVAD, frame_rms

SR = 16000
FL = 512


def _frame(amplitude: float) -> np.ndarray:
    """A frame of white-ish noise at a given int16 amplitude."""
    rng = np.random.default_rng(0)
    return (rng.standard_normal(FL) * amplitude).astype(np.int16)


def test_frame_rms_silence_vs_speech():
    assert frame_rms(_frame(5)) < 50
    assert frame_rms(_frame(3000)) > 1000


def test_vad_detects_speech_then_silence():
    vad = EnergyVAD(SR, FL, threshold=500, silence_timeout=0.3,
                    min_speech_seconds=0.1, start_active_seconds=0.15)
    audio = None
    finished = False
    for _ in range(int(0.4 * SR / FL)):           # ~0.4s of "speech"
        finished, audio = vad.push(_frame(4000))
        assert not finished
    for _ in range(int(0.5 * SR / FL)):           # then silence ends it
        finished, audio = vad.push(_frame(5))
        if finished:
            break
    assert finished and audio is not None
    assert audio.dtype == np.float32 and np.max(np.abs(audio)) <= 1.0


def test_vad_brief_noise_does_not_start():
    # A short blip (click / pop) must not start a capture that then ends on its own.
    vad = EnergyVAD(SR, FL, threshold=500, silence_timeout=0.3, min_speech_seconds=0.3,
                    max_utterance_seconds=0.6, start_active_seconds=0.3)
    for _ in range(2):                             # 2-frame noise blip
        finished, _ = vad.push(_frame(4000))
        assert not finished
    for _ in range(int(0.25 * SR / FL)):           # must not have started speech
        finished, _ = vad.push(_frame(5))
        assert not finished
    for _ in range(int(0.6 * SR / FL) + 5):        # ends by max length, capturing nothing
        finished, audio = vad.push(_frame(5))
        if finished:
            break
    assert finished and audio is None


def test_vad_hysteresis_keeps_quiet_speech():
    # Once speaking, quiet/far syllables (above the lower threshold) must not be read
    # as silence and cut the utterance off early.
    vad = EnergyVAD(SR, FL, threshold=1000, silence_timeout=0.3, min_speech_seconds=0.1,
                    start_active_seconds=0.1, end_threshold_ratio=0.4)
    for _ in range(int(0.2 * SR / FL)):            # loud onset
        vad.push(_frame(3000))
    for _ in range(int(0.5 * SR / FL)):            # quiet voiced (> end threshold) must persist
        finished, _ = vad.push(_frame(600))
        assert not finished
    finished = False
    for _ in range(int(0.4 * SR / FL)):            # real silence finally ends it
        finished, audio = vad.push(_frame(5))
        if finished:
            break
    assert finished and audio is not None


def test_vad_start_guard_skips_initial_frames():
    vad = EnergyVAD(SR, FL, threshold=500, silence_timeout=0.3, min_speech_seconds=0.1,
                    start_guard_seconds=0.2, start_active_seconds=0.1)
    for _ in range(int(0.2 * SR / FL)):            # loud "echo" during guard
        finished, _ = vad.push(_frame(8000))
        assert not finished
    assert vad._buffer == []                       # nothing buffered during guard
    for _ in range(int(0.4 * SR / FL)):
        vad.push(_frame(4000))
    finished, audio = (False, None)
    for _ in range(int(0.5 * SR / FL)):
        finished, audio = vad.push(_frame(5))
        if finished:
            break
    assert finished and audio is not None


def test_vad_onset_timeout_ends_an_empty_turn():
    # With an onset timeout (the follow-up window), pure silence ends quickly with no audio.
    vad = EnergyVAD(SR, FL, threshold=500, silence_timeout=0.7,
                    max_utterance_seconds=15.0, onset_timeout=0.5)
    finished, audio = (False, None)
    frames = 0
    for _ in range(int(2.0 * SR / FL)):
        finished, audio = vad.push(_frame(5))
        frames += 1
        if finished:
            break
    assert finished and audio is None
    assert frames <= int(0.7 * SR / FL)  # ended around the 0.5s onset window, not 15s


def test_vad_onset_timeout_never_cuts_a_started_utterance():
    # The bug fix: once speech starts, the onset timeout must NOT chop it mid-sentence.
    vad = EnergyVAD(SR, FL, threshold=500, silence_timeout=0.5, min_speech_seconds=0.1,
                    max_utterance_seconds=15.0, start_active_seconds=0.2, onset_timeout=0.5)
    # speak for ~2s — well past the 0.5s onset window
    for _ in range(int(2.0 * SR / FL)):
        finished, _ = vad.push(_frame(4000))
        assert not finished, "a turn in progress must never end on the onset timeout"
    finished, audio = (False, None)
    for _ in range(int(0.7 * SR / FL)):
        finished, audio = vad.push(_frame(5))
        if finished:
            break
    assert finished and audio is not None
    assert len(audio) / SR > 1.5  # captured the whole utterance, not a 0.5s fragment


def test_chunker_emits_complete_sentences():
    c = SentenceChunker()
    assert c.add("Right away, sir. ") == ["Right away, sir."]
    out = c.add("The build is green")
    assert out == []  # no terminator yet
    assert c.add("! Anything else? ") == ["The build is green!", "Anything else?"]
    assert c.flush() == ""


def test_chunker_soft_cap_flushes_runons():
    c = SentenceChunker()
    long_text = "word " * 60  # 300 chars, no sentence end
    out = c.add(long_text)
    assert out, "a very long run-on should be flushed at a word boundary"
    assert all(" " in s or s for s in out)


def test_chunker_flush_returns_tail():
    c = SentenceChunker()
    c.add("A complete one. ")
    assert c.add("a trailing partial") == []
    assert c.flush() == "a trailing partial"


class _ApiError(Exception):
    """Stand-in for elevenlabs.core.ApiError: str() is the noisy headers+body dump."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"headers: {{...}}, status_code: {status_code}, body: {body}")


def test_describe_tts_error_quota_is_clean_and_flagged():
    # The real shape ElevenLabs returned when credits ran out (from the live log).
    exc = _ApiError(401, {"detail": {
        "type": "invalid_request", "code": "quota_exceeded",
        "message": "This request exceeds your quota of 10000. You have 0 credits remaining, "
                   "while 13 credits are required for this request.",
        "status": "quota_exceeded", "request_id": "fc7a0350"}})
    message, is_quota = describe_tts_error(exc)
    assert is_quota is True
    assert "credit" in message.lower()
    assert "headers" not in message and "status_code" not in message  # no HTTP noise
    assert len(message) < 120


def test_describe_tts_error_other_is_short_not_quota():
    exc = _ApiError(500, {"detail": {"message": "Internal server error", "status": "error"}})
    message, is_quota = describe_tts_error(exc)
    assert is_quota is False
    assert "Internal server error" in message
    assert "headers" not in message


def test_describe_tts_error_unstructured_is_truncated():
    message, is_quota = describe_tts_error(RuntimeError("boom\nsecond line " + "x" * 400))
    assert is_quota is False
    assert "\n" not in message and len(message) <= 180


def test_build_speaker_selects_engine():
    # Kokoro: keyless, local; must NOT load the model just to construct (lazy).
    c = JarvisConfig().voice
    c.tts_engine = "kokoro"
    spk = tts_mod.build_speaker(c)
    assert type(spk).__name__ == "KokoroSpeaker"
    assert spk._sample_rate == c.kokoro_sample_rate == 24000
    assert spk._kokoro is None  # not loaded yet

    c2 = JarvisConfig().voice
    c2.tts_engine = "elevenlabs"
    c2.elevenlabs_api_key = "k"
    c2.elevenlabs_voice_id = "v"
    spk2 = tts_mod.build_speaker(c2)
    assert type(spk2).__name__ == "ElevenLabsSpeaker"
    assert spk2._sample_rate == 16000
    # Back-compat: the old import name still points at the cloud engine.
    assert tts_mod.Speaker is tts_mod.ElevenLabsSpeaker


def test_kokoro_paths_default_to_cache():
    c = JarvisConfig().voice
    model, voices = tts_mod._kokoro_paths(c)
    assert model.endswith("kokoro-v1.0.onnx")
    assert voices.endswith("voices-v1.0.bin")
    assert "kokoro" in model.replace("\\", "/")


def test_build_speaker_selects_xtts_lazily():
    # The voice-clone engine must construct WITHOUT importing torch or loading the ~1.8 GB
    # model — the model (and reference-clip check) is deferred to the first spoken line.
    c = JarvisConfig().voice
    c.tts_engine = "xtts"
    spk = tts_mod.build_speaker(c)
    assert type(spk).__name__ == "XttsSpeaker"
    assert spk._sample_rate == c.xtts_sample_rate == 24000
    assert spk._bundle is None  # not loaded yet


def test_float_to_pcm_numpy_and_torch_like():
    # A float waveform in -1..1 -> int16 PCM, clipped. Works for numpy AND torch-like tensors.
    arr = np.array([0.0, 1.0, -1.0, 2.0, 0.5], dtype=np.float32)  # 2.0 must clip to +full-scale
    pcm = np.frombuffer(tts_mod._float_to_pcm(arr), dtype=np.int16)
    assert pcm.tolist() == [0, 32767, -32767, 32767, 16383]

    class _FakeTensor:
        """Mimics the torch tensor XTTS streams: .detach().to('cpu').numpy() -> ndarray."""
        def __init__(self, a):
            self._a = a
        def detach(self):
            return self
        def to(self, _dev):
            return self
        def numpy(self):
            return self._a

    pcm2 = np.frombuffer(tts_mod._float_to_pcm(_FakeTensor(arr)), dtype=np.int16)
    assert pcm2.tolist() == pcm.tolist()


def test_load_xtts_requires_a_reference_clip():
    c = JarvisConfig().voice
    c.tts_engine = "xtts"
    c.xtts_reference = None
    try:
        tts_mod.load_xtts(c)
        assert False, "expected a RuntimeError when no reference clip is set"
    except RuntimeError as exc:
        assert "xtts_reference" in str(exc)

    c.xtts_reference = "C:/does/not/exist/nope.wav"
    try:
        tts_mod.load_xtts(c)
        assert False, "expected a RuntimeError when the reference clip is missing"
    except RuntimeError as exc:
        assert "not found" in str(exc)
