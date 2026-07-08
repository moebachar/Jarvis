"""Unit tests for the Phase 4 Canvas board model (server side).

No browser, no uvicorn, no network: we drive DashboardServer's board model and its
inbound-message handler directly. The handler is async but does no real I/O when there
are no connected clients, so asyncio.run() is enough (no pytest-asyncio).
"""

import asyncio
import json

from jarvis.config import JarvisConfig
from jarvis.context import RuntimeContext
from jarvis.eventbus import EventBus
from jarvis.dashboard.server import DashboardServer
from jarvis.state import StateStore


class FakeWS:
    """Stands in for a connected browser; never actually in the client set during tests."""


def _server(tmp_path):
    bus = EventBus()
    ctx = RuntimeContext(JarvisConfig(), bus, StateStore(bus), tmp_path)
    return DashboardServer(ctx, "127.0.0.1", 8999), ctx


def _send(server, msg):
    asyncio.run(server._handle_client_msg(FakeWS(), json.dumps(msg)))


def test_add_item_creates_unplaced_item_with_geometry(tmp_path):
    server, _ = _server(tmp_path)
    item = server._add_item("mermaid", "Arch", "graph TD; A-->B")
    assert item["id"] == "c1"
    assert item["kind"] == "mermaid" and item["title"] == "Arch"
    assert item["placed"] is False
    assert item["w"] > 0 and item["h"] > 0
    assert server._board["order"] == ["c1"]
    payload = server._board_payload()
    assert [i["id"] for i in payload["items"]] == ["c1"]


def test_update_marks_placed_and_moves(tmp_path):
    server, _ = _server(tmp_path)
    server._add_item("markdown", "Notes", "# hi")
    _send(server, {"type": "canvas_update", "id": "c1", "x": 300, "y": 200, "w": 410, "h": 260})
    it = server._board["items"]["c1"]
    assert (it["x"], it["y"], it["w"], it["h"]) == (300, 200, 410, 260)
    assert it["placed"] is True


def test_focus_sets_context_hint_and_clears(tmp_path):
    server, ctx = _server(tmp_path)
    server._add_item("stats", "At a glance", json.dumps([{"label": "x", "value": "1"}]))
    _send(server, {"type": "canvas_focus", "id": "c1"})
    assert ctx.focused_canvas is not None and ctx.focused_canvas["id"] == "c1"
    hint = ctx.canvas_focus_hint()
    assert hint and "At a glance" in hint and "stats" in hint
    _send(server, {"type": "canvas_focus", "id": None})
    assert ctx.focused_canvas is None
    assert ctx.canvas_focus_hint() is None


def test_remove_clears_focus_if_focused(tmp_path):
    server, ctx = _server(tmp_path)
    server._add_item("markdown", "Doomed", "bye")
    _send(server, {"type": "canvas_focus", "id": "c1"})
    assert ctx.focused_canvas is not None
    _send(server, {"type": "canvas_remove", "id": "c1"})
    assert "c1" not in server._board["items"]
    assert ctx.focused_canvas is None


def test_clear_empties_board(tmp_path):
    server, ctx = _server(tmp_path)
    server._add_item("markdown", "a", "1")
    server._add_item("markdown", "b", "2")
    _send(server, {"type": "canvas_clear"})
    assert server._board["items"] == {} and server._board["order"] == []


def test_viewport_is_saved(tmp_path):
    server, _ = _server(tmp_path)
    _send(server, {"type": "canvas_viewport", "x": -120.0, "y": 40.0, "zoom": 1.75})
    assert server._board["viewport"] == {"x": -120.0, "y": 40.0, "zoom": 1.75}


def test_persistence_round_trip(tmp_path):
    server, _ = _server(tmp_path)
    server._add_item("chart", "Tokens", json.dumps({"type": "line"}))
    _send(server, {"type": "canvas_update", "id": "c1", "x": 500, "y": 250, "w": 460, "h": 340})
    _send(server, {"type": "canvas_viewport", "x": 10.0, "y": 20.0, "zoom": 1.2})
    server._write_board()

    fresh, _ = _server(tmp_path)  # a brand-new server on the same project dir
    assert list(fresh._board["items"]) == ["c1"]
    it = fresh._board["items"]["c1"]
    assert (it["x"], it["y"], it["w"], it["h"]) == (500, 250, 460, 340)
    assert fresh._board["viewport"]["zoom"] == 1.2
    assert fresh._next_id == 2  # continues numbering past the loaded items


def test_load_skips_orphan_order_ids(tmp_path):
    server, _ = _server(tmp_path)
    server._add_item("markdown", "real", "x")
    server._write_board()
    # corrupt: an order id with no backing item should be dropped on load
    f = server._board_file
    data = json.loads(f.read_text())
    data["order"].append("ghost")
    f.write_text(json.dumps(data))
    fresh, _ = _server(tmp_path)
    assert fresh._board["order"] == ["c1"]
