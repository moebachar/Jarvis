"""Speech-to-text via faster-whisper (local, no cloud, private)."""

from __future__ import annotations

import numpy as np
from faster_whisper import WhisperModel


class Transcriber:
    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        download_root: str | None = None,
    ) -> None:
        self._beam_size = beam_size
        # First construction downloads the model (~150 MB for base.en) and caches it.
        self._model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
        )

    def device_report(self) -> str:
        """The device faster-whisper actually landed on ('cuda' / 'cpu'), for a startup log.

        CTranslate2 raises rather than silently falling back when 'cuda' is requested without a
        working GPU, so this mostly *confirms* the GPU is in use — but reading it back removes all
        doubt about where inference runs.
        """
        try:
            return str(getattr(getattr(self._model, "model", None), "device", "unknown"))
        except Exception:  # never let a diagnostic break startup
            return "unknown"

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz array to text (English)."""
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=self._beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return "".join(segment.text for segment in segments).strip()
