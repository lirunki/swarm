"""Chunked browser uploads to the owning controller's private temporary directory."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .files import MAX_AGENT_FILES, FileRequest, FileUnavailable
from .models import now
from .products import MAX_PRODUCT_BYTES, open_regular
from .store import atomic_write

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)
CHUNK_BYTES = 192 * 1024  # JSON plus the relay's own base64 envelope stays below frame limits.
MAX_CHUNK_BODY = 300 * 1024
MAX_ACTIVE_UPLOADS = 8
MAX_TEMP_BYTES = 1024 * 1024 * 1024
UPLOAD_TTL = timedelta(minutes=10)
MAX_UPLOAD_RECORDS = 4096
MAX_TOMBSTONES = 256


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: UUID
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(ge=0, le=MAX_PRODUCT_BYTES, strict=True)
    label: str = Field(default="", max_length=120)

    @field_validator("filename")
    @classmethod
    def basename(cls, value: str) -> str:
        if (
            not value.strip()
            or value in {".", ".."}
            or value.endswith((".", " "))
            or len(value.encode("utf-8")) > 240
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:<>"|?*\u2028\u2029' for c in value)
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)
        ):
            raise ValueError("upload filename must be a safe plain filename, not a path")
        return value


class UploadChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: int = Field(ge=0, strict=True)
    data: str = Field(min_length=1, max_length=4 * ((CHUNK_BYTES + 2) // 3))


class UploadState(UploadRequest):
    status: str = "receiving"
    touched_at: str = Field(default_factory=lambda: now().isoformat())
    result: dict | None = None
    device: int = 0
    inode: int = 0


class Uploads:
    def __init__(self, controller: Controller):
        self.controller, self.store = controller, controller.store
        self.root = self.store.home / "uploads"
        key = hashlib.sha256(str(self.store.home).encode()).hexdigest()[:16]
        self.temp_root = Path(tempfile.gettempdir()).resolve() / f"swarm-uploads-{key}"

    def state_path(self, agent_id: UUID | str, upload_id: UUID | str) -> Path:
        directory = self.root / str(UUID(str(agent_id)))
        path = directory / f"{UUID(str(upload_id))}.json"
        if any(p.is_symlink() for p in (self.root, directory, path)):
            raise ValueError("upload metadata must not use symbolic links")
        if not path.resolve().is_relative_to(self.store.home):
            raise ValueError("upload metadata escapes registry")
        return path

    def payload(self, agent_id: UUID | str, state: UploadState) -> Path:
        directory = self.temp_root / str(UUID(str(agent_id))) / str(state.upload_id)
        path = directory / state.filename
        if any(p.is_symlink() for p in (self.temp_root, directory.parent, directory, path)):
            raise ValueError("upload storage must not use symbolic links")
        if not path.resolve().is_relative_to(self.temp_root.resolve()):
            raise ValueError("upload storage escapes temporary directory")
        if self.temp_root.exists() and hasattr(os, "getuid"):
            if self.temp_root.stat().st_uid != os.getuid():
                raise ValueError("temporary upload directory belongs to another user")
        return path

    def read(self, agent_id, upload_id) -> UploadState:
        path = self.state_path(agent_id, upload_id)
        if path.stat().st_size > 16384:
            raise ValueError("upload metadata is oversized")
        value = UploadState.model_validate_json(path.read_text(encoding="utf-8"))
        if str(value.upload_id) != str(upload_id):
            raise ValueError("upload identity mismatch")
        return value

    def save(self, agent_id, state: UploadState):
        state.touched_at = now().isoformat()
        atomic_write(self.state_path(agent_id, state.upload_id), state.model_dump_json() + "\n")

    def states(self):
        if self.root.is_symlink():
            raise ValueError("upload metadata root must not be a symbolic link")
        for path in self.root.glob("*/*.json"):
            agent_id, upload_id = UUID(path.parent.name), UUID(path.stem)
            if path != self.state_path(agent_id, upload_id):
                raise ValueError("noncanonical upload metadata path")
            yield agent_id, self.read(agent_id, upload_id)

    def registered(self, agent_id, path: Path) -> bool:
        return any(ref.path == str(path) for ref in self.controller.files.read(agent_id).files)

    def cleanup(self, interrupted: bool = False):
        """Only stale, unregistered partial uploads expire. Completed references are retained."""
        from datetime import datetime

        with self.controller.lock:
            states = list(self.states())
            for agent_id, state in states:
                if state.status not in {"receiving", "finishing"}:
                    continue
                path = self.payload(agent_id, state)
                index = self.controller.files.read(agent_id)
                ref = next((r for r in index.files if r.path == str(path)), None)
                if ref is not None:
                    if state.status != "finishing":
                        continue  # Never delete a path explicitly registered during transfer.
                    state.status = "complete"
                    state.result = {
                        "agent_id": str(agent_id),
                        "file": self.controller.files.info(ref),
                        "revision": index.revision,
                    }
                    self.save(agent_id, state)
                    continue
                if (
                    not interrupted
                    and now() - datetime.fromisoformat(state.touched_at) < UPLOAD_TTL
                ):
                    continue
                if path.parent.exists():
                    shutil.rmtree(path.parent)
                state.status = "expired"
                self.save(agent_id, state)
            terminal = [(id, s) for id, s in states if s.status in {"expired", "cancelled"}]
            terminal.sort(key=lambda item: item[1].touched_at, reverse=True)
            for agent_id, state in terminal[MAX_TOMBSTONES:]:
                self.state_path(agent_id, state.upload_id).unlink(missing_ok=True)
            for agent_id, state in states:
                path = self.payload(agent_id, state)
                if (
                    state.status == "complete"
                    and not path.exists()
                    and not self.registered(agent_id, path)
                ):
                    self.state_path(agent_id, state.upload_id).unlink(missing_ok=True)

    def begin(self, agent_id: str, request: UploadRequest) -> dict:
        with self.controller.lock:
            self.store.get(agent_id)
            self.cleanup()
            state_path = self.state_path(agent_id, request.upload_id)
            if state_path.exists():
                state = self.read(agent_id, request.upload_id)
                expected = request.model_dump()
                actual = {key: getattr(state, key) for key in expected}
                if actual != expected:
                    raise RuntimeError("upload ID already belongs to different file metadata")
                if state.status not in {"receiving", "finishing", "complete"}:
                    raise FileUnavailable("Upload was cancelled or expired; choose the file again")
                path = self.payload(agent_id, state)
                if not path.is_file():
                    raise FileUnavailable("Temporary upload disappeared; choose the file again")
                return {
                    "upload_id": str(state.upload_id),
                    "offset": path.stat().st_size,
                    "chunk_bytes": CHUNK_BYTES,
                    "status": "complete" if state.status == "complete" else "receiving",
                    **(state.result or {}),
                }
            all_states = list(self.states())
            if len(all_states) >= MAX_UPLOAD_RECORDS:
                raise ValueError(
                    "Upload metadata limit reached; remove old uploads before retrying"
                )
            pending = [(id, s) for id, s in all_states if s.status in {"receiving", "finishing"}]
            if len(pending) >= MAX_ACTIVE_UPLOADS:
                raise RuntimeError("Too many active uploads; retry after another finishes")
            reserved = sum(str(id) == str(agent_id) for id, _ in pending)
            if len(self.controller.files.read(agent_id).files) + reserved >= MAX_AGENT_FILES:
                raise ValueError("An agent can register at most 64 files")
            used = sum(
                s.size
                for id, s in all_states
                if s.status in {"receiving", "finishing", "complete"}
                and self.payload(id, s).exists()
            )
            if used + request.size > MAX_TEMP_BYTES:
                raise ValueError(
                    "Temporary uploads exceed the 1 GiB quota; remove old server-side temporary uploads first"
                )
            state = UploadState(**request.model_dump())
            path = self.payload(agent_id, state)
            if path.parent.exists():
                raise RuntimeError("Upload directory already exists; choose a new upload ID")
            self.temp_root.mkdir(mode=0o700, exist_ok=True)
            path.parent.parent.mkdir(mode=0o700, exist_ok=True)
            path.parent.mkdir(mode=0o700)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                info = os.fstat(fd)
                state.device, state.inode = info.st_dev, info.st_ino
                os.close(fd)
                self.save(agent_id, state)
            except Exception:
                shutil.rmtree(path.parent, ignore_errors=True)
                raise
            return {
                "upload_id": str(state.upload_id),
                "offset": 0,
                "chunk_bytes": CHUNK_BYTES,
                "status": "receiving",
            }

    def chunk(self, agent_id: str, upload_id: str, request: UploadChunk) -> dict:
        block = base64.b64decode(request.data, validate=True)
        if not block or len(block) > CHUNK_BYTES:
            raise ValueError("invalid upload chunk size")
        with self.controller.lock:
            self.store.get(agent_id)
            state = self.read(agent_id, upload_id)
            if state.status != "receiving":
                raise RuntimeError("upload is no longer receiving data")
            path = self.payload(agent_id, state)
            # Reject symlinks/device substitutions before opening a write descriptor.
            with open_regular(path) as check:
                info = os.fstat(check.fileno())
                if (info.st_dev, info.st_ino) != (state.device, state.inode):
                    raise RuntimeError("Temporary upload file was replaced")
                size = info.st_size
                if request.offset < size and request.offset + len(block) <= size:
                    check.seek(request.offset)
                    if check.read(len(block)) == block:
                        return {"offset": size}  # A lost chunk response may be retried safely.
                if request.offset != size:
                    raise RuntimeError("upload offset mismatch; no bytes were overwritten")
            if size + len(block) > state.size:
                raise ValueError("chunk exceeds declared upload size")
            fd = os.open(
                path,
                os.O_WRONLY
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            with os.fdopen(fd, "ab") as output:
                info = os.fstat(output.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size != size
                    or (info.st_dev, info.st_ino) != (state.device, state.inode)
                ):
                    raise RuntimeError("upload changed while writing")
                output.write(block)
                output.flush()
                os.fsync(output.fileno())
            self.save(agent_id, state)
            return {"offset": size + len(block)}

    def finish(self, agent_id: str, upload_id: str) -> dict:
        with self.controller.lock:
            self.store.get(agent_id)
            state = self.read(agent_id, upload_id)
            if state.status == "complete":
                if not any(
                    str(ref.id) == state.result["file"]["id"]
                    for ref in self.controller.files.read(agent_id).files
                ):
                    raise FileUnavailable("Completed upload reference was removed")
                return state.result
            if state.status not in {"receiving", "finishing"}:
                raise RuntimeError("Upload was cancelled or expired")
            path = self.payload(agent_id, state)
            with open_regular(path) as stream:
                info = os.fstat(stream.fileno())
                if (info.st_dev, info.st_ino) != (state.device, state.inode):
                    raise RuntimeError("Temporary upload file was replaced")
                if info.st_size != state.size:
                    raise RuntimeError("Upload is incomplete; file was not registered")
            state.status = "finishing"
            self.save(agent_id, state)
            # File registration is path-deduplicated, including after a lost finish response.
            result = self.controller.files.add(
                agent_id, FileRequest(path=str(path), label=state.label), uploaded=True
            )
            state.status, state.result = "complete", result
            self.save(agent_id, state)
            self.controller.refresh()
            return result

    def remove_upload(self, agent_id: str, file_id: str) -> dict:
        """An explicit removal deletes only the owned temporary copy, not client originals."""
        with self.controller.lock:
            self.store.get(agent_id)
            ref = next(
                (
                    r
                    for r in self.controller.files.read(agent_id).files
                    if str(r.id) == str(file_id)
                ),
                None,
            )
            if ref is None or not ref.uploaded:
                raise FileNotFoundError("Uploaded file is not registered for this agent")
            for owner, state in self.states():
                if str(owner) != str(agent_id) or state.status not in {
                    "receiving",
                    "finishing",
                    "complete",
                }:
                    continue
                path = self.payload(agent_id, state)
                if str(path) != ref.path:
                    continue
                path.unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass  # Never delete other files the agent may have created here.
                result = self.controller.files.remove(agent_id, file_id)
                state.status = "cancelled"
                self.save(agent_id, state)
                return result | {"deleted_upload": True}
            raise ValueError("No matching owned upload; refusing to delete a registered original")

    def forget(self, agent_id: str) -> None:
        with self.controller.lock:
            for owner, state in list(self.states()):
                if str(owner) != str(agent_id):
                    continue
                if state.status in {"cancelled", "expired"}:
                    self.state_path(agent_id, state.upload_id).unlink(missing_ok=True)
                    continue
                path = self.payload(agent_id, state)
                path.unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
                self.state_path(agent_id, state.upload_id).unlink(missing_ok=True)

    def cancel(self, agent_id: str, upload_id: str) -> dict:
        with self.controller.lock:
            state = self.read(agent_id, upload_id)
            path = self.payload(agent_id, state)
            if state.status == "complete" or self.registered(agent_id, path):
                return {"cancelled": False, "complete": True}
            if path.parent.exists():
                shutil.rmtree(path.parent)
            state.status = "cancelled"
            self.save(agent_id, state)
            return {"cancelled": True}
