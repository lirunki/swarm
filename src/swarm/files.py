"""Reference-only agent files. Persist paths, never payloads; read bytes only on click."""

from __future__ import annotations

import mimetypes
import os
import stat
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import BinaryIO
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import now
from .paths import local_path
from .products import MAX_PRODUCT_BYTES, open_regular
from .store import Store, atomic_write

MAX_AGENT_FILES = 64
MAX_INDEX_BYTES = 1024 * 1024
PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


class FileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    label: str = Field(default="", max_length=120)


class FileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(default_factory=uuid4)
    path: str = Field(min_length=1, max_length=4096, repr=False)
    filename: str = Field(min_length=1, max_length=255)
    label: str = Field(min_length=1, max_length=120)
    size: int = Field(ge=0)
    media_type: str
    uploaded: bool = False
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def valid(self):
        if not Path(self.path).is_absolute() or self.created_at.tzinfo is None:
            raise ValueError("reference needs an absolute path and timezone")
        if not self.label.strip() or any(ord(c) < 32 or ord(c) == 127 for c in self.filename):
            raise ValueError("invalid reference name")
        if (
            self.filename != Path(self.filename).name
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ValueError("reference filename must be a basename")
        return self


class FileIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    revision: int = Field(default=0, ge=0)
    files: list[FileReference] = Field(default_factory=list, max_length=MAX_AGENT_FILES)

    @model_validator(mode="after")
    def valid(self):
        if self.version != 1 or len({ref.id for ref in self.files}) != len(self.files):
            raise ValueError("invalid file reference index")
        return self


class FileUnavailable(RuntimeError):
    def __init__(self, message: str, status: int = 410):
        super().__init__(message)
        self.status = status


def preview_type(header: bytes) -> str:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


class AgentFiles:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.home / "files"
        self.cache: dict[str, tuple[tuple[int, int], FileIndex]] = {}

    def path(self, agent_id: str | UUID) -> Path:
        path = self.root / f"{UUID(str(agent_id))}.json"
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("file reference storage must not be a symbolic link")
        if self.root.exists() and not self.root.is_dir():
            raise ValueError("file reference storage is not a directory")
        if not path.resolve().is_relative_to(self.store.home):
            raise ValueError("file reference storage escapes Swarm home")
        return path

    def read(self, agent_id: str | UUID) -> FileIndex:
        path = self.path(agent_id)
        if not path.exists():
            self.cache.pop(str(agent_id), None)
            return FileIndex()
        info = path.stat()
        stamp = info.st_mtime_ns, info.st_size
        cached = self.cache.get(str(agent_id))
        if cached and cached[0] == stamp:
            return cached[1]
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INDEX_BYTES:
            raise ValueError("file reference index is invalid or too large")
        try:
            value = FileIndex.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("File references could not be read") from exc
        self.cache[str(agent_id)] = stamp, value
        return value

    def write(self, agent_id: str | UUID, index: FileIndex) -> None:
        content = index.model_dump_json() + "\n"
        if len(content.encode("utf-8")) > MAX_INDEX_BYTES:
            raise ValueError("file reference metadata exceeds the size limit")
        atomic_write(self.path(agent_id), content)
        self.cache.pop(str(agent_id), None)

    def summary(self, agent_id: str | UUID) -> dict:
        # No original-file stats/payload reads in the dashboard polling path.
        index = self.read(agent_id)
        return {"file_count": len(index.files), "files_version": index.revision}

    @staticmethod
    def info(ref: FileReference, check: bool = False) -> dict:
        value = ref.model_dump(mode="json", exclude={"path"})
        value.update(previewable=ref.media_type in PREVIEW_TYPES, available=True, error=None)
        if check:
            try:
                path = Path(ref.path)
                if path.resolve(strict=True) != path or path.is_symlink():
                    raise ValueError("File path was replaced by a symbolic link")
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Registered path is no longer a regular file")
                if info.st_size > MAX_PRODUCT_BYTES:
                    raise ValueError("File exceeds the 128 MiB limit")
                value["size"] = info.st_size
                value["modified_at"] = datetime.fromtimestamp(
                    info.st_mtime, now().tzinfo
                ).isoformat()
            except FileNotFoundError:
                value.update(available=False, error="Original file was moved or deleted")
            except (OSError, ValueError, RuntimeError):
                value.update(
                    available=False, error="Original file is unreadable, replaced or too large"
                )
        return value

    def list(self, agent_id: str | UUID) -> dict:
        self.store.get(agent_id)
        with self.store.lock(agent_id):
            index = self.read(agent_id)
        return {
            "files": [self.info(ref, True) for ref in index.files],
            "revision": index.revision,
        }

    def add(self, agent_id: str | UUID, request: FileRequest, *, uploaded: bool = False) -> dict:
        agent = self.store.get(agent_id)
        raw = request.path
        absolute = Path(raw).expanduser().is_absolute() or PureWindowsPath(raw).is_absolute()
        source = local_path(raw) if absolute else local_path(Path(agent.folder) / raw)
        source = source.resolve(strict=True)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("registered source must be a regular file")
        if info.st_size > MAX_PRODUCT_BYTES:
            raise ValueError("file exceeds the 128 MiB limit")
        # Registration only stats the source. It never opens/hashes/copies its bytes.
        with self.store.lock(agent_id):
            index = self.read(agent_id)
            previous = next((ref for ref in index.files if ref.path == str(source)), None)
            if previous is None and len(index.files) >= MAX_AGENT_FILES:
                raise ValueError("an agent can register at most 64 files")
            ref = FileReference(
                id=previous.id if previous else uuid4(),
                path=str(source),
                filename=source.name,
                label=request.label.strip() or (previous.label if previous else source.name[:120]),
                size=info.st_size,
                media_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                created_at=previous.created_at if previous else now(),
                uploaded=uploaded or bool(previous and previous.uploaded),
            )
            values = (
                [ref if old.id == ref.id else old for old in index.files]
                if previous
                else [*index.files, ref]
            )
            self.write(agent_id, FileIndex(revision=index.revision + 1, files=values))
            return {
                "agent_id": str(agent.id),
                "file": self.info(ref),
                "revision": index.revision + 1,
            }

    def remove(self, agent_id: str | UUID, file_id: str | UUID) -> dict:
        self.store.get(agent_id)
        selected = UUID(str(file_id))
        with self.store.lock(agent_id):
            index = self.read(agent_id)
            values = [ref for ref in index.files if ref.id != selected]
            if len(values) == len(index.files):
                raise FileNotFoundError("File is not registered for this agent")
            self.write(agent_id, FileIndex(revision=index.revision + 1, files=values))
        return {"removed": str(selected), "revision": index.revision + 1}

    def forget(self, agent_id: str | UUID) -> None:
        """Discard only reference metadata, never the registered originals."""
        with self.store.lock(agent_id):
            self.path(agent_id).unlink(missing_ok=True)
            self.cache.pop(str(agent_id), None)

    def reference(self, agent_id: str | UUID, file_id: str | UUID) -> FileReference:
        """Resolve a registered reference for explicit insertion, without reading its bytes."""
        self.store.get(agent_id)
        with self.store.lock(agent_id):
            ref = next((r for r in self.read(agent_id).files if r.id == UUID(str(file_id))), None)
            if ref is None:
                raise FileNotFoundError("File is not registered for this agent")
            info = self.info(ref, check=True)
            if not info["available"]:
                raise FileUnavailable(info["error"])
            return ref

    def open(
        self, agent_id: str | UUID, file_id: str | UUID
    ) -> tuple[FileReference, BinaryIO, int, str]:
        self.store.get(agent_id)
        with self.store.lock(agent_id):
            ref = next(
                (ref for ref in self.read(agent_id).files if ref.id == UUID(str(file_id))), None
            )
            if ref is None:
                raise FileNotFoundError("File is not registered for this agent")
            path = Path(ref.path)
            try:
                if path.resolve(strict=True) != path or path.is_symlink():
                    raise FileUnavailable("Registered file path was replaced")
                stream = open_regular(path)
            except FileNotFoundError as exc:
                raise FileUnavailable("Original file was moved or deleted") from exc
            except FileUnavailable:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                raise FileUnavailable("Original file is not a readable regular file") from exc
            try:
                info = os.fstat(stream.fileno())
                if info.st_size > MAX_PRODUCT_BYTES:
                    raise FileUnavailable("File exceeds the 128 MiB limit", 413)
                media_type = preview_type(stream.read(512))
                stream.seek(0)
                return ref, stream, info.st_size, media_type
            except Exception:
                stream.close()
                raise
