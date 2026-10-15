"""Loopback-only HTTP/WebSocket application. One controller per SWARM_HOME."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from anyio import CancelScope
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .controller import Controller
from .discovery import browse_folders, discover_models
from .importers.grok import GrokImportRequest, GrokSessions
from .meta_compact import MetaCompactor, MetaCompactRequest, require_not_busy
from .models import AgentPatch, CreateAgent, WakeRequest
from .orchestration import ChildMessage, ChildStop, Orchestrator, OwnershipError, SpawnRequest
from .processes import Processes
from .sessions import AdoptRequest, Sessions
from .store import Store
from .terminal import bridge
from .tool_inputs import RevertToolInputsRequest, ToolInputCompactor

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def same_origin(headers) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True  # local CLI clients do not set Origin
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc == headers.get("host")


def restart_process() -> None:
    """Replace this controller process after its HTTP response has been sent."""
    time.sleep(0.35)
    argv = list(sys.argv)
    script = Path(argv[0])
    if script.name == "__main__.py" and script.parent.name == "swarm":
        command = [sys.executable, "-m", "swarm", *argv[1:]]
    else:
        command = [sys.executable, *argv]
    os.execv(sys.executable, command)


def create_app(
    home: Path,
    pi: str = "pi",
    recover: bool = True,
    session_roots: list[Path] | None = None,
    allowed_hosts: list[str] | None = None,
    grok_session_roots: list[Path] | None = None,
    api_url: str = "http://127.0.0.1:8787",
    restart_action: Callable[[], None] = restart_process,
) -> FastAPI:
    store = Store(home)
    processes = Processes(store, pi, api_url)
    controller = Controller(store, processes)
    orchestration = Orchestrator(controller)
    meta_compactor = MetaCompactor(controller, pi)
    tool_input_compactor = ToolInputCompactor(meta_compactor)
    sessions = Sessions(store, session_roots)
    grok = GrokSessions(store, grok_session_roots)
    listeners: set[asyncio.Queue] = set()

    async def broadcast() -> None:
        for queue in list(listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(controller.state)

    async def monitor() -> None:
        while True:
            try:
                changed = await asyncio.to_thread(controller.tick)
                coordinated = await asyncio.to_thread(orchestration.tick)
                if changed or coordinated:
                    await broadcast()
            except Exception:
                log.exception("Registry monitor failed; retrying on next tick")
            await asyncio.sleep(0.75)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with store.controller_lock():
            await asyncio.to_thread(orchestration.recover)
            await asyncio.to_thread(tool_input_compactor.recover)
            await asyncio.to_thread(meta_compactor.recover)
            if recover:
                await asyncio.to_thread(controller.recover)
            else:
                await asyncio.to_thread(controller.refresh)
            task = asyncio.create_task(monitor())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(meta_compactor.close)
                # Stopping the web application deliberately does NOT stop any agent.

    app = FastAPI(title="Swarm", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.controller = controller
    app.state.orchestration = orchestration
    app.state.meta_compactor = meta_compactor
    app.state.tool_input_compactor = tool_input_compactor
    app.state.sessions = sessions
    app.state.grok_sessions = grok
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["127.0.0.1", "localhost", "[::1]"]
    )

    @app.middleware("http")
    async def secure_local_access(request: Request, call_next):
        if not same_origin(request.headers):
            return JSONResponse({"detail": "cross-origin access is forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(FileNotFoundError)
    async def not_found(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(OwnershipError)
    async def ownership_error(request: Request, exc: OwnershipError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        controller.refresh()
        return controller.state

    @app.post("/api/controller/restart", status_code=202)
    async def restart_controller(background_tasks: BackgroundTasks):
        # Starlette starts background tasks only after sending the response. The
        # in-place exec keeps the controller PID while tmux-owned agents continue.
        background_tasks.add_task(restart_action)
        return {"restarting": True}

    @app.get("/api/config/models")
    async def models():
        return await asyncio.to_thread(discover_models, pi)

    @app.get("/api/folders")
    async def folders(path: str | None = None):
        return await asyncio.to_thread(browse_folders, path)

    @app.post("/api/agents", status_code=201)
    async def create(request: CreateAgent):
        agent = await asyncio.to_thread(controller.create, request)
        await broadcast()
        return agent.model_dump(mode="json") | {"alive": processes.alive(agent)}

    @app.patch("/api/agents/{agent_id}")
    async def patch(agent_id: UUID, request: AgentPatch):
        def update():
            with controller.lock:
                require_not_busy(store.get(agent_id))
                agent = store.update(agent_id, request.model_dump(exclude_unset=True))
                controller.refresh()
                return agent.model_dump(mode="json") | {"alive": processes.alive(agent)}

        result = await asyncio.to_thread(update)
        await broadcast()
        return result

    @app.post("/api/agents/{parent_id}/children", status_code=201)
    async def spawn_child(parent_id: UUID, request: SpawnRequest):
        result = await asyncio.to_thread(orchestration.spawn, str(parent_id), request)
        await broadcast()
        return result

    @app.get("/api/agents/{parent_id}/children")
    async def children(parent_id: UUID):
        return await asyncio.to_thread(orchestration.children, str(parent_id))

    @app.get("/api/agents/{parent_id}/children/{child_id}")
    async def child_status(parent_id: UUID, child_id: UUID):
        return await asyncio.to_thread(orchestration.status, str(parent_id), str(child_id))

    @app.post("/api/agents/{parent_id}/children/{child_id}/message")
    async def child_message(parent_id: UUID, child_id: UUID, request: ChildMessage):
        return await asyncio.to_thread(
            orchestration.message, str(parent_id), str(child_id), request
        )

    @app.post("/api/agents/{parent_id}/children/{child_id}/wake")
    async def child_wake(parent_id: UUID, child_id: UUID, request: WakeRequest):
        result = await asyncio.to_thread(
            orchestration.wake, str(parent_id), str(child_id), request.message
        )
        await broadcast()
        return result

    @app.post("/api/agents/{parent_id}/children/{child_id}/stop")
    async def child_stop(parent_id: UUID, child_id: UUID, request: ChildStop):
        result = await asyncio.to_thread(
            orchestration.stop, str(parent_id), str(child_id), request.force
        )
        await broadcast()
        return result

    @app.post("/api/agents/{parent_id}/children/{child_id}/wait")
    async def child_wait(parent_id: UUID, child_id: UUID):
        result = await asyncio.to_thread(orchestration.wait, str(parent_id), str(child_id))
        await broadcast()
        return result

    @app.post("/api/agents/{agent_id}/meta-compact", status_code=202)
    async def meta_compact(agent_id: UUID, request: MetaCompactRequest):
        job = await asyncio.to_thread(meta_compactor.start, str(agent_id), request)
        await asyncio.to_thread(controller.refresh)
        await broadcast()
        return job

    @app.post("/api/agents/{agent_id}/compact-tool-inputs")
    async def compact_tool_inputs(agent_id: UUID):
        result = await asyncio.to_thread(tool_input_compactor.compact, str(agent_id))
        await asyncio.to_thread(controller.refresh)
        await broadcast()
        return result

    @app.post("/api/agents/{agent_id}/revert-tool-input-compaction")
    async def revert_tool_input_compaction(
        agent_id: UUID, request: RevertToolInputsRequest | None = None
    ):
        options = request or RevertToolInputsRequest()
        result = await asyncio.to_thread(
            tool_input_compactor.revert, str(agent_id), options.allow_history_loss
        )
        await asyncio.to_thread(controller.refresh)
        await broadcast()
        return result

    @app.post("/api/agents/{agent_id}/wake")
    async def wake(agent_id: UUID, request: WakeRequest):
        agent = await asyncio.to_thread(controller.wake, str(agent_id), request.message)
        await broadcast()
        return agent.model_dump(mode="json") | {"alive": processes.alive(agent)}

    @app.post("/api/agents/{agent_id}/stop")
    async def stop(agent_id: UUID):
        agent = await asyncio.to_thread(controller.stop, str(agent_id))
        await broadcast()
        return agent.model_dump(mode="json") | {"alive": False}

    @app.delete("/api/agents/{agent_id}")
    async def forget(agent_id: UUID):
        agent = await asyncio.to_thread(controller.forget, str(agent_id))
        await broadcast()
        return {"id": str(agent.id), "forgotten": True, "session_file": agent.session_file}

    @app.get("/api/pi/sessions")
    def list_sessions():
        with controller.lock:
            return sessions.list()

    @app.post("/api/agents/adopt", status_code=201)
    async def adopt(request: AdoptRequest):
        def register():
            with controller.lock:
                agent = sessions.adopt(request)
                controller.refresh()
                return agent.model_dump(mode="json") | {"alive": False}

        result = await asyncio.to_thread(register)
        await broadcast()
        return result

    @app.get("/api/grok/sessions")
    def list_grok_sessions():
        with controller.lock:
            return grok.list()

    @app.post("/api/agents/import/grok", status_code=201)
    async def import_grok(request: GrokImportRequest):
        def register():
            with controller.lock:
                agent = grok.import_session(request)
                controller.refresh()
                return agent.model_dump(mode="json") | {"alive": False}

        result = await asyncio.to_thread(register)
        await broadcast()
        return result

    @app.websocket("/ws/state")
    async def state_ws(ws: WebSocket):
        if not same_origin(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        listeners.add(queue)
        queue.put_nowait(controller.state)

        # Listen for disconnect even if no agent changes, avoiding idle client leaks.
        async def sender():
            while True:
                await ws.send_json(await queue.get())

        async def receiver():
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            listeners.discard(queue)
            # Preserve the host's original cancellation while joining child
            # tasks, rather than leaking a new CancelledError from gather.
            with CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    @app.websocket("/ws/terminal/{agent_id}")
    async def terminal(ws: WebSocket, agent_id: UUID, cols: int = 120, rows: int = 34):
        if not same_origin(ws.headers):
            await ws.close(code=1008)
            return
        try:
            agent = await asyncio.to_thread(store.get, agent_id)
        except (OSError, ValueError):
            await ws.close(code=1008)
            return
        if not await asyncio.to_thread(processes.alive, agent):
            await ws.close(code=1008, reason="agent is not running; wake it explicitly")
            return
        await ws.accept()
        await bridge(ws, processes, agent, cols, rows)

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
