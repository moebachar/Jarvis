"""Tests for the packaging helpers: project scaffolding, the dashboard-forward hint, and the
per-machine profile (GPU auto-apply + profile resolution/writing)."""

import tempfile
import tomllib
from pathlib import Path

from jarvis import machine as machine_mod
from jarvis.cli import _dashboard_forward_note
from jarvis.config import JarvisConfig, _apply_machine_gpu
from jarvis.scaffold import init_project


def test_init_project_creates_starter_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        created, skipped = init_project(proj)
        names = {p.name for p in created}
        assert names == {"config.toml", ".env.example", ".gitignore"}
        assert skipped == []
        # the files really exist under .jarvis/ with content
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
        # the existing config is kept verbatim; only the missing files are created
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


def test_apply_machine_gpu_fills_unset_voice_devices():
    merged = {"machine": {"gpu": True}, "voice": {"whisper_device": "cpu", "kokoro_device": "auto"}}
    _apply_machine_gpu(merged, explicit_voice=set())  # nothing set by hand
    # Kokoro + XTTS go to CUDA (they degrade gracefully); Whisper is left on CPU by design.
    assert merged["voice"]["kokoro_device"] == "cuda"
    assert merged["voice"]["xtts_device"] == "cuda"
    assert merged["voice"]["whisper_device"] == "cpu"
    assert "whisper_compute_type" not in merged["voice"]


def test_apply_machine_gpu_respects_explicit_and_no_gpu():
    # An explicit [voice] whisper_device the user typed must survive the GPU pass.
    merged = {"machine": {"gpu": True}, "voice": {"whisper_device": "cpu"}}
    _apply_machine_gpu(merged, explicit_voice={"whisper_device"})
    assert merged["voice"]["whisper_device"] == "cpu"      # user's choice kept
    assert merged["voice"]["kokoro_device"] == "cuda"      # the un-set ones still upgraded

    # gpu = false → no changes at all.
    m2 = {"machine": {"gpu": False}, "voice": {"whisper_device": "cpu"}}
    _apply_machine_gpu(m2, explicit_voice=set())
    assert m2["voice"] == {"whisper_device": "cpu"}


def test_write_machine_profile_roundtrips(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "machine.toml"
        # point the writer at a temp file instead of the real ~/.jarvis
        orig = machine_mod.machine_toml_path
        machine_mod.machine_toml_path = lambda: target
        try:
            path = machine_mod.write_machine_profile(gpu=True, voice_clone=True, cuda="cu124")
        finally:
            machine_mod.machine_toml_path = orig
        assert path == target and target.is_file()
        prof = tomllib.loads(target.read_text(encoding="utf-8"))["machine"]
        assert prof["gpu"] is True and prof["voice_clone"] is True
        assert prof["cuda"] == "cu124" and prof["extras"] == "all"


def test_resolve_profile_precedence():
    # forced env flag beats everything; else the existing profile; else auto-detect.
    orig_read, orig_detect = machine_mod._read_profile, machine_mod.detect_gpu
    try:
        machine_mod._read_profile = lambda: {"gpu": False}
        machine_mod.detect_gpu = lambda: True  # would say GPU, but the profile said False…
        r = machine_mod.resolve_profile({})    # …and no forced flag → profile wins
        assert r["GPU"] is False

        r2 = machine_mod.resolve_profile({"JARVIS_FORCE_GPU": "1"})  # forced flag wins
        assert r2["GPU"] is True

        machine_mod._read_profile = lambda: {}  # no profile → fall back to auto-detect
        assert machine_mod.resolve_profile({})["GPU"] is True
    finally:
        machine_mod._read_profile, machine_mod.detect_gpu = orig_read, orig_detect
