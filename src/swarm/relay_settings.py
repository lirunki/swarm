"""Local-controller relay profile and runtime lifecycle; secrets never enter public state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from collections.abc import Callable
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .federation import FederationClient, FederationConfig
from .store import Store, atomic_write


class RelayConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relay_url: str = Field(max_length=2048)
    relay_node_id: str = Field(max_length=64)
    relay_name: str = Field(max_length=120)
    relay_api_key: SecretStr | None = None


class RelaySettings:
    def __init__(
        self,
        store: Store,
        local_api_url: str,
        state_provider: Callable[[], dict],
        initial: FederationConfig | None = None,
        access=None,
    ):
        self.path = store.home / "relay-settings.json"
        self.local_api_url = initial.local_api_url if initial else local_api_url
        self.allow_remote_restart = bool(initial and initial.allow_remote_restart)
        self.state_provider = state_provider
        self.access = access
        self.lock = asyncio.Lock()
        host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname()).strip("-._") or "swarm"
        self.url = initial.url if initial else ""
        self.node_id = (
            initial.node_id
            if initial
            else f"{host[:48]}-{uuid5(NAMESPACE_URL, str(store.home)).hex[:8]}"
        )
        self.name = initial.name if initial else socket.gethostname()[:120]
        self._key = initial.api_key if initial else ""
        self.enabled = initial is not None
        self.load_error: str | None = None
        self.client: FederationClient | None = None
        self._load()
        if self._key and self.url:
            config = self._config(self.url, self.node_id, self.name, self._key)
            self.client = self._new_client(config)

    def _new_client(self, config: FederationConfig) -> FederationClient:
        # Keep compatibility with auth-free integrations while ensuring a configured
        # BrowserAccess object is shared by every replacement federation client.
        if self.access is None:
            return FederationClient(config, self.state_provider)
        return FederationClient(config, self.state_provider, self.access)

    def _config(self, url: str, node_id: str, name: str, key: str) -> FederationConfig:
        config = FederationConfig(
            url=url,
            node_id=node_id,
            name=name,
            api_key=key,
            local_api_url=self.local_api_url,
            allow_remote_restart=self.allow_remote_restart,
        )
        config.validate()
        return config

    def _load(self) -> None:
        if not self.path.exists() and not self.path.is_symlink():
            return
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError("not a regular settings file")
            if os.name != "nt" and self.path.stat().st_mode & 0o077:
                raise ValueError("unsafe settings permissions")
            if self.path.stat().st_size > 16384:
                raise ValueError("oversized settings file")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("version") != 1
                or type(value.get("enabled")) is not bool
            ):
                raise ValueError("invalid settings format")
            config = self._config(value["url"], value["node_id"], value["name"], value["api_key"])
            self.url, self.node_id, self.name, self._key = (
                config.url,
                config.node_id,
                config.name,
                config.api_key,
            )
            self.enabled = value["enabled"]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Never echo parser input, filesystem contents or credentials in an API error.
            self.url, self._key, self.enabled = "", "", False
            self.load_error = (
                "Saved relay settings could not be loaded. Enter the configuration again."
            )

    def _save(self, config: FederationConfig, enabled: bool) -> None:
        data = asdict(config)
        # Host API origin and restart capability are process policy, never browser-controlled.
        for key in ("local_api_url", "allow_remote_restart"):
            data.pop(key)
        atomic_write(
            self.path,
            json.dumps({"version": 1, "enabled": enabled, **data}, ensure_ascii=False) + "\n",
        )

    def status(self) -> dict:
        connected = bool(self.enabled and self.client and self.client.ready)
        error = self.client.last_error if self.enabled and self.client else self.load_error
        state = (
            "connected"
            if connected
            else "reconnecting"
            if self.enabled and error
            else "connecting"
            if self.enabled
            else "disconnected"
        )
        return {
            "relay_url": self.url,
            "relay_node_id": self.node_id,
            "relay_name": self.name,
            "has_api_key": bool(self._key),
            "enabled": self.enabled,
            "connected": connected,
            "status": state,
            "last_error": error,
        }

    def start(self) -> None:
        if self.enabled and self.client:
            self.client.start()

    async def connect(self, request: RelayConnectRequest) -> dict:
        async with self.lock:
            url, node_id, name = (
                request.relay_url.strip(),
                request.relay_node_id.strip(),
                request.relay_name.strip(),
            )
            supplied = (
                request.relay_api_key.get_secret_value().strip() if request.relay_api_key else ""
            )
            if not supplied and url != self.url:
                raise ValueError("Enter an API key when changing the relay URL.")
            key = supplied or self._key
            if not key:
                raise ValueError("A relay API key is required.")
            config = self._config(url, node_id, name, key)
            if self.enabled and self.client and self.client.config == config:
                return self.status()  # Idempotent: do not spawn a duplicate connection.
            # Persist before disturbing a healthy connection. A failed save leaves it intact.
            await asyncio.to_thread(self._save, config, True)
            if self.client:
                await self.client.close()
                self.client.config = config
                self.client.last_error = None
            else:
                self.client = self._new_client(config)
            self.url, self.node_id, self.name, self._key = url, node_id, name, key
            self.enabled, self.load_error = True, None
            self.client.start()
            return self.status()

    async def disconnect(self) -> dict:
        async with self.lock:
            if self._key and self.url:
                config = self._config(self.url, self.node_id, self.name, self._key)
                await asyncio.to_thread(self._save, config, False)
            self.enabled = False
            if self.client:
                await self.client.close()
            return self.status()

    async def close(self) -> None:
        # Controller shutdown does not change the saved user's connection intent.
        async with self.lock:
            if self.client:
                await self.client.close()
