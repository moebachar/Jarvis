"""`jarvis --check-audio` — confirm the mic/speakers work before a live voice test.

Needs no API keys. Lists devices, runs a live mic level meter (speak and watch it
move), and plays a short tone so you can confirm output. This is the fastest way to
diagnose the "wake word doesn't hear me" class of problems (muted mic, wrong default
device, or Windows desktop-app microphone permission turned off).
"""

from __future__ import annotations

import time

import numpy as np
import sounddevice as sd

from ..config import JarvisConfig

SR = 16000
FL = 512
DIM = "\033[2m"
RESET = "\033[0m"
GOLD = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"


def _list_devices() -> None:
    print(f"{GOLD}Audio devices{RESET} (default input/output = {sd.default.device}):")
    for i, d in enumerate(sd.query_devices()):
        tags = []
        if d["max_input_channels"] > 0:
            tags.append("IN")
        if d["max_output_channels"] > 0:
            tags.append("OUT")
        marker = " <- default in" if i == sd.default.device[0] else (
            " <- default out" if i == sd.default.device[1] else ""
        )
        print(f"   [{i:2}] {d['name'][:42]:42} {'/'.join(tags):7}{marker}")


def _mic_meter(input_device, seconds: float = 6.0) -> float:
    print(f"\n{GOLD}Mic level{RESET} — speak now for {int(seconds)}s "
          f"(say 'hey jarvis, can you hear me?'):")
    peak = 0.0
    try:
        with sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                            blocksize=FL, device=input_device) as stream:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                data, _ = stream.read(FL)
                frame = np.asarray(data).reshape(-1).astype(np.float64)
                rms = float(np.sqrt(np.mean(frame**2))) if frame.size else 0.0
                peak = max(peak, rms)
                bars = min(40, int(rms / 50))
                print("\r   [" + "#" * bars + " " * (40 - bars) + f"] {rms:6.0f}",
                      end="", flush=True)
    except Exception as exc:
        print(f"\n   {RED}mic stream error: {exc!r}{RESET}")
        return -1.0
    print()
    return peak


def _play_test_tone(output_device, volume: float) -> None:
    print(f"\n{GOLD}Speaker test{RESET} — this is the wake-word activation chime you'll hear...")
    try:
        from .tts import play_activation_chime
        play_activation_chime(SR, volume, output_device)
        print("   (if you heard the chime, output works)")
    except Exception as exc:
        print(f"   {RED}output error: {exc!r}{RESET}")


def check_voice(config: JarvisConfig) -> None:
    """Preview the configured TTS voice by actually speaking a test phrase.

    Kokoro (local) needs no key; ElevenLabs (cloud) validates the key + voice id.
    """
    v = config.voice

    if getattr(v, "tts_engine", "elevenlabs") == "kokoro":
        print(f"{GOLD}Engine: Kokoro (local, free — no key). Voice: {v.kokoro_voice}."
              f"\nSynthesizing a test phrase (first run downloads the model)...{RESET}")
        try:
            from .tts import render_utterance
            arr = render_utterance(
                v, "All systems online, sir. Standing by.",
                sample_rate=v.kokoro_sample_rate,
            )
            if arr.size == 0:
                print(f"{RED}FAIL: no audio returned.{RESET}")
                return
            sd.play(arr, samplerate=v.kokoro_sample_rate, device=v.output_device)
            sd.wait()
            print(f"{GREEN}PASS — you should have just heard Jarvis (Kokoro, local).{RESET}")
        except Exception as e:
            print(f"{RED}FAIL:{RESET} {str(e)[:220]}")
        return

    if not v.elevenlabs_api_key or not v.elevenlabs_voice_id:
        print(f"{RED}Missing ELEVENLABS_API_KEY or JARVIS_ELEVENLABS_VOICE_ID.{RESET}")
        return

    vid = v.elevenlabs_voice_id
    print(f"voice id: {len(vid)} chars, starts {vid[:4]!r}")
    if len(vid) != 20:
        print(f"   {RED}note:{RESET} ElevenLabs voice ids are ~20 chars; yours is {len(vid)}. "
              f"On the website open the voice and use 'Copy Voice ID' (no label/spaces).")

    from elevenlabs.client import ElevenLabs
    client = ElevenLabs(api_key=v.elevenlabs_api_key)

    # Optional: list voices (needs voices_read permission on the key).
    try:
        resp = client.voices.get_all()
        voices = getattr(resp, "voices", []) or []
        print(f"\n{GREEN}API key can read voices.{RESET} {len(voices)} in your library:")
        for vo in voices[:12]:
            print(f"   {getattr(vo, 'name', '?'):20} id={getattr(vo, 'voice_id', '?')} "
                  f"({getattr(vo, 'category', '?')})")
    except Exception as e:
        code = getattr(e, "status_code", None)
        print(f"\n{DIM}(can't list voices: {code} — key likely lacks 'voices_read'. "
              f"That's fine for speaking; you just won't see ids here.){RESET}")

    # The real test: synthesize + play a short phrase, using the tuned delivery.
    print(f"\n{GOLD}Synthesizing a test phrase...{RESET}")
    try:
        from .tts import build_voice_settings
        stream = client.text_to_speech.convert(
            vid, text="All systems online, sir. Standing by.",
            model_id=v.elevenlabs_model_id, output_format="pcm_16000",
            voice_settings=build_voice_settings(v),
        )
        data = b"".join(stream)
        if not data:
            print(f"{RED}FAIL: no audio returned.{RESET}")
            return
        arr = np.frombuffer(data, dtype=np.int16)
        sd.play(arr, samplerate=16000, device=v.output_device)
        sd.wait()
        print(f"{GREEN}PASS — you should have just heard the voice. TTS is good to go.{RESET}")
    except Exception as e:
        code = getattr(e, "status_code", None)
        body = getattr(e, "body", None)
        print(f"{RED}FAIL ({code}):{RESET} {str(body)[:220]}")
        print(f"   Fixes: ensure the API key has the {GOLD}Text to Speech{RESET} permission, "
              f"and that the voice id is the bare ~20-char id.")


def check_audio(config: JarvisConfig) -> None:
    v = config.voice
    _list_devices()
    peak = _mic_meter(v.input_device)
    _play_test_tone(v.output_device, v.ack_volume)

    print()
    if peak < 0:
        print(f"{RED}Could not read the microphone.{RESET} Check that a mic is connected and "
              f"that desktop apps are allowed to use it (Windows mic privacy settings).")
    elif peak < 60:
        print(f"{RED}The mic looks silent (peak ~{peak:.0f}).{RESET} Likely muted, not the "
              f"default device, or blocked by Windows mic permission. Fix that, then the wake "
              f"word will work.")
    else:
        print(f"{GREEN}Mic is live (peak ~{peak:.0f}).{RESET} Wake-word detection should work. "
              f"Add your voice keys and run `jarvis --voice`.")
