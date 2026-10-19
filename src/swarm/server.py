"""Loopback-by-default HTTP/WebSocket application. One controller per SWARM_HOME."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from anyio import CancelScope
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .controller import Controller
from .discovery import browse_folders, discover_models
from .federation import MAX_RPC_REQUEST, FederationConfig
from .files import FileRequest, FileUnavailable
from .importers.grok import GrokImportRequest, GrokSessions
from .meta_compact import MetaCompactor, MetaCompactRequest, require_not_busy
from .models import AgentPatch, CreateAgent, WakeRequest
from .orchestration import ChildMessage, ChildStop, Orchestrator, OwnershipError, SpawnRequest
from .processes import Processes
from .products import ProductRequest, Products
from .relay_settings import RelayConnectRequest, RelaySettings
from .sessions import AdoptRequest, Sessions
from .store import Store
from .terminal import bridge
from .tool_inputs import RevertToolInputsRequest, ToolInputCompactor
from .uploads import MAX_CHUNK_BODY, UploadChunk, UploadRequest, Uploads

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
    federation_config: FederationConfig | None = None,
) -> FastAPI:
    store = Store(home)
    processes = Processes(store, pi, api_url)
    controller = Controller(store, processes)
    orchestration = Orchestrator(controller)
    products = Products(controller)
    uploads = Uploads(controller)
    meta_compactor = MetaCompactor(controller, pi)
    tool_input_compactor = ToolInputCompactor(meta_compactor)
    sessions = Sessions(store, session_roots)
    grok = GrokSessions(store, grok_session_roots)
    listeners: set[asyncio.Queue] = set()
    relay_settings = RelaySettings(store, api_url, lambda: controller.state, federation_config)
    federation = relay_settings.client

    async def broadcast() -> None:
        for queue in list(listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(controller.state)
        if federation and relay_settings.enabled:
            federation.queue_state()

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
            await asyncio.to_thread(products.collect)
            try:
                await asyncio.to_thread(uploads.cleanup, True)
            except (OSError, ValueError, RuntimeError):
                log.exception("Could not clean expired upload sessions")
            await asyncio.to_thread(orchestration.recover)
            await asyncio.to_thread(tool_input_compactor.recover)
            await asyncio.to_thread(meta_compactor.recover)
            if recover:
                await asyncio.to_thread(controller.recover)
            else:
                await asyncio.to_thread(controller.refresh)
            task = asyncio.create_task(monitor())
            relay_settings.start()
            try:
                yield
            finally:
                await relay_settings.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(meta_compactor.close)
                # Stopping the web application deliberately does NOT stop any agent.

    app = FastAPI(title="Swarm", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.controller = controller
    app.state.orchestration = orchestration
    app.state.products = products
    app.state.uploads = uploads
    app.state.meta_compactor = meta_compactor
    app.state.tool_input_compactor = tool_input_compactor
    app.state.sessions = sessions
    app.state.grok_sessions = grok
    app.state.federation = federation
    app.state.relay_settings = relay_settings
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

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/federation/connect":
            # FastAPI normally echoes invalid input; relay credentials must never return.
            errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
            return JSONResponse(
                {"detail": errors}, status_code=422, headers={"Cache-Control": "no-store"}
            )
        return await request_validation_exception_handler(request, exc)

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

    @app.get("/api/federation/settings")
    async def relay_configuration():
        return JSONResponse(relay_settings.status(), headers={"Cache-Control": "no-store"})

    @app.post("/api/federation/connect", status_code=202)
    async def relay_connect(request: RelayConnectRequest):
        nonlocal federation
        status = await relay_settings.connect(request)
        federation = relay_settings.client
        app.state.federation = federation
        return JSONResponse(status, status_code=202, headers={"Cache-Control": "no-store"})

    @app.post("/api/federation/disconnect")
    async def relay_disconnect():
        return JSONResponse(
            await relay_settings.disconnect(), headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/federation/nodes")
    async def federation_nodes():
        if not federation or not relay_settings.enabled:
            return {"enabled": False, "relay_online": False, "nodes": []}
        return {
            "enabled": True,
            "relay_online": federation.websocket is not None,
            "nodes": federation.summaries(),
        }

    @app.api_route(
        "/api/federation/{node_id}/proxy/{remote_path:path}",
        methods=["GET", "POST", "PATCH", "DELETE"],
    )
    async def federation_proxy(node_id: str, remote_path: str, request: Request):
        if not federation:
            return JSONResponse({"detail": "federation is not configured"}, status_code=404)
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
        if declared > MAX_RPC_REQUEST:
            return JSONResponse({"detail": "federated request exceeds 512 KiB"}, status_code=413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_RPC_REQUEST:
                return JSONResponse(
                    {"detail": "federated request exceeds 512 KiB"}, status_code=413
                )
            chunks.append(chunk)
        try:
            result = await federation.request(
                node_id,
                request.method,
                "/" + remote_path.lstrip("/"),
                request.url.query,
                b"".join(chunks),
            )
        except TimeoutError:
            return JSONResponse(
                {"detail": "remote Swarm timed out; mutation outcome may be unknown"},
                status_code=504,
            )
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        content = result.get("_body", b"")
        headers = {
            "Content-Type": result.get("content_type", "application/octet-stream"),
            "Cache-Control": "no-store",
        }
        if result.get("content_disposition"):
            headers["Content-Disposition"] = result["content_disposition"]
        return Response(content, status_code=int(result.get("status", 502)), headers=headers)

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

    @app.exception_handler(FileUnavailable)
    async def file_unavailable(request: Request, exc: FileUnavailable):
        return JSONResponse(
            {"detail": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/agents/{agent_id}/files")
    def list_agent_files(agent_id: UUID):
        # Original-file stats must not hold the global controller lock.
        return JSONResponse(controller.files.list(agent_id), headers={"Cache-Control": "no-store"})

    @app.post("/api/agents/{agent_id}/files", status_code=201)
    async def register_agent_file(agent_id: UUID, request: FileRequest):
        def register():
            with controller.lock:
                result = controller.files.add(agent_id, request)
                controller.refresh()
                return result

        result = await asyncio.to_thread(register)
        await broadcast()
        return result

    async def bounded_upload_json(request: Request, model, limit: int):
        body = bytearray()
        async for block in request.stream():
            if len(body) + len(block) > limit:
                raise FileUnavailable("Upload request exceeds the chunk size limit", 413)
            body.extend(block)
        try:
            return model.model_validate(json.loads(body))
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid upload request metadata or chunk encoding") from exc

    @app.post("/api/agents/{agent_id}/files/uploads", status_code=201)
    async def begin_file_upload(agent_id: UUID, request: Request):
        options = await bounded_upload_json(request, UploadRequest, 4096)
        return await asyncio.to_thread(uploads.begin, str(agent_id), options)

    @app.post("/api/agents/{agent_id}/files/uploads/{upload_id}/chunks")
    async def append_file_upload(agent_id: UUID, upload_id: UUID, request: Request):
        chunk = await bounded_upload_json(request, UploadChunk, MAX_CHUNK_BODY)
        return await asyncio.to_thread(uploads.chunk, str(agent_id), str(upload_id), chunk)

    @app.post("/api/agents/{agent_id}/files/uploads/{upload_id}/finish")
    async def finish_file_upload(agent_id: UUID, upload_id: UUID):
        result = await asyncio.to_thread(uploads.finish, str(agent_id), str(upload_id))
        await broadcast()
        return result

    @app.delete("/api/agents/{agent_id}/files/uploads/{upload_id}")
    async def cancel_file_upload(agent_id: UUID, upload_id: UUID):
        return await asyncio.to_thread(uploads.cancel, str(agent_id), str(upload_id))

    @app.post("/api/agents/{agent_id}/files/{file_id}/insert")
    async def insert_agent_file(agent_id: UUID, file_id: UUID):
        return await asyncio.to_thread(
            controller.insert_file_reference, str(agent_id), str(file_id)
        )

    @app.delete("/api/agents/{agent_id}/files/{file_id}")
    async def unregister_agent_file(agent_id: UUID, file_id: UUID):
        def unregister():
            with controller.lock:
                ref = next(
                    (r for r in controller.files.read(agent_id).files if r.id == file_id), None
                )
                result = (
                    uploads.remove_upload(str(agent_id), str(file_id))
                    if ref and ref.uploaded
                    else controller.files.remove(agent_id, file_id)
                )
                controller.refresh()
                return result

        result = await asyncio.to_thread(unregister)
        await broadcast()
        return result

    @app.get("/api/agents/{agent_id}/files/{file_id}/content")
    async def agent_file_content(agent_id: UUID, file_id: UUID, download: bool = False):
        ref, stream, size, kind = await asyncio.to_thread(controller.files.open, agent_id, file_id)

        def content():
            remaining = size
            try:
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        raise RuntimeError(
                            "Original file changed during download; retry when writing is finished"
                        )
                    remaining -= len(block)
                    yield block
            finally:
                stream.close()

        inline = kind != "application/octet-stream" and not download
        return StreamingResponse(
            content(),
            media_type=kind if inline else "application/octet-stream",
            headers={
                "Content-Disposition": ("inline" if inline else "attachment")
                + "; filename*=UTF-8''"
                + quote(ref.filename, safe=""),
                "Content-Length": str(size),
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(stream.close),
        )

    @app.post("/api/agents/{agent_id}/product")
    async def publish_product(agent_id: UUID, request: ProductRequest):
        result = await asyncio.to_thread(products.publish, str(agent_id), request)
        await broadcast()
        return result

    @app.delete("/api/agents/{agent_id}/product")
    async def clear_product(agent_id: UUID):
        result = await asyncio.to_thread(products.clear, str(agent_id))
        await broadcast()
        return result

    @app.get("/api/agents/{agent_id}/products/{product_id}/download")
    async def download_product(agent_id: UUID, product_id: UUID):
        product, stream = await asyncio.to_thread(products.download, str(agent_id), str(product_id))

        def content():
            try:
                while block := stream.read(65536):
                    yield block
            finally:
                stream.close()

        def finish_download():
            stream.close()
            products.collect()  # Retry cleanup of a superseded snapshot held open by this download.

        return StreamingResponse(
            content(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(product.filename, safe=""),
                "Content-Length": str(product.size),
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(finish_download),
        )

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
        await asyncio.to_thread(products.collect)
        try:
            await asyncio.to_thread(uploads.forget, str(agent.id))
            await asyncio.to_thread(controller.files.forget, agent.id)
        except (OSError, ValueError):
            log.warning("Could not clear forgotten agent's file reference metadata")
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

    async def queue_websocket(ws: WebSocket, queue: asyncio.Queue) -> None:
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
            with CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    @app.websocket("/ws/state")
    async def state_ws(ws: WebSocket):
        if not same_origin(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        listeners.add(queue)
        queue.put_nowait(controller.state)

        try:
            await queue_websocket(ws, queue)
        finally:
            listeners.discard(queue)

    @app.websocket("/ws/federation/presence")
    async def federation_presence_ws(ws: WebSocket):
        if not same_origin(ws.headers) or not federation:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = federation.subscribe_presence()
        try:
            await queue_websocket(ws, queue)
        finally:
            federation.unsubscribe_presence(queue)

    @app.websocket("/ws/federation/{node_id}/state")
    async def federation_state_ws(ws: WebSocket, node_id: str):
        if not same_origin(ws.headers) or not federation:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = federation.subscribe_state(node_id)
        try:
            await queue_websocket(ws, queue)
        finally:
            federation.unsubscribe_state(node_id, queue)

    @app.websocket("/ws/federation/{node_id}/terminal/{agent_id}")
    async def federation_terminal(
        ws: WebSocket, node_id: str, agent_id: UUID, cols: int = 120, rows: int = 34
    ):
        if not same_origin(ws.headers) or not federation:
            await ws.close(code=1008)
            return
        try:
            stream = await federation.open_terminal(node_id, str(agent_id), cols, rows)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            await ws.close(code=1008, reason="remote terminal is unavailable")
            return
        await ws.accept()

        async def output():
            while True:
                data = await stream.receive()
                if data is None:
                    return
                await ws.send_bytes(data)

        async def input_():
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await stream.send_text(message["text"])

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await stream.close()

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
