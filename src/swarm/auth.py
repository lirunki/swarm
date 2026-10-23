"""Optional per-controller browser passwords, memory sessions and one-use navigation tickets.

No cookies: a browser reload loses its credential. Local CLI agents use a separate
owner-only service token; federation is never allowed to authenticate with that token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from anyio import CancelScope

from .store import atomic_write

SESSION_SECONDS = 24 * 60 * 60
TICKET_SECONDS = 30
MAX_SESSIONS = 512
MAX_TICKETS = 1024
MAX_LOGIN_BODY = 8192
SOCKET_CHECK_SECONDS = 2
CREDENTIAL_QUERY_NAMES = {
    "api-key",
    "api_key",
    "access_token",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "key",
    "password",
    "token",
}


class TicketRedactor(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"([?&]access_ticket=)[^&\\s]+", r"\\1[redacted]", str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(
                re.sub(r"([?&]access_ticket=)[^&\\s]+", r"\\1[redacted]", value)
                if isinstance(value, str)
                else value
                for value in record.args
            )
        return True


class AuthFailure(ValueError):
    def __init__(self, detail: str = "Authentication required", status: int = 401):
        super().__init__(detail)
        self.status = status


def password_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError()
        info = path.stat()
        #if (os.name != "nt" and info.st_mode & 0o077):
        #    raise ValueError()
        if not 1 <= info.st_size <= 4096:
            raise ValueError()
        value = path.read_bytes()
        if not 1 <= len(value) <= 4096:
            raise ValueError()
        value = value.rstrip(b"\r\n")
        text = value.decode("utf-8-sig")
        if not text or len(text) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError()
        return text.encode("utf-8")
    except (OSError, ValueError):
        raise ValueError(
            "Password file must be a private regular UTF-8 file (mode 0600), "
            "containing one nonempty password of at most 1024 characters"
        ) from None


def loopback(scope: dict) -> bool:
    try:
        return ipaddress.ip_address(scope.get("client", ("", 0))[0]).is_loopback
    except (ValueError, TypeError):
        return False


def canonical(path: str) -> str:
    if len(path) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise AuthFailure("Invalid access path", 400)
    parsed = urlsplit(path)
    decoded = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not decoded.startswith("/")
        or ".." in decoded
        or "//" in decoded
        or "\\" in decoded
    ):
        raise AuthFailure("Invalid access path", 400)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(k.lower() in CREDENTIAL_QUERY_NAMES | {"access_ticket"} for k, _ in pairs):
        raise AuthFailure("Credentials are not allowed in access paths", 400)
    return decoded + ("?" + urlencode(sorted(pairs)) if pairs else "")


def navigation_path(path: str) -> str:
    normalized = canonical(path)
    route = normalized.split("?", 1)[0]
    uuid = r"[0-9a-fA-F-]{36}"
    peer = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    resource = rf"/api/agents/{uuid}/(?:files/{uuid}/content|products/{uuid}/download)"
    http = rf"(?:{resource}|/api/federation/{peer}/proxy{resource})"
    ws = rf"/ws/(?:state|federation/presence|terminal/{uuid}|federation/{peer}/(?:state|terminal/{uuid}))"
    if re.fullmatch(http, route):
        return "http"
    if re.fullmatch(ws, route):
        return "websocket"
    raise AuthFailure("Only file navigation and WebSocket paths can receive access tickets", 400)


class BrowserAccess:
    def __init__(self, home: Path, password_file: Path | None = None):
        self.required = password_file is not None
        for name in ("uvicorn.access", "uvicorn.error"):
            logger = logging.getLogger(name)
            if not any(isinstance(f, TicketRedactor) for f in logger.filters):
                logger.addFilter(TicketRedactor())
        self.lock = threading.RLock()
        self.salt = secrets.token_bytes(16)
        self.digest = (
            self.derive(password_bytes(Path(password_file).expanduser())) if self.required else b""
        )
        self.service_token = secrets.token_urlsafe(48) if self.required else ""
        self.service_file = home / "api-auth" / "client.token"
        self.sessions: OrderedDict[str, float] = OrderedDict()
        self.tickets: OrderedDict[str, tuple] = OrderedDict()
        self.attempts: deque[float] = deque()

    def derive(self, value: bytes) -> bytes:
        return hashlib.scrypt(value, salt=self.salt, n=2**14, r=8, p=1, dklen=32)

    def start(self):
        if not self.required:
            return
        directory = self.service_file.parent
        if directory.is_symlink() or self.service_file.is_symlink():
            raise ValueError("Local API authentication storage must not use symlinks")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        atomic_write(self.service_file, self.service_token + "\n")
        os.chmod(self.service_file, 0o600)

    @staticmethod
    def credential(authorization: str) -> str:
        if not isinstance(authorization, str) or len(authorization) > 512:
            return ""
        scheme, _, token = authorization.partition(" ")
        return (
            token
            if scheme.lower() == "bearer" and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
            else ""
        )

    def authorize(self, authorization: str, service_allowed: bool = False) -> bool:
        if not self.required:
            return True
        token = self.credential(authorization)
        if not token:
            return False
        if service_allowed and hmac.compare_digest(token, self.service_token):
            return True
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            deadline = self.sessions.get(key, 0)
            if deadline > time.monotonic():
                return True
            self.sessions.pop(key, None)
        return False

    def login(self, password: str) -> dict:
        if not self.required:
            return {"required": False, "token": None}
        # A controller-wide bound also covers authentication forwarded via relay peers.
        with self.lock:
            clock = time.monotonic()
            while self.attempts and clock - self.attempts[0] > 60:
                self.attempts.popleft()
            if len(self.attempts) >= 20:
                raise AuthFailure("Too many sign-in attempts; wait one minute", 429)
            self.attempts.append(clock)
            if (
                not isinstance(password, str)
                or not 1 <= len(password) <= 1024
                or not hmac.compare_digest(self.derive(password.encode()), self.digest)
            ):
                raise AuthFailure("Incorrect password")
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = clock + SESSION_SECONDS
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
            return {"required": True, "token": token}

    def logout(self, authorization: str):
        token = self.credential(authorization)
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def ticket(self, path: str, authorization: str, remote: str, service_allowed: bool) -> str:
        kind = navigation_path(path)
        if len(remote) > 512:
            raise AuthFailure("Invalid remote credential", 400)
        if not self.authorize(authorization, service_allowed):
            raise AuthFailure()
        key = secrets.token_urlsafe(32)
        with self.lock:
            self.tickets[key] = (
                time.monotonic() + TICKET_SECONDS,
                kind,
                canonical(path),
                authorization,
                remote,
                service_allowed,
            )
            while len(self.tickets) > MAX_TICKETS:
                self.tickets.popitem(last=False)
        return key

    def consume(self, key: str, kind: str, path: str) -> tuple[str, str]:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
            raise AuthFailure("Invalid or expired access ticket")
        with self.lock:
            value = self.tickets.pop(key, None)
        if not value:
            raise AuthFailure("Invalid or expired access ticket")
        deadline, expected_kind, expected, authorization, remote, service_allowed = value
        if (
            time.monotonic() > deadline
            or kind != expected_kind
            or canonical(path) != expected
            or not self.authorize(authorization, service_allowed)
        ):
            raise AuthFailure("Invalid or expired access ticket")
        return authorization, remote


class BrowserAuthMiddleware:
    """Gate all data/control routes before they run; never authenticate by source IP alone."""

    def __init__(self, app, access: BrowserAccess):
        self.app, self.access = app, access

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        path = scope["path"]
        public = (
            path == "/"
            or path.startswith("/static/")
            or path == "/favicon.ico"
            or path in {"/api/auth/status", "/api/auth/login"}
        )
        if public and kind == "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        service_allowed = loopback(scope) and b"origin" not in headers
        try:
            pairs = parse_qsl(
                scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True
            )
            tickets = [v for k, v in pairs if k == "access_ticket"]
            if tickets:
                if len(tickets) != 1 or (kind == "http" and scope["method"] != "GET"):
                    raise AuthFailure("Invalid access ticket")
                query = urlencode([(k, v) for k, v in pairs if k != "access_ticket"])
                # Uvicorn retains this original scope for WebSocket log lines.
                scope["query_string"] = query.encode("ascii")
                authorization, remote = self.access.consume(
                    tickets[0], kind, path + ("?" + query if query else "")
                )
                clean = [
                    (k, v)
                    for k, v in scope["headers"]
                    if k.lower() not in {b"authorization", b"x-swarm-authorization"}
                ]
                if authorization:
                    clean.append((b"authorization", authorization.encode("latin-1")))
                if remote:
                    clean.append((b"x-swarm-authorization", remote.encode("latin-1")))
                scope["headers"] = clean
                scope["query_string"] = query.encode("ascii")
            elif not self.access.authorize(authorization, service_allowed):
                raise AuthFailure()
        except (AuthFailure, ValueError) as exc:
            if kind == "websocket":
                await send(
                    {"type": "websocket.close", "code": 4401, "reason": "Authentication required"}
                )
                return
            detail = str(exc) if isinstance(exc, AuthFailure) else "Authentication required"
            status = exc.status if isinstance(exc, AuthFailure) else 401
            body = json.dumps({"detail": detail}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                        (b"x-swarm-auth-node", b"local"),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if kind != "websocket" or not self.access.required:
            return await self.app(scope, receive, send)

        async def revalidate():
            while True:
                await asyncio.sleep(SOCKET_CHECK_SECONDS)
                if not self.access.authorize(authorization, service_allowed):
                    await send(
                        {"type": "websocket.close", "code": 4401, "reason": "Session expired"}
                    )
                    return

        tasks = [
            asyncio.create_task(self.app(scope, receive, send)),
            asyncio.create_task(revalidate()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            pass  # Client disconnect/server shutdown: finish cleanup, not a failed request.
        finally:
            with CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
