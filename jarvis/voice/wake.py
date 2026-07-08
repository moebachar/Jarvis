"""Wake-word detectors behind a tiny common interface.

A detector exposes:
  * sample_rate   – required capture rate (16000)
  * frame_length  – samples per frame to feed process()
  * process(frame_int16: np.ndarray) -> bool   (True when the wake word fired)
  * reset()       – clear internal state (called when (re)arming)
  * delete()      – release resources

Default engine is **openWakeWord** ("hey jarvis"): open source, ONNX, no account or
key. **Porcupine** ("jarvis") remains available for anyone who supplies a free
Picovoice access key.
"""

from __future__ import annotations

import numpy as np


class OpenWakeWordDetector:
    sample_rate = 16000
    frame_length = 1280  # 80 ms — openWakeWord's recommended chunk

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.5) -> None:
        import openwakeword
        from openwakeword.model import Model

        def _attempt() -> "Model":
            try:
                m = Model(wakeword_models=[model_name], inference_framework="onnx")
                if getattr(m, "models", None):
                    return m
            except Exception:
                pass
            # Fall back to loading the full default set and reading our key from it.
            return Model(inference_framework="onnx")

        try:
            model = _attempt()
        except Exception:
            # Models not downloaded yet — fetch once, then retry.
            openwakeword.utils.download_models()
            model = _attempt()

        self._model = model
        self._key = next(
            (k for k in model.models if "jarvis" in k.lower()),
            next(iter(model.models)),
        )
        self._threshold = threshold

    def process(self, frame: np.ndarray) -> bool:
        scores = self._model.predict(frame)
        return float(scores.get(self._key, 0.0)) >= self._threshold

    def reset(self) -> None:
        try:
            self._model.reset()
        except Exception:
            pass

    def delete(self) -> None:
        pass


class VoskDetector:
    """Keyless single-word wake via Vosk, constrained to just the wake word.

    Runs a small offline recognizer with a grammar of {keyword, [unk]}, so it spots
    "jarvis" on its own — no account, no cloud. A touch more CPU than openWakeWord,
    but it does what the pretrained models can't: a bare single word.
    """

    sample_rate = 16000

    def __init__(
        self,
        keyword: str = "jarvis",
        frame_length: int = 1600,   # 100 ms chunks
        model_path: str | None = None,
        lang: str = "en-us",
    ) -> None:
        import json
        import vosk

        vosk.SetLogLevel(-1)  # silence kaldi chatter
        self._json = json
        self._keyword = keyword.lower().strip()
        self.frame_length = frame_length

        model = vosk.Model(model_path) if model_path else vosk.Model(lang=lang)
        grammar = json.dumps([self._keyword, "[unk]"])
        self._rec = vosk.KaldiRecognizer(model, self.sample_rate, grammar)
        self._strict = False  # set True for barge-in: only act on finalized results

    def set_strict(self, strict: bool) -> None:
        self._strict = strict

    def process(self, frame: np.ndarray) -> bool:
        data = frame.astype(np.int16).tobytes()
        if self._rec.AcceptWaveform(data):
            text = self._json.loads(self._rec.Result()).get("text", "")
        elif self._strict:
            # While Jarvis is speaking, ignore flickery partials (his own audio echo);
            # only a clean, finalized "jarvis" counts as a barge-in.
            return False
        else:
            text = self._json.loads(self._rec.PartialResult()).get("partial", "")
        if self._keyword in text.split():
            self._rec.Reset()
            return True
        return False

    def reset(self) -> None:
        try:
            self._rec.Reset()
        except Exception:
            pass

    def delete(self) -> None:
        pass


class PorcupineDetector:
    def __init__(self, *, access_key: str, keyword: str = "jarvis", sensitivity: float = 0.5) -> None:
        import pvporcupine

        self._porcupine = pvporcupine.create(
            access_key=access_key, keywords=[keyword], sensitivities=[sensitivity]
        )
        self.sample_rate = self._porcupine.sample_rate
        self.frame_length = self._porcupine.frame_length

    def process(self, frame: np.ndarray) -> bool:
        return self._porcupine.process(frame.tolist()) >= 0

    def reset(self) -> None:
        pass

    def delete(self) -> None:
        try:
            self._porcupine.delete()
        except Exception:
            pass


class NullDetector:
    """A no-op wake detector for button-only (push-to-talk) mode.

    It satisfies the small interface the Listener needs (`sample_rate`/`frame_length`
    plus no-op `process`/`reset`/`set_strict`/`delete`) but never reports the wake word,
    so the mic stream runs at a sane frame size while wake detection is effectively off.
    """

    sample_rate = 16000
    frame_length = 1600

    def process(self, frame) -> bool:
        return False

    def reset(self) -> None:
        pass

    def set_strict(self, strict: bool) -> None:
        pass

    def delete(self) -> None:
        pass


def build_detector(voice_config):
    """Construct the configured wake detector. Blocking (may download models)."""
    engine = voice_config.wake_engine
    # Button-only mode: no wake word at all (called + interrupted with push-to-talk).
    if engine == "none" or not getattr(voice_config, "wake_enabled", True):
        return NullDetector()
    if engine == "porcupine":
        return PorcupineDetector(
            access_key=voice_config.picovoice_access_key,
            keyword=voice_config.wake_word,
            sensitivity=voice_config.wake_sensitivity,
        )
    if engine == "openwakeword":
        return OpenWakeWordDetector(
            model_name=voice_config.oww_model,
            threshold=voice_config.oww_threshold,
        )
    # Default: keyless single-word "jarvis".
    return VoskDetector(
        keyword=voice_config.wake_word,
        model_path=voice_config.vosk_model_path,
    )
