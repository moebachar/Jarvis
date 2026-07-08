"""Tests for the packaging helpers: project scaffolding + the dashboard-forward hint."""

import tempfile
from pathlib import Path

from jarvis.cli import _dashboard_forward_note
from jarvis.config import JarvisConfig
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
