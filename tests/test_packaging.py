"""Tests for the packaging helpers: project scaffolding, the dashboard-forward hint, and the
single-layer config loader (project config + env overrides only)."""

import os
import tempfile
from pathlib import Path

from jarvis.cli import _dashboard_forward_note
from jarvis.config import JarvisConfig, load_config
from jarvis.scaffold import init_project


def test_init_project_creates_starter_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        created, skipped = init_project(proj)
        names = {p.name for p in created}
        assert names == {"config.toml", ".env.example", ".gitignore"}
        assert skipped == []
        cfg = proj / ".jarvis" / "config.toml"
        assert cfg.is_file()
        text = cfg.read_text(encoding="utf-8")
        assert "tts_engine" in text and "[dashboard]" in text

        # a second run must NOT overwrite — everything is now "skipped"
        created2, skipped2 = init_project(proj)
        assert created2 == []
        assert {p.name for p in skipped2} == {"config.toml", ".env.example", ".gitignore"}


def test_init_project_preserves_existing_config():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        jdir = proj / ".jarvis"
        jdir.mkdir()
        (jdir / "config.toml").write_text("# mine\n", encoding="utf-8")
        created, skipped = init_project(proj)
        assert (jdir / "config.toml").read_text(encoding="utf-8") == "# mine\n"
        assert {p.name for p in created} == {".env.example", ".gitignore"}
        assert {p.name for p in skipped} == {"config.toml"}


def test_dashboard_forward_note_loopback_gives_ssh_hint():
    c = JarvisConfig()
    c.dashboard.enabled = True
    c.dashboard.host = "127.0.0.1"
    c.dashboard.port = 8765
    note = _dashboard_forward_note(c)
    assert note is not None
    assert "ssh -L 8765:localhost:8765" in note
    assert "http://localhost:8765/" in note


def test_dashboard_forward_note_lan_warns_and_disabled_is_none():
    c = JarvisConfig()
    c.dashboard.enabled = True
    c.dashboard.host = "0.0.0.0"
    c.dashboard.port = 9000
    note = _dashboard_forward_note(c)
    assert note is not None and "NO" in note and "auth" in note  # security warning

    c.dashboard.enabled = False
    assert _dashboard_forward_note(c) is None


def test_defaults_are_keyless_and_gpu_auto():
    # A fresh install must work with NO keys and NO per-machine setup: local voice, GPU-auto.
    v = JarvisConfig().voice
    assert v.tts_engine == "kokoro"          # local/free, no key
    assert v.whisper_device == "auto"        # GPU when present, else CPU (never per-machine config)
    assert v.kokoro_device == "auto"
    assert v.xtts_device == "auto"


def test_load_config_single_layer_project_and_env():
    # ONE config layer: the project's config.toml, then env overrides for secrets/Docker knobs.
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        (proj / ".jarvis").mkdir()
        (proj / ".jarvis" / "config.toml").write_text(
            '[voice]\ntts_engine = "elevenlabs"\n[dashboard]\nport = 8765\n', encoding="utf-8"
        )
        cfg = load_config(proj)
        assert cfg.voice.tts_engine == "elevenlabs"   # from the project file

        # env override wins (and the Docker host/port knobs coerce correctly)
        os.environ["JARVIS_TTS_ENGINE"] = "kokoro"
        os.environ["JARVIS_DASHBOARD_HOST"] = "0.0.0.0"
        os.environ["JARVIS_DASHBOARD_PORT"] = "9999"
        try:
            cfg2 = load_config(proj)
            assert cfg2.voice.tts_engine == "kokoro"
            assert cfg2.dashboard.host == "0.0.0.0"
            assert cfg2.dashboard.port == 9999            # coerced to int
        finally:
            for k in ("JARVIS_TTS_ENGINE", "JARVIS_DASHBOARD_HOST", "JARVIS_DASHBOARD_PORT"):
                os.environ.pop(k, None)
