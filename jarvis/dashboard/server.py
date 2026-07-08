"""DashboardServer — serves the HUD + Canvas board and streams live events.

Runs uvicorn inside the orchestrator's asyncio loop. One EventBus subscription fans
every event out to all connected browsers, after sending each new client a status
snapshot (HUD) and the full canvas board (Canvas page).

The Canvas board is an infinite, manipulable whiteboard. The server is the source of
truth for it: every `show_on_dashboard` push becomes a board *item* (with a position
and size), and the browser sends layout edits back over the same WebSocket
(move / resize / remove / clear / focus / viewport). The board is persisted to disk per
project, so it survives restarts and re-opening the page. Selecting ("focusing") a card
also records it on the RuntimeContext so the brain knows what "this" refers to.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import state_dir
from ..context import RuntimeContext

# On Windows the .js MIME type is read from the registry (HKCR\.js) and is often "text/plain",
# which makes browsers reject ES modules and — critically — `AudioWorklet.addModule()` (used by
# remote-voice mic capture) with only a console error. Pin it so static JS is always served as
# JavaScript regardless of the host's registry.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

STATIC = Path(__file__).parent / "static"

# Default card size by kind (world pixels). The browser may resize from here.
_DEFAULT_SIZE = {
    "mermaid": (480, 360),
    "chart": (460, 340),
    "image": (440, 340),
    "stats": (440, 220),
    "markdown": (420, 300),
}


class DashboardServer:
    def __init__(self, ctx: RuntimeContext, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.ctx = ctx
        self.host = host
        self.port = port
        self._clients: set[WebSocket] = set()
        # The canvas board: items keyed by id, a back-to-front z-order, and the saved
        # viewport (pan/zoom). Persisted so the board is the same when you reopen it.
        self._board: dict = {"items": {}, "order": [], "viewport": {"x": 0, "y": 0, "zoom": 1.0}}
        self._next_id = 1
        self._board_file = state_dir(ctx.project_dir) / "canvas-board.json"
        self._dirty = False
        self._app = FastAPI()
        self._server: uvicorn.Server | None = None
        self._tasks: list[asyncio.Task] = []
        self._load_board()
        self._setup_routes()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # ----------------------------------------------------------------- board persistence
    def _load_board(self) -> None:
        try:
            data = json.loads(self._board_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = data.get("items")
        order = data.get("order")
        if isinstance(items, dict) and isinstance(order, list):
            # keep only ids that exist in both, preserving order
            self._board["items"] = {k: v for k, v in items.items() if isinstance(v, dict)}
            self._board["order"] = [i for i in order if i in self._board["items"]]
            vp = data.get("viewport")
            if isinstance(vp, dict):
                self._board["viewport"] = {
                    "x": float(vp.get("x", 0)), "y": float(vp.get("y", 0)),
                    "zoom": float(vp.get("zoom", 1.0)) or 1.0,
                }
            nums = [int(i[1:]) for i in self._board["items"] if i[1:].isdigit()]
            self._next_id = (max(nums) + 1) if nums else 1

    def _write_board(self) -> None:
        try:
            self._board_file.parent.mkdir(parents=True, exist_ok=True)
            self._board_file.write_text(json.dumps(self._board), encoding="utf-8")
        except OSError:
            pass  # board persistence is best-effort

    def _schedule_save(self) -> None:
        self._dirty = True

    async def _saver_loop(self) -> None:
        """Debounced board writer — flushes at most ~once/second when something changed."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._dirty:
                    self._dirty = False
                    await asyncio.to_thread(self._write_board)
        except asyncio.CancelledError:
            pass

    # ----------------------------------------------------------------- board mutation
    def _add_item(self, kind: str, title: str, content: str) -> dict:
        """Create a board item from a show_on_dashboard push (browser will place it)."""
        item_id = f"c{self._next_id}"
        self._next_id += 1
        w, h = _DEFAULT_SIZE.get(kind, (420, 300))
        n = len(self._board["order"])
        item = {
            "id": item_id, "kind": kind, "title": title, "content": content,
            "x": 120 + (n % 7) * 56, "y": 120 + (n % 7) * 44, "w": w, "h": h,
            "placed": False,  # browser does smart placement, then reports back
        }
        self._board["items"][item_id] = item
        self._board["order"].append(item_id)
        self._schedule_save()
        return item

    def _update_item(self, msg: dict) -> dict | None:
        item = self._board["items"].get(msg.get("id"))
        if not item:
            return None
        for key in ("x", "y", "w", "h"):
            if isinstance(msg.get(key), (int, float)):
                item[key] = float(msg[key])
        item["placed"] = True
        # bring to front
        if item["id"] in self._board["order"]:
            self._board["order"].remove(item["id"])
            self._board["order"].append(item["id"])
        self._schedule_save()
        return item

    def _remove_item(self, item_id: str) -> bool:
        if item_id in self._board["items"]:
            del self._board["items"][item_id]
            if item_id in self._board["order"]:
                self._board["order"].remove(item_id)
            if self.ctx.focused_canvas and self.ctx.focused_canvas.get("id") == item_id:
                self.ctx.set_focused_canvas(None)
            self._schedule_save()
            return True
        return False

    def _board_payload(self) -> dict:
        items = [self._board["items"][i] for i in self._board["order"] if i in self._board["items"]]
        return {"items": items, "viewport": self._board["viewport"]}

    # ----------------------------------------------------------------- inbound from browser
    async def _handle_client_msg(self, sender: WebSocket, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        kind = msg.get("type")
        if kind == "canvas_update":
            item = self._update_item(msg)
            if item:
                await self._send_to_clients({"type": "canvas_update", "data": {
                    "id": item["id"], "x": item["x"], "y": item["y"],
                    "w": item["w"], "h": item["h"]}}, exclude=sender)
        elif kind == "canvas_remove":
            if self._remove_item(msg.get("id")):
                await self._send_to_clients(
                    {"type": "canvas_remove", "data": {"id": msg.get("id")}}, exclude=sender)
        elif kind == "canvas_clear":
            self._board["items"].clear()
            self._board["order"].clear()
            self.ctx.set_focused_canvas(None)
            self._schedule_save()
            await self._send_to_clients({"type": "canvas_clear", "data": {}}, exclude=sender)
        elif kind == "canvas_viewport":
            vp = self._board["viewport"]
            for key in ("x", "y", "zoom"):
                if isinstance(msg.get(key), (int, float)):
                    vp[key] = float(msg[key])
            self._schedule_save()
        elif kind == "canvas_focus":
            item = self._board["items"].get(msg.get("id")) if msg.get("id") else None
            self.ctx.set_focused_canvas(dict(item) if item else None)
            self.ctx.bus.emit(
                "canvas_focus",
                id=(item or {}).get("id"),
                title=(item or {}).get("title", ""),
                kind=(item or {}).get("kind", ""),
            )
        elif kind in ("audio_hello", "ptt_press", "ptt_release", "barge_in", "ping"):
            # Remote/tunneled voice: this browser tab is the mic + speaker. Route control to
            # the audio hub (only present in transport="browser" mode; otherwise ignored).
            if self.ctx.remote_audio is not None:
                self.ctx.remote_audio.on_control(sender, msg)

    def _setup_routes(self) -> None:
        app = self._app

        # Local single-user dashboard: never let the browser cache HUD assets, so a plain
        # reload always picks up new HTML/JS/CSS (no hard-refresh needed after an update).
        @app.middleware("http")
        async def _no_cache(request, call_next):
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC / "index.html")

        # The full-page infinite board (/canvas) was retired in favour of the navigable
        # mini-viewer in the HUD; the board model + focus still live on here for it.

        @app.get("/health")
        async def health() -> dict:
            return {"ok": True, "clients": len(self._clients)}

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            self._clients.add(websocket)
            try:
                await websocket.send_json(
                    {"type": "snapshot", "data": self.ctx.state.status.as_dict()}
                )
                await websocket.send_json({"type": "board", "data": self._board_payload()})
                # Tell the tab whether it should act as the mic + speaker (remote voice).
                await websocket.send_json(
                    {"type": "audio_mode", "data": {"remote": self.ctx.remote_audio is not None}}
                )
                while True:
                    # Text frames = JSON control (canvas edits + remote-voice PTT); binary
                    # frames = mic PCM for remote voice.
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    text = message.get("text")
                    if text is not None:
                        await self._handle_client_msg(websocket, text)
                    else:
                        data = message.get("bytes")
                        if data is not None and self.ctx.remote_audio is not None:
                            self.ctx.remote_audio.on_binary(websocket, data)
            except Exception:
                pass
            finally:
                self._clients.discard(websocket)
                if self.ctx.remote_audio is not None:
                    self.ctx.remote_audio.on_disconnect(websocket)

        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    async def _send_to_clients(self, payload: dict, exclude: WebSocket | None = None) -> None:
        for client in list(self._clients):
            if client is exclude:
                continue
            try:
                await client.send_json(payload)
            except Exception:
                self._clients.discard(client)

    async def _broadcast_loop(self) -> None:
        queue = self.ctx.bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.type == "canvas":
                    # A show_on_dashboard push → a new board item the browser will place.
                    item = self._add_item(
                        (event.data.get("kind") or "markdown"),
                        event.data.get("title") or "",
                        event.data.get("content") or "",
                    )
                    await self._send_to_clients({"type": "canvas_add", "data": item})
                    continue
                payload = {"type": event.type, "data": event.data}
                await self._send_to_clients(payload)
        except asyncio.CancelledError:
            pass
        finally:
            self.ctx.bus.unsubscribe(queue)

    async def start(self) -> None:
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # don't hijack Ctrl+C
        self._tasks = [
            asyncio.create_task(self._server.serve(), name="dashboard-uvicorn"),
            asyncio.create_task(self._broadcast_loop(), name="dashboard-broadcast"),
            asyncio.create_task(self._saver_loop(), name="dashboard-saver"),
        ]
        for _ in range(200):  # wait (max ~10s) for uvicorn to bind
            if self._server.started:
                break
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._dirty:  # final flush
            self._dirty = False
            await asyncio.to_thread(self._write_board)
        if self._server is not None:
            self._server.should_exit = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except BaseException:
                pass
        self._tasks = []
