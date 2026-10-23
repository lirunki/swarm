"""Small, loopback-only stdlib client for agent orchestration endpoints."""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

DEFAULT_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class AgentClientError(RuntimeError):
    """An orchestration request was rejected or could not be completed."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def validate_api_url(value: str) -> str:
    """Return a normalized API origin after enforcing the local-only boundary."""
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("--api-url must use http or https (not another scheme)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("--api-url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("--api-url must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid --api-url port: {exc}") from exc
    host = parsed.hostname
    if not host:
        raise ValueError("--api-url must include a loopback host")
    loopback = host.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ValueError("--api-url host must be loopback (localhost, 127.0.0.0/8, or ::1)")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


class AgentClient:
    """JSON client that deliberately ignores proxy configuration and redirects."""

    def __init__(self, api_url: str, *, opener=None, token_file: Path | None = None):
        self.api_url = validate_api_url(api_url)
        self.token_file = token_file
        self._opener = opener or build_opener(*self._handlers())

    def _handlers(self):
        # Loopback-only HTTPS for the local controller uses a verifying context that
        # trusts SWARM_API_CA_FILE; verification is never disabled.
        handlers = [ProxyHandler({}), _NoRedirects()]
        if self.api_url.startswith("https://"):
            handlers.append(HTTPSHandler(context=self._ssl_context()))
        return handlers

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        ca_file = os.environ.get("SWARM_API_CA_FILE")
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
        return context

    @staticmethod
    def _guidance(operation: str) -> str:
        if operation == "spawn":
            return (
                "The outcome may be unknown. Inspect `swarm agent list`, then retry only with "
                "the same --request-id; do not invent a new key for the same task."
            )
        if operation == "message":
            return "The outcome may be unknown; inspect child status before resending the message."
        if operation == "wait":
            return (
                "The wait may already be registered. Inspect status before retrying; do not poll "
                "the child."
            )
        return "The outcome may be unknown; inspect agent status before retrying."

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        expected: int = 200,
        timeout: float = DEFAULT_TIMEOUT,
        operation: str,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        token_path = Path(
            self.token_file
            or os.environ.get("SWARM_API_TOKEN_FILE")
            or (
                Path(os.environ.get("SWARM_HOME", "~/.swarm")).expanduser()
                / "api-auth"
                / "client.token"
            )
        )
        if token_path.exists():
            if (
                token_path.is_symlink()
                or not token_path.is_file()
                or token_path.stat().st_size > 512
            ):
                raise AgentClientError("Local API credential file is invalid")
            if os.name != "nt" and token_path.stat().st_mode & 0o077:
                raise AgentClientError("Local API credential file must be private (mode 0600)")
            token = token_path.read_text(encoding="utf-8").strip()
            if not token or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in token):
                raise AgentClientError("Local API credential file is invalid")
            headers["Authorization"] = f"Bearer {token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.api_url + path, data=data, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=timeout)
            with response:
                status = response.getcode()
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise AgentClientError("API response exceeded the 16 MiB limit")
        except HTTPError as exc:
            detail = exc.read(65536).decode("utf-8", errors="replace")
            try:
                decoded = json.loads(detail)
                detail = decoded.get("detail", decoded) if isinstance(decoded, dict) else decoded
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            raise AgentClientError(f"API returned HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise AgentClientError(
                f"API request failed: {exc}. {self._guidance(operation)}"
            ) from exc
        if status != expected:
            raise AgentClientError(f"API returned HTTP {status}; expected HTTP {expected}")
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentClientError("API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise AgentClientError("API returned JSON that is not an object")
        return decoded

    def product(self, agent_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        return self.request(
            "DELETE" if payload is None else "POST",
            f"/api/agents/{quote(agent_id, safe='')}/product",
            body=payload,
            operation="publish product",
        )

    def files(self, agent_id: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/api/agents/{quote(agent_id, safe='')}/files", operation="list files"
        )

    def add_file(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/agents/{quote(agent_id, safe='')}/files",
            body=payload,
            expected=201,
            operation="register file",
        )

    def remove_file(self, agent_id: str, file_id: str) -> dict[str, Any]:
        return self.request(
            "DELETE",
            f"/api/agents/{quote(agent_id, safe='')}/files/{quote(file_id, safe='')}",
            operation="remove file reference",
        )

    @staticmethod
    def _children(parent_id: str) -> str:
        return f"/api/agents/{quote(parent_id, safe='')}/children"

    def spawn(self, parent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST", self._children(parent_id), body=payload, expected=201, operation="spawn"
        )

    def list(self, parent_id: str) -> dict[str, Any]:
        return self.request("GET", self._children(parent_id), operation="list")

    def status(self, parent_id: str, child_id: str) -> dict[str, Any]:
        path = f"{self._children(parent_id)}/{quote(child_id, safe='')}"
        return self.request("GET", path, operation="status")

    def action(
        self,
        parent_id: str,
        child_id: str,
        action: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        path = f"{self._children(parent_id)}/{quote(child_id, safe='')}/{action}"
        # wait registers an event and returns immediately; it is not a long-poll request.
        return self.request("POST", path, body=body, operation=action)
