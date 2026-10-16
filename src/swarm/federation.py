"""Outbound Swarm federation client and multiplexed RPC/terminal transport."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .relay import NODE_ID, PROTOCOL_VERSION

log = logging.getLogger(__name__)
MAX_RPC_REQUEST = 512 * 1024
MAX_RPC_RESPONSE = 129 * 1024 * 1024
RPC_CHUNK = 384 * 1024
ALLOWED_METHODS = {"GET", "POST", "PATCH", "DELETE"}


def _local_ssl_context() -> ssl.SSLContext:
    """Verifying TLS context scoped to local dashboard connections only.

    Loads SWARM_API_CA_FILE on top of the system trust store; verification is
    never disabled. This context is deliberately not used for the remote relay
    (wss://) connection, which keeps using the websockets default handling.
    """
    context = ssl.create_default_context()
    ca_file = os.environ.get("SWARM_API_CA_FILE")
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    return context


@dataclass(frozen=True)
class FederationConfig:
    url: str
    node_id: str
    name: str
    api_key: str = field(repr=False)
    local_api_url: str
    allow_remote_restart: bool = False

    def validate(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in {"wss", "ws"}:
            raise ValueError(
                "relay URL must use wss:// (ws:// is accepted only for private testing)"
            )
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "relay URL must include a host and must not contain credentials, a query or a fragment"
            )
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("relay URL has an invalid port") from exc
        if parsed.scheme == "ws" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("insecure relay ws:// is allowed only on loopback")
        if not NODE_ID.fullmatch(self.node_id):
            raise ValueError("relay node ID must be 1-64 URL-safe characters")
        if not self.name.strip() or len(self.name) > 120:
            raise ValueError("relay node name must be 1-120 characters")
        if not 32 <= len(self.api_key.encode()) <= 4096 or any(
            ord(char) < 33 or ord(char) > 126 for char in self.api_key
        ):
            raise ValueError(
                "relay API key must be 32-4096 printable ASCII characters without spaces"
            )


class FederatedStream:
    def __init__(self, client: FederationClient, node_id: str, stream_id: str):
        self.client = client
        self.node_id = node_id
        self.stream_id = stream_id
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        self.closed = False

    async def receive(self) -> bytes | None:
        return await self.queue.get()

    async def send_text(self, text: str) -> None:
        if self.closed:
            raise RuntimeError("remote terminal stream is closed")
        await self.client._route(
            self.node_id,
            {
                "type": "stream.data",
                "stream_id": self.stream_id,
                "text": text,
            },
        )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.client.streams.pop((self.node_id, self.stream_id), None)
        with suppress(RuntimeError):
            await self.client._route(
                self.node_id, {"type": "stream.close", "stream_id": self.stream_id}
            )


class FederationClient:
    def __init__(
        self,
        config: FederationConfig,
        state_provider: Callable[[], dict[str, Any]],
    ):
        config.validate()
        self.config = config
        self.state_provider = state_provider
        self.websocket = None
        self.ready = False
        self.last_error: str | None = None
        # websockets DEBUG logging prints handshake headers, including Authorization.
        # A private disabled logger prevents credential leaks even with global DEBUG.
        self._transport_logger = logging.Logger(f"{__name__}.transport")
        self._transport_logger.disabled = True
        self.send_lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.state_task: asyncio.Task | None = None
        self.state_dirty = False
        self.presence_version = -1
        # Product snapshots may be large (bounded at 129 MiB). Limit concurrent
        # local response buffering so trusted peers cannot multiply it without bound.
        self.rpc_semaphore = asyncio.Semaphore(2)
        self.stopping = False
        self.nodes: dict[str, dict[str, Any]] = {}
        self.remote_states: dict[str, dict[str, Any]] = {}
        self.presence_listeners: set[asyncio.Queue] = set()
        self.state_listeners: dict[str, set[asyncio.Queue]] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.rpc_responses: dict[str, dict[str, Any]] = {}
        self.rpc_chunks: dict[str, list[bytes]] = {}
        self.streams: dict[tuple[str, str], FederatedStream] = {}
        self.inbound_streams: dict[tuple[str, str], asyncio.Queue] = {}
        self.inbound_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if not self.task:
            self.stopping = False
            self.task = asyncio.create_task(self._run(), name="swarm-federation")

    async def close(self) -> None:
        self.stopping = True
        if self.websocket:
            await self.websocket.close()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.state_task:
            self.state_task.cancel()
            await asyncio.gather(self.state_task, return_exceptions=True)
            self.state_task = None
        for task in list(self.inbound_tasks):
            task.cancel()
        await asyncio.gather(*self.inbound_tasks, return_exceptions=True)
        self._fail_connection(RuntimeError("federation client stopped"))
        self.ready = False
        self.nodes = {}
        await self._invalidate_remote_states()
        await self._notify_presence()

    async def _run(self) -> None:
        delay = 1.0
        while not self.stopping:
            try:
                async with connect(
                    self.config.url,
                    additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                    logger=self._transport_logger,
                    max_size=1_048_576,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    compression=None,
                ) as websocket:
                    self.websocket = websocket
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "protocol": PROTOCOL_VERSION,
                                "node_id": self.config.node_id,
                                "name": self.config.name,
                                "capabilities": {
                                    "remote_restart": self.config.allow_remote_restart
                                },
                            }
                        )
                    )
                    delay = 1.0
                    await self._receive_loop(websocket)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self.stopping:
                    self.last_error = self._connection_error(exc)
                    log.warning("Federation relay disconnected: %s", self.last_error)
            finally:
                self.websocket = None
                self.ready = False
                self.nodes = {}
                self.presence_version = -1
                await self._invalidate_remote_states()
                await self._notify_presence()
                self._fail_connection(
                    RuntimeError("relay connection was lost; outcome may be unknown")
                )
            if not self.stopping:
                await asyncio.sleep(delay + random.random() * min(delay, 1.0))
                delay = min(delay * 2, 30)

    @staticmethod
    def _connection_error(error: Exception) -> str:
        # An untrusted peer can put arbitrary text in close reasons/HTTP headers.
        # Never reflect those strings (or Authorization values) into logs/settings.
        if isinstance(error, InvalidStatus):
            return "Relay rejected the connection. Check the API key and relay URL."
        if isinstance(error, ConnectionClosed):
            return "Relay closed the connection. Check the node ID and relay configuration."
        if isinstance(error, OSError):
            return "Could not reach the relay. Check the URL, TLS certificate and network."
        return "Relay connection failed. Check the URL, node ID and API key."

    async def _receive_loop(self, websocket) -> None:
        async for raw in websocket:
            if not isinstance(raw, str):
                raise ValueError("relay sent an unsupported binary frame")
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "welcome":
                if message.get("protocol") != PROTOCOL_VERSION:
                    raise ValueError("unsupported relay protocol")
                self.ready = True
                self.last_error = None
                await self._notify_presence()
                self.queue_state()
            elif kind == "presence":
                version = message.get("version")
                if not isinstance(version, int) or version <= self.presence_version:
                    continue
                self.presence_version = version
                self.nodes = {
                    item["id"]: item
                    for item in message.get("nodes", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                await self._invalidate_remote_states(set(self.nodes))
                await self._notify_presence()
                self.queue_state()
            elif kind == "route":
                sender, payload = message.get("from"), message.get("payload")
                if isinstance(sender, str) and isinstance(payload, dict):
                    try:
                        await self._handle_route(sender, payload)
                    except Exception as exc:
                        log.warning("Rejected malformed federation frame from %s: %s", sender, exc)
            elif kind == "route_error":
                request_id = message.get("request_id")
                self._reject_pending(
                    request_id, RuntimeError(message.get("detail", "route failed"))
                )
            elif kind not in {"pong"}:
                raise ValueError("relay sent an unsupported frame")

    async def _send(self, message: dict[str, Any]) -> None:
        websocket = self.websocket
        if not websocket:
            raise RuntimeError("federation relay is offline")
        encoded = json.dumps(message, separators=(",", ":"))
        if len(encoded.encode()) > 1_048_576:
            raise ValueError("federation frame exceeds 1 MiB")
        async with self.send_lock:
            await websocket.send(encoded)

    async def _route(self, node_id: str, payload: dict[str, Any]) -> None:
        if node_id == self.config.node_id:
            raise ValueError("cannot route federation traffic to this local Swarm")
        await self._send({"type": "route", "to": node_id, "payload": payload})

    def queue_state(self) -> None:
        """Coalesce state publication without coupling local API latency to the relay."""
        self.state_dirty = True
        if not self.state_task or self.state_task.done():
            self.state_task = asyncio.create_task(self._flush_state(), name="federation-state")

    async def _flush_state(self) -> None:
        while self.state_dirty and self.websocket:
            self.state_dirty = False
            try:
                await self.publish_state()
            except Exception as exc:
                log.warning("Could not publish federated state: %s", exc)

    async def publish_state(self) -> None:
        if not self.websocket:
            return
        state = self.state_provider()
        payload = {"type": "state", "state": state}
        for node_id in list(self.nodes):
            if node_id != self.config.node_id:
                with suppress(RuntimeError, ValueError, ConnectionClosed, TimeoutError):
                    await self._route(node_id, payload)

    async def _invalidate_remote_states(self, online: set[str] | None = None) -> None:
        online = online or set()
        removed = [node_id for node_id in self.remote_states if node_id not in online]
        for node_id in removed:
            self.remote_states.pop(node_id, None)
            offline = {
                "agents": [],
                "errors": {},
                "health": {},
                "home": "",
                "federation_offline": True,
            }
            for queue in list(self.state_listeners.get(node_id, set())):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(offline)

    def summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for node_id, node in sorted(self.nodes.items()):
            if node_id == self.config.node_id:
                continue
            remote_state = self.remote_states.get(node_id)
            summaries.append(
                node
                | {
                    "online": True,
                    "agent_count": len(remote_state.get("agents", [])) if remote_state else None,
                    "state_ready": remote_state is not None,
                }
            )
        return summaries

    async def _notify_presence(self) -> None:
        value = {
            "nodes": self.summaries(),
            "relay_online": self.websocket is not None,
            "enabled": bool(self.task) and not self.stopping,
        }
        for queue in list(self.presence_listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(value)

    async def _notify_state(self, node_id: str) -> None:
        state = self.remote_states.get(node_id)
        if state is None:
            return
        for queue in list(self.state_listeners.get(node_id, set())):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(state)

    def subscribe_presence(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.presence_listeners.add(queue)
        queue.put_nowait(
            {
                "nodes": self.summaries(),
                "relay_online": self.websocket is not None,
                "enabled": bool(self.task) and not self.stopping,
            }
        )
        return queue

    def unsubscribe_presence(self, queue: asyncio.Queue) -> None:
        self.presence_listeners.discard(queue)

    def subscribe_state(self, node_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.state_listeners.setdefault(node_id, set()).add(queue)
        if node_id in self.remote_states:
            queue.put_nowait(self.remote_states[node_id])
        return queue

    def unsubscribe_state(self, node_id: str, queue: asyncio.Queue) -> None:
        listeners = self.state_listeners.get(node_id)
        if listeners:
            listeners.discard(queue)

    async def request(
        self, node_id: str, method: str, path: str, query: str, body: bytes
    ) -> dict[str, Any]:
        if method not in ALLOWED_METHODS or not path.startswith("/api/"):
            raise ValueError("unsupported federated API request")
        if len(body) > MAX_RPC_REQUEST:
            raise ValueError("federated API request exceeds 512 KiB")
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._route(
                node_id,
                {
                    "type": "rpc.request",
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "query": query,
                    "body": base64.b64encode(body).decode(),
                },
            )
            return await asyncio.wait_for(future, timeout=90)
        finally:
            self.pending.pop(request_id, None)
            self.rpc_responses.pop(request_id, None)
            self.rpc_chunks.pop(request_id, None)

    async def open_terminal(
        self, node_id: str, agent_id: str, cols: int, rows: int
    ) -> FederatedStream:
        stream_id = str(uuid4())
        stream = FederatedStream(self, node_id, stream_id)
        self.streams[(node_id, stream_id)] = stream
        future = asyncio.get_running_loop().create_future()
        self.pending[stream_id] = future
        try:
            await self._route(
                node_id,
                {
                    "type": "stream.open",
                    "stream_id": stream_id,
                    "agent_id": agent_id,
                    "cols": cols,
                    "rows": rows,
                },
            )
            await asyncio.wait_for(future, timeout=20)
            return stream
        except Exception:
            self.streams.pop((node_id, stream_id), None)
            raise
        finally:
            self.pending.pop(stream_id, None)

    def _reject_pending(self, request_id: Any, error: Exception) -> None:
        if isinstance(request_id, str):
            future = self.pending.get(request_id)
            if future and not future.done():
                future.set_exception(error)

    def _fail_connection(self, error: Exception) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        for stream in list(self.streams.values()):
            while not stream.queue.empty():
                stream.queue.get_nowait()
            stream.queue.put_nowait(None)
        self.streams.clear()

    async def _handle_route(self, sender: str, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "state":
            state = payload.get("state")
            if sender in self.nodes and isinstance(state, dict):
                self.remote_states[sender] = state
                await self._notify_state(sender)
                await self._notify_presence()
        elif kind == "rpc.request":
            self._track(asyncio.create_task(self._serve_rpc(sender, payload)))
        elif kind == "rpc.response":
            request_id = payload.get("request_id")
            if payload.get("chunked") and isinstance(request_id, str):
                self.rpc_responses[request_id] = payload
                self.rpc_chunks[request_id] = []
            else:
                future = self.pending.get(request_id)
                if future and not future.done():
                    payload["_body"] = base64.b64decode(payload.get("body", ""), validate=True)
                    future.set_result(payload)
        elif kind == "rpc.chunk":
            request_id = payload.get("request_id")
            chunks = self.rpc_chunks.get(request_id)
            if chunks is not None:
                chunk = base64.b64decode(payload.get("data", ""), validate=True)
                if sum(map(len, chunks)) + len(chunk) > MAX_RPC_RESPONSE:
                    self._reject_pending(request_id, ValueError("remote API response is too large"))
                else:
                    chunks.append(chunk)
        elif kind == "rpc.end":
            request_id = payload.get("request_id")
            response = self.rpc_responses.pop(request_id, None)
            chunks = self.rpc_chunks.pop(request_id, None)
            future = self.pending.get(request_id)
            if response is not None and chunks is not None and future and not future.done():
                response["_body"] = b"".join(chunks)
                future.set_result(response)
        elif kind == "stream.open":
            self._track(asyncio.create_task(self._serve_terminal(sender, payload)))
        elif kind == "stream.opened":
            request_id = payload.get("stream_id")
            future = self.pending.get(request_id)
            if future and not future.done():
                future.set_result(True)
        elif kind == "stream.error":
            self._reject_pending(
                payload.get("stream_id"), RuntimeError(payload.get("detail", "stream failed"))
            )
        elif kind == "stream.data":
            await self._deliver_stream_data(sender, payload)
        elif kind == "stream.close":
            await self._deliver_stream_close(sender, payload)

    def _track(self, task: asyncio.Task) -> None:
        if len(self.inbound_tasks) >= 64:
            task.cancel()
            log.warning("Rejected federation work: 64 inbound operations are already active")
            return
        self.inbound_tasks.add(task)
        task.add_done_callback(self.inbound_tasks.discard)

    @staticmethod
    def _validate_remote_path(path: str) -> None:
        # FastAPI decodes percent escapes before route matching. Reject escapes
        # entirely so encoded federation paths or dot segments cannot recurse.
        if (
            not path.startswith("/api/")
            or path.startswith("/api/federation")
            or "%" in path
            or ".." in path
            or "//" in path
            or "\\" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise ValueError("remote API path is forbidden")

    async def _serve_rpc(self, sender: str, payload: dict[str, Any]) -> None:
        async with self.rpc_semaphore:
            await self._serve_rpc_bounded(sender, payload)

    async def _serve_rpc_bounded(self, sender: str, payload: dict[str, Any]) -> None:
        request_id = payload.get("request_id")
        try:
            method, path = payload.get("method"), payload.get("path")
            if method not in ALLOWED_METHODS or not isinstance(path, str):
                raise ValueError("invalid remote API request")
            self._validate_remote_path(path)
            if path == "/api/controller/restart" and not self.config.allow_remote_restart:
                response = {
                    "status": 403,
                    "content_type": "application/json",
                    "content_disposition": None,
                    "_body": b'{"detail":"remote controller restart is disabled"}',
                }
            else:
                body = base64.b64decode(payload.get("body", ""), validate=True)
                query = payload.get("query", "")
                if not isinstance(query, str) or len(query) > 8192:
                    raise ValueError("invalid remote API query")
                response = await asyncio.to_thread(self._local_request, method, path, query, body)
            content = response.pop("_body")
            if len(content) <= RPC_CHUNK:
                await self._route(
                    sender,
                    {
                        "type": "rpc.response",
                        "request_id": request_id,
                        "body": base64.b64encode(content).decode(),
                    }
                    | response,
                )
            else:
                await self._route(
                    sender,
                    {"type": "rpc.response", "request_id": request_id, "chunked": True} | response,
                )
                for offset in range(0, len(content), RPC_CHUNK):
                    await self._route(
                        sender,
                        {
                            "type": "rpc.chunk",
                            "request_id": request_id,
                            "data": base64.b64encode(content[offset : offset + RPC_CHUNK]).decode(),
                        },
                    )
                await self._route(sender, {"type": "rpc.end", "request_id": request_id})
        except Exception as exc:
            with suppress(Exception):
                await self._route(
                    sender,
                    {
                        "type": "rpc.response",
                        "request_id": request_id,
                        "status": 502,
                        "content_type": "application/json",
                        "body": base64.b64encode(
                            json.dumps({"detail": str(exc)}).encode()
                        ).decode(),
                    },
                )

    def _local_request(self, method: str, path: str, query: str, body: bytes) -> dict[str, Any]:
        url = self.config.local_api_url.rstrip("/") + path
        if query:
            url += "?" + query
        headers = {"Accept": "application/json"}
        data = body if body or method in {"POST", "PATCH"} else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        handlers = [urllib.request.ProxyHandler({})]
        if self.config.local_api_url.lower().startswith("https://"):
            # Scoped to the loopback dashboard; never used for the remote relay.
            handlers.append(urllib.request.HTTPSHandler(context=_local_ssl_context()))
        opener = urllib.request.build_opener(*handlers)
        try:
            response = opener.open(request, timeout=60)
        except urllib.error.HTTPError as exc:
            response = exc
        content = response.read(MAX_RPC_RESPONSE + 1)
        if len(content) > MAX_RPC_RESPONSE:
            raise ValueError("remote API response exceeds 129 MiB")
        return {
            "status": response.status,
            "content_type": response.headers.get("Content-Type", "application/octet-stream"),
            "content_disposition": response.headers.get("Content-Disposition"),
            "_body": content,
        }

    async def _serve_terminal(self, sender: str, payload: dict[str, Any]) -> None:
        stream_id = payload.get("stream_id")
        key = (sender, stream_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self.inbound_streams[key] = queue
        try:
            agent_id = payload.get("agent_id")
            cols, rows = int(payload.get("cols", 120)), int(payload.get("rows", 34))
            url = self.config.local_api_url.replace("http://", "ws://", 1).replace(
                "https://", "wss://", 1
            )
            url += f"/ws/terminal/{agent_id}?cols={cols}&rows={rows}"
            # The local terminal websocket trusts the local CA only for wss://
            # loopback; ws:// needs no TLS and the remote relay is unaffected.
            local_ssl = _local_ssl_context() if url.lower().startswith("wss://") else None
            async with connect(
                url,
                max_size=1_048_576,
                compression=None,
                ssl=local_ssl,
            ) as local:
                await self._route(sender, {"type": "stream.opened", "stream_id": stream_id})

                async def output():
                    async for message in local:
                        if isinstance(message, bytes):
                            frame = {
                                "type": "stream.data",
                                "stream_id": stream_id,
                                "data": base64.b64encode(message).decode(),
                            }
                        else:
                            frame = {"type": "stream.data", "stream_id": stream_id, "text": message}
                        await self._route(sender, frame)

                async def input_():
                    while True:
                        frame = await queue.get()
                        if frame is None:
                            return
                        if "text" in frame:
                            await local.send(frame["text"])
                        elif "data" in frame:
                            await local.send(base64.b64decode(frame["data"], validate=True))

                tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for task in done:
                    task.result()
        except Exception as exc:
            with suppress(Exception):
                await self._route(
                    sender,
                    {"type": "stream.error", "stream_id": stream_id, "detail": str(exc)},
                )
        finally:
            self.inbound_streams.pop(key, None)
            with suppress(Exception):
                await self._route(sender, {"type": "stream.close", "stream_id": stream_id})

    async def _deliver_stream_data(self, sender: str, payload: dict[str, Any]) -> None:
        key = (sender, payload.get("stream_id"))
        stream = self.streams.get(key)
        if stream:
            if "data" in payload:
                value = base64.b64decode(payload["data"], validate=True)
            else:
                value = str(payload.get("text", "")).encode()
            try:
                stream.queue.put_nowait(value)
            except asyncio.QueueFull:
                self.streams.pop(key, None)
                stream.closed = True
                while not stream.queue.empty():
                    stream.queue.get_nowait()
                stream.queue.put_nowait(None)
                self._track(
                    asyncio.create_task(
                        self._route(
                            sender,
                            {"type": "stream.close", "stream_id": payload.get("stream_id")},
                        )
                    )
                )
            return
        queue = self.inbound_streams.get(key)
        if queue:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                self.inbound_streams.pop(key, None)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
                self._track(
                    asyncio.create_task(
                        self._route(
                            sender,
                            {"type": "stream.close", "stream_id": payload.get("stream_id")},
                        )
                    )
                )

    async def _deliver_stream_close(self, sender: str, payload: dict[str, Any]) -> None:
        key = (sender, payload.get("stream_id"))
        stream = self.streams.pop(key, None)
        if stream:
            stream.closed = True
            while not stream.queue.empty():
                stream.queue.get_nowait()
            stream.queue.put_nowait(None)
        queue = self.inbound_streams.get(key)
        if queue:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
