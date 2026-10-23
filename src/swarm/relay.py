"""Authenticated WSS rendezvous/stream router for federated Swarm controllers."""

from __future__ import annotations

import asyncio
import hmac
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from anyio import CancelScope
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

PROTOCOL_VERSION = 1
NODE_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?\Z")
MAX_NAME = 120
MAX_ROUTE_BYTES = 1_048_576


@dataclass
class Peer:
    node_id: str
    name: str
    capabilities: dict[str, bool]
    websocket: WebSocket
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    connected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "name": self.name,
            "capabilities": self.capabilities,
            "connected_at": self.connected_at,
        }

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await asyncio.wait_for(self.websocket.send_json(message), timeout=10)


class RelayDirectory:
    def __init__(self, api_keys: list[str]):
        keys = [key for key in api_keys if key]
        if not 1 <= len(keys) <= 2:
            raise ValueError("relay requires one or two nonempty API keys")
        if any(len(key.encode()) < 32 for key in keys):
            raise ValueError("relay API keys must be at least 32 bytes")
        self.api_keys = tuple(keys)
        self.peers: dict[str, Peer] = {}
        self.lock = asyncio.Lock()
        self.presence_send_lock = asyncio.Lock()
        self.presence_version = 0

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        token = header.removeprefix("Bearer ")
        matches = [hmac.compare_digest(token, key) for key in self.api_keys]
        return any(matches)

    async def attach(self, peer: Peer) -> bool:
        async with self.lock:
            if peer.node_id in self.peers:
                return False
            self.peers[peer.node_id] = peer
            self.presence_version += 1
        await self.broadcast_presence()
        return True

    async def detach(self, peer: Peer) -> None:
        async with self.lock:
            if self.peers.get(peer.node_id) is not peer:
                return
            del self.peers[peer.node_id]
            self.presence_version += 1
        await self.broadcast_presence()

    async def broadcast_presence(self) -> None:
        # Serialize snapshots and sends so an older membership version can never
        # overtake a newer one on any peer connection.
        async with self.presence_send_lock:
            async with self.lock:
                peers = list(self.peers.values())
                message = {
                    "type": "presence",
                    "version": self.presence_version,
                    "nodes": [
                        peer.public() for peer in sorted(peers, key=lambda item: item.node_id)
                    ],
                }
            await asyncio.gather(*(peer.send(message) for peer in peers), return_exceptions=True)

    async def route(self, sender: Peer, message: dict[str, Any]) -> None:
        target_id = message.get("to")
        payload = message.get("payload")
        if not isinstance(target_id, str) or not isinstance(payload, dict):
            raise ValueError("route requires string 'to' and object 'payload'")
        async with self.lock:
            target = self.peers.get(target_id)
        if not target:
            await sender.send(
                {
                    "type": "route_error",
                    "to": target_id,
                    "request_id": payload.get("request_id") or payload.get("stream_id"),
                    "detail": "target Swarm is offline",
                }
            )
            return
        try:
            await target.send({"type": "route", "from": sender.node_id, "payload": payload})
        except (TimeoutError, RuntimeError):
            # The destination is the slow/broken peer. Remove it rather than
            # propagating the exception through the healthy sender's handler.
            with suppress(RuntimeError):
                await target.websocket.close(code=1011, reason="relay send timeout")
            await self.detach(target)
            await sender.send(
                {
                    "type": "route_error",
                    "to": target_id,
                    "request_id": payload.get("request_id") or payload.get("stream_id"),
                    "detail": "target Swarm connection failed",
                }
            )


def _validate_hello(message: Any) -> tuple[str, str, dict[str, bool]]:
    if not isinstance(message, dict) or message.get("type") != "hello":
        raise ValueError("first frame must be a hello object")
    if message.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("unsupported federation protocol version")
    node_id, name = message.get("node_id"), message.get("name")
    capabilities = message.get("capabilities", {})
    if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
        raise ValueError("invalid node_id")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        raise ValueError("invalid node name")
    if not isinstance(capabilities, dict) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in capabilities.items()
    ):
        raise ValueError("capabilities must be boolean fields")
    return node_id, name.strip(), capabilities


def create_relay_app(api_keys: list[str]) -> FastAPI:
    directory = RelayDirectory(api_keys)
    app = FastAPI(title="Swarm Relay", docs_url=None, redoc_url=None)
    app.state.directory = directory

    @app.get("/health")
    async def health():
        async with directory.lock:
            count = len(directory.peers)
        return {"ok": True, "protocol": PROTOCOL_VERSION, "connected_nodes": count}

    @app.websocket("/v1/connect")
    async def connect(websocket: WebSocket):
        if not directory.authorized(websocket.headers.get("authorization")):
            await websocket.close(code=1008, reason="invalid relay API key")
            return
        await websocket.accept()
        peer: Peer | None = None
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            node_id, name, capabilities = _validate_hello(hello)
            peer = Peer(node_id, name, capabilities, websocket)
            if not await directory.attach(peer):
                await websocket.close(code=1008, reason="node_id is already connected")
                return
            await peer.send(
                {
                    "type": "welcome",
                    "protocol": PROTOCOL_VERSION,
                    "connection_id": peer.connection_id,
                    "max_route_bytes": MAX_ROUTE_BYTES,
                }
            )
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    raise ValueError("frames must be JSON objects")
                if message.get("type") == "route":
                    await directory.route(peer, message)
                elif message.get("type") == "ping":
                    await peer.send({"type": "pong"})
                else:
                    raise ValueError("unsupported relay frame type")
        except (TimeoutError, ValueError) as exc:
            if websocket.client_state.name == "CONNECTED":
                await websocket.close(code=1008, reason=str(exc)[:120])
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            if peer:
                # Disconnect cancellation must not leave a ghost peer or lose its
                # offline notification while other controllers queue messages.
                with CancelScope(shield=True):
                    await directory.detach(peer)

    return app
