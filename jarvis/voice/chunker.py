"""Turn a stream of text chunks into speakable sentences.

The brain streams text in arbitrary pieces. To keep latency low we want to start
speaking as soon as the first sentence is complete rather than waiting for the whole
reply. This buffers incoming text and emits complete sentences as they form, with a
soft length cap so a very long run-on still gets spoken in chunks.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[\.!\?…:;]+[\)\]\"'”’]*\s")
_SOFT_CAP = 220  # flush at a word boundary if a sentence runs longer than this


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""

    def add(self, text: str) -> list[str]:
        """Add streamed text; return any complete sentences now ready to speak."""
        self._buf += text
        out: list[str] = []

        while True:
            match = _SENTENCE_END.search(self._buf)
            if match:
                cut = match.end()
                sentence = self._buf[:cut].strip()
                self._buf = self._buf[cut:]
                if sentence:
                    out.append(sentence)
                continue

            if len(self._buf) > _SOFT_CAP:
                # No sentence end in sight; flush at the last space to keep speech moving.
                space = self._buf.rfind(" ", 0, _SOFT_CAP)
                if space <= 0:
                    break
                sentence = self._buf[:space].strip()
                self._buf = self._buf[space:]
                if sentence:
                    out.append(sentence)
                continue
            break

        return out

    def flush(self) -> str:
        """Return whatever text remains (the trailing partial sentence)."""
        tail, self._buf = self._buf.strip(), ""
        return tail
