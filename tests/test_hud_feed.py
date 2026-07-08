"""Unit tests for the Phase-2 HUD additions: the tool-use counter and the live
"Claude Code" feed's action summaries. Pure logic — no browser, no network."""

from jarvis.brain.hooks import summarize_action
from jarvis.eventbus import EventBus
from jarvis.state import StateStore


def test_bump_tools_counts_and_publishes():
    bus = EventBus()
    q = bus.subscribe()
    store = StateStore(bus)
    assert store.status.tools_used == 0
    store.bump_tools()
    store.bump_tools()
    assert store.status.tools_used == 2
    # the count travels on the status snapshot the dashboard consumes
    assert store.status.as_dict()["tools_used"] == 2
    # and each bump published a status event
    assert not q.empty()


def test_summarize_action_uses_the_most_telling_field():
    assert summarize_action("Read", {"file_path": "C:/x/jarvis/agent.py"}) == "Read · agent.py"
    assert summarize_action("Bash", {"command": "pytest -q"}) == "Bash · pytest -q"
    assert summarize_action("Grep", {"pattern": "bus.emit"}) == "Grep · bus.emit"
    # Jarvis's own MCP tools drop the mcp__jarvis__ prefix
    assert summarize_action("mcp__jarvis__show_on_dashboard",
                            {"title": "Architecture", "kind": "mermaid"}) == "show_on_dashboard · Architecture"
    # no useful input → just the tool name
    assert summarize_action("SomeTool", {}) == "SomeTool"
    assert summarize_action("SomeTool", None) == "SomeTool"


def test_summarize_action_truncates_long_values():
    long = "x" * 200
    out = summarize_action("Bash", {"command": long})
    assert out.startswith("Bash · ") and out.endswith("…") and len(out) < 100
