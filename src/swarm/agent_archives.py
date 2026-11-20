"""Private, transactional whole-agent archives.

Archives are data, never instructions: this module does not launch Pi, run Git, or
extract arbitrary ZIP paths.  Every payload is bounded, named by this module, and
covered by the archive manifest before an agent record can be forgotten.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .files import MAX_AGENT_FILES, MAX_INDEX_BYTES, FileIndex, FileReference
from .meta_compact import MAX_SOURCE_BYTES, MetaCompactor, digest, read_source, session_context
from .models import Agent, Status, now
from .paths import agent_path
from .products import MAX_PRODUCT_BYTES, open_regular
from .restarts import require_not_restarting
from .store import MAX_RECORD_BYTES, atomic_write, decode

if TYPE_CHECKING:
    from .controller import Controller

VERSION = 1
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ZIP_MEMBERS = 4096
# Idempotency is guaranteed for the 256 most recent durable completions; older
# request IDs expire when this bounded index ledger advances.
MAX_RECEIPTS = 256
MAX_JOBS = 255
BLOCK = 1024 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")
JOB_ID = re.compile(r"(toolrun|toolout)_([0-9a-f]{32})_[0-9a-f]{16}")
log = logging.getLogger(__name__)

# Explicitly selected referenced files may otherwise make a convenient credential
# exfiltration feature.  Exact session history is different: the operator explicitly
# requested an archive and the API warns that the private ZIP may contain secrets.
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "apikey.txt",
    "api-key.txt",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "password",
    "password.txt",
    "secrets.json",
    "service-account.json",
    "token",
    "token.txt",
}
_SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx")


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_files: bool = Field(default=False, strict=True)
    request_id: UUID


class ResurrectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class ArchiveCorrupt(ValueError):
    """The private index or ZIP cannot be trusted."""


class _ArchiveClosing(RuntimeError):
    """A resumable worker stopped for controller shutdown."""


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stream_digest(stream: BinaryIO) -> str:
    checksum = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(BLOCK):
        checksum.update(chunk)
    stream.seek(0)
    return checksum.hexdigest()


class _VerifiedPayloads(Mapping[str, bytes]):
    """Lazy, identity-bound access to members of an already verified ZIP."""

    def __init__(
        self,
        path: Path,
        identity: tuple[int, int, int, int, int],
        archive_sha256: str,
        descriptors: dict[str, dict[str, Any]],
    ) -> None:
        self._path = path
        self._identity = identity
        self._archive_sha256 = archive_sha256
        self._descriptors = descriptors

    def __len__(self) -> int:
        return len(self._descriptors)

    def __iter__(self) -> Iterator[str]:
        return iter(self._descriptors)

    def __getitem__(self, name: str) -> bytes:
        descriptor = self._descriptors[name]
        try:
            with open_regular(self._path) as source:
                if _file_identity(os.fstat(source.fileno())) != self._identity:
                    raise ArchiveCorrupt("Archive ZIP identity changed after verification")
                if _stream_digest(source) != self._archive_sha256:
                    raise ArchiveCorrupt("Archive ZIP bytes changed after verification")
                with zipfile.ZipFile(source, "r") as archive:
                    infos = [info for info in archive.infolist() if info.filename == name]
                    if len(infos) != 1:
                        raise ArchiveCorrupt("Verified archive member is no longer unique")
                    info = infos[0]
                    data = bytearray()
                    checksum = hashlib.sha256()
                    with archive.open(info, "r") as member:
                        while chunk := member.read(BLOCK):
                            if len(data) + len(chunk) > descriptor["size"]:
                                raise ArchiveCorrupt("Archive member exceeds its verified size")
                            data.extend(chunk)
                            checksum.update(chunk)
                if _file_identity(os.fstat(source.fileno())) != self._identity:
                    raise ArchiveCorrupt("Archive ZIP changed while reading a member")
        except (OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
            if isinstance(exc, ArchiveCorrupt):
                raise
            raise ArchiveCorrupt("Archive member could not be read safely") from exc
        if len(data) != descriptor["size"] or checksum.hexdigest() != descriptor["sha256"]:
            raise ArchiveCorrupt("Archive member changed after verification")
        return bytes(data)


class _Budget:
    def __init__(self) -> None:
        self.total = 0
        self.members = 0

    def add(self, size: int, name: str, limit: int = MAX_MEMBER_BYTES) -> None:
        if size < 0 or size > limit:
            raise ValueError(f"Archive artifact {name} exceeds its safety limit")
        if self.members >= MAX_ZIP_MEMBERS:
            raise ValueError("Archive contains too many artifacts")
        if self.total + size > MAX_TOTAL_BYTES:
            raise ValueError("Archive artifacts exceed the 512 MiB total limit")
        self.members += 1
        self.total += size


def _json(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _payload_hash(value: object) -> str:
    return digest(_json(value))


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:255]


def _regular_bytes(path: Path, limit: int, label: str) -> bytes:
    try:
        with open_regular(path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > limit:
                raise ValueError(f"{label} exceeds its safety limit")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
    except FileNotFoundError:
        raise ValueError(f"{label} is missing") from None
    if len(data) > limit:
        raise ValueError(f"{label} exceeds its safety limit")
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError(f"{label} changed while it was being archived")
    return data


def _regular_digest(path: Path, limit: int, label: str) -> str:
    try:
        with open_regular(path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > limit:
                raise ValueError(f"{label} exceeds its safety limit")
            checksum = hashlib.sha256()
            count = 0
            while chunk := stream.read(BLOCK):
                count += len(chunk)
                if count > limit:
                    raise ValueError(f"{label} exceeds its safety limit")
                checksum.update(chunk)
            after = os.fstat(stream.fileno())
    except FileNotFoundError:
        raise ValueError(f"{label} is missing") from None
    if _file_identity(before) != _file_identity(after):
        raise RuntimeError(f"{label} changed while it was being read")
    return checksum.hexdigest()


def _source_snapshot(path: Path) -> tuple[bytes, dict[str, int]]:
    """Read one stable regular-file generation without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ValueError("Session is missing") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
                raise ValueError("Session must be a regular file at most 128 MiB")
            data = stream.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(stream.fileno())
    except Exception:
        raise
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Session exceeds the 128 MiB archive limit")
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise RuntimeError("Session changed while it was being captured")
    return data, {
        "device": before.st_dev,
        "inode": before.st_ino,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "ctime_ns": before.st_ctime_ns,
    }


def _metadata_stat(path: Path) -> dict[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise ValueError("Session must be a regular file at most 128 MiB")
        return {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
    finally:
        os.close(fd)


def _secret_name(filename: str) -> bool:
    lowered = filename.casefold()
    return (
        lowered in _SECRET_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith(_SECRET_SUFFIXES)
        or "credential" in lowered
        or "private-key" in lowered
    )


def _mkdir_private(path: Path, boundary: Path) -> None:
    """Create canonical controller-owned directories without following symlinks."""
    path, boundary = path.absolute(), boundary.absolute()
    if not path.is_relative_to(boundary):
        raise ValueError("Private archive storage escapes Swarm home")
    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Private archive storage must not use symbolic links")
        if current.exists():
            info = current.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Private archive storage path is not a directory")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ValueError("Private archive storage belongs to another user")
        else:
            current.mkdir(mode=0o700)
        os.chmod(current, 0o700)


def _member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ArchiveCorrupt("Archive contains an invalid member name")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveCorrupt("Archive contains an unsafe member path")
    return path.as_posix()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _session_hash(session_id: str) -> str:
    return digest(session_id.encode("utf-8"))[:24]


def _dependency_roots(
    home: Path, agent: Agent, source: Path, data: bytes
) -> list[tuple[Path, str]]:
    """Return only roots deterministically owned/referenced by this saved session."""
    roots: list[tuple[Path, str]] = []
    if agent.session_id:
        context_key = _session_hash(agent.session_id)
        spill_key = digest(agent.session_id.encode("utf-8"))[:16]
        roots.extend(
            [
                (source.parent / "context-archives" / context_key, "session/context-archives"),
                (
                    source.parent / "output-spills" / f"session-{spill_key}",
                    "session/output-spills",
                ),
            ]
        )
    text = data.decode("utf-8", errors="ignore")
    referenced = {(match.group(1), match.group(2)) for match in JOB_ID.finditer(text)}
    for kind, job_id in sorted(referenced):
        parent, prefix, collection = (
            (home / "tool-input-compactions", "global/tool-input", "calls")
            if kind == "toolrun"
            else (home / "tool-output-repairs", "global/tool-output", "outputs")
        )
        root = parent / job_id
        if not root.exists():
            continue
        if parent.is_symlink() or root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError("Referenced tool archive is not canonical")
        index_data = _regular_bytes(root / "index.json", MAX_MANIFEST_BYTES, "Tool archive index")
        try:
            index = json.loads(index_data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Referenced tool archive index is invalid") from exc
        expected_source = str(source)
        source_sha = index.get("source_sha256") if isinstance(index, dict) else None
        if (
            not isinstance(index, dict)
            or index.get("source") != expected_source
            or not isinstance(source_sha, str)
            or not HEX64.fullmatch(source_sha)
            or not isinstance(index.get(collection), dict)
        ):
            raise ValueError("Referenced tool archive is not owned by this session")
        original = _regular_bytes(root / "original.jsonl", MAX_SOURCE_BYTES, "Tool archive source")
        if digest(original) != source_sha:
            raise ValueError("Referenced tool archive source checksum is invalid")
        job_path = root / "job.json"
        if job_path.exists():
            try:
                report = json.loads(_regular_bytes(job_path, MAX_MANIFEST_BYTES, "Tool job record"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("Referenced tool job record is invalid") from exc
            if (
                isinstance(report, dict)
                and report.get("session_id") is not None
                and report.get("session_id") != agent.session_id
            ):
                raise ValueError("Referenced tool archive belongs to another session")
        roots.append((root, f"{prefix}/{job_id}"))
    return roots


def _tree_payloads(root: Path, prefix: str) -> Iterator[tuple[str, bytes, str]]:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise ValueError("Session dependency storage must be a canonical directory")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("Session dependencies must not contain symbolic links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Session dependencies must contain only regular files")
        member = f"dependencies/{prefix}/{relative.as_posix()}"
        yield member, _regular_bytes(path, MAX_MEMBER_BYTES, "Session dependency"), str(path)


class AgentArchives:
    def __init__(self, guard: MetaCompactor):
        self.guard = guard
        self.controller: Controller = guard.controller
        self.store = guard.store
        self.root = self.store.home / "archives"
        self.index_path = self.root / "index.json"
        self.jobs = self.root / "jobs"
        self.lock_path = self.store.locks_dir / "agent-archives.lock"
        self._thread_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-archive")
        self._closing = threading.Event()
        self._cleanup: Callable[[str], None] | None = None
        self._pending_upload: Callable[[str], bool] | None = None

    def set_cleanup(self, cleanup: Callable[[str], None]) -> None:
        self._cleanup = cleanup

    def set_pending_upload(self, pending: Callable[[str], bool]) -> None:
        self._pending_upload = pending

    def close(self) -> None:
        self._closing.set()
        self._executor.shutdown(wait=False, cancel_futures=False)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self._thread_lock:
            self.store.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ArchiveCorrupt("Archive lock is not a regular file")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _path(self, archive_id: str | UUID) -> Path:
        selected = str(UUID(str(archive_id)))
        path = self.root / f"{selected}.zip"
        if any(part.is_symlink() for part in (self.root, path)):
            raise ArchiveCorrupt("Archive storage must not use symbolic links")
        if not path.resolve().is_relative_to(self.store.home.resolve()):
            raise ArchiveCorrupt("Archive storage escapes Swarm home")
        return path

    def _empty(self) -> dict[str, Any]:
        return {"version": VERSION, "archives": [], "receipts": []}

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty()
        if self.root.is_symlink() or self.index_path.is_symlink():
            raise ArchiveCorrupt("Archive index must not use symbolic links")
        info = self.index_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise ArchiveCorrupt("Archive index is invalid or exceeds 1 MiB")
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveCorrupt("Archive index could not be read safely") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "archives", "receipts"}
            or value.get("version") != VERSION
            or not isinstance(value.get("archives"), list)
            or not isinstance(value.get("receipts"), list)
            or len(value["receipts"]) > MAX_RECEIPTS
        ):
            raise ArchiveCorrupt("Archive index has an invalid format")
        ids: set[str] = set()
        for record in value["archives"]:
            try:
                archive_id = str(UUID(record["id"]))
                agent_id = str(UUID(record["agent_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveCorrupt("Archive index contains invalid identities") from exc
            if archive_id in ids or archive_id != record["id"] or agent_id != record["agent_id"]:
                raise ArchiveCorrupt("Archive index contains duplicate or noncanonical identities")
            ids.add(archive_id)
        return value

    def _write_index(self, value: dict[str, Any]) -> None:
        payload = _json(value, pretty=True)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("Archive index exceeds the 1 MiB metadata limit")
        _mkdir_private(self.root, self.store.home)
        atomic_write(self.index_path, payload)
        os.chmod(self.index_path, 0o600)

    @staticmethod
    def _find(index: dict[str, Any], archive_id: str) -> dict[str, Any]:
        record = next((item for item in index["archives"] if item.get("id") == archive_id), None)
        if record is None:
            raise FileNotFoundError("Agent archive was not found")
        return record

    @staticmethod
    def _receipt(index: dict[str, Any], request_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in index["receipts"] if item.get("request_id") == request_id), None
        )

    def _save_receipt(
        self,
        index: dict[str, Any],
        request_id: str,
        operation: str,
        archive_id: str,
        payload_hash: str,
    ) -> None:
        receipt = {
            "request_id": request_id,
            "operation": operation,
            "archive_id": archive_id,
            "payload_hash": payload_hash,
            "completed_at": now().isoformat(),
        }
        index["receipts"] = [
            item for item in index["receipts"] if item.get("request_id") != request_id
        ][-(MAX_RECEIPTS - 1) :] + [receipt]

    def _job_path(self, request_id: str) -> Path:
        return self.jobs / f"{UUID(request_id)}.json"

    def _read_job(self, request_id: str) -> dict[str, Any] | None:
        path = self._job_path(request_id)
        if not path.exists():
            return None
        if self.jobs.is_symlink() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ArchiveCorrupt("Archive job metadata is invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveCorrupt("Archive job metadata could not be read") from exc
        if not isinstance(value, dict) or value.get("request_id") != request_id:
            raise ArchiveCorrupt("Archive job identity is invalid")
        return value

    def _save_job(self, request_id: str, value: dict[str, Any]) -> None:
        _mkdir_private(self.jobs, self.store.home)
        path = self._job_path(request_id)
        payload = _json(value, pretty=True)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("Archive job metadata exceeds 1 MiB")
        atomic_write(path, payload)
        os.chmod(path, 0o600)

        # Active jobs are transaction records, not display history. Never evict one merely
        # because many other requests completed while it was queued or recovering.
        finished: list[Path] = []
        for candidate in self.jobs.glob("*.json"):
            try:
                report = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(report, dict) and report.get("status") in {"completed", "failed"}:
                finished.append(candidate)
        finished.sort(key=lambda item: item.stat().st_mtime_ns)
        for old in finished[:-MAX_JOBS]:
            # Snapshot names share the request UUID. Since only terminal metadata is
            # selected, none of these files can be referenced by a live job.
            for artifact in self.jobs.glob(f"{old.stem}.*"):
                artifact.unlink(missing_ok=True)

    def _check_open(self) -> None:
        if self._closing.is_set():
            raise _ArchiveClosing("Agent archive service is shutting down")

    def _build(
        self,
        agent: Agent,
        agent_md: bytes,
        source: Path,
        source_data: bytes,
        include_files: bool,
        archive_id: str,
        file_index: FileIndex,
    ) -> tuple[dict[str, Any], bytes]:
        self._check_open()
        record_path = self.store.path(agent.id)
        exact = decode(agent_md.decode("utf-8"))
        if exact != agent or exact.id != agent.id:
            raise RuntimeError("Captured agent metadata does not match its immutable model")

        budget = _Budget()
        payloads: dict[str, bytes] = {}
        paths: dict[str, str] = {}

        def add(name: str, data: bytes, original: str = "", limit: int = MAX_MEMBER_BYTES) -> None:
            name = _member_name(name)
            if name in payloads:
                raise ValueError("Archive member name collision")
            budget.add(len(data), name, limit)
            payloads[name] = data
            if original:
                paths[name] = original

        add("agent.md", agent_md, str(record_path), MAX_RECORD_BYTES)
        add("session.jsonl", source_data, str(source), MAX_SOURCE_BYTES)

        references = []
        missing_files: list[dict[str, str]] = []
        if len(file_index.files) > MAX_AGENT_FILES:
            raise ValueError("Agent has too many file references")
        for ref in file_index.files:
            self._check_open()
            path = Path(ref.path)
            include_payload = bool(ref.uploaded or include_files)
            if include_payload:
                data = _regular_bytes(path, MAX_PRODUCT_BYTES, f"Referenced file {ref.filename}")
                if include_files and not ref.uploaded and _secret_name(ref.filename):
                    raise ValueError(
                        f"Refusing to include credential-like referenced file {ref.filename!r}; "
                        "unregister it or archive without file bytes"
                    )
                member = f"files/{ref.id}/payload"
                add(member, data, str(path), MAX_PRODUCT_BYTES)
                checksum: str | None = digest(data)
            else:
                # Reference-only archival never opens ordinary originals. Retain only a
                # currently canonical regular path whose size still matches its metadata;
                # resurrection repeats this check rather than creating a dangling entry.
                try:
                    info = path.stat(follow_symlinks=False)
                    available = (
                        not path.is_symlink()
                        and stat.S_ISREG(info.st_mode)
                        and info.st_size == ref.size
                        and path.resolve(strict=True) == path
                    )
                except (OSError, RuntimeError):
                    available = False
                if not available:
                    missing_files.append(
                        {"id": str(ref.id), "filename": ref.filename, "reason": "unavailable"}
                    )
                    continue
                member = None
                checksum = None
            references.append(
                ref.model_dump(mode="json") | {"sha256": checksum, "archive_member": member}
            )
        add(
            "file_reference.json",
            _json(
                {
                    "version": 1,
                    "revision": file_index.revision,
                    "files": references,
                    "missing_files": missing_files,
                },
                pretty=True,
            ),
            limit=MAX_INDEX_BYTES,
        )

        product_member = None
        if include_files and agent.product and agent.product.kind == "file":
            product_path = (
                self.store.home / "products" / str(agent.id) / str(agent.product.id) / "payload"
            )
            product_data = _regular_bytes(product_path, MAX_PRODUCT_BYTES, "Product snapshot")
            if (
                len(product_data) != agent.product.size
                or digest(product_data) != agent.product.sha256
            ):
                raise ValueError("Product snapshot does not match agent metadata")
            if _secret_name(agent.product.filename or ""):
                raise ValueError("Refusing to include a credential-like product filename")
            product_member = "product/payload"
            add(product_member, product_data, str(product_path), MAX_PRODUCT_BYTES)

        dependency_roots: list[dict[str, Any]] = []
        for root, prefix in _dependency_roots(self.store.home, agent, source, source_data):
            files: list[dict[str, Any]] = []
            for member, data, original in _tree_payloads(root, prefix):
                self._check_open()
                add(member, data, original)
                files.append({"member": member, "sha256": digest(data), "size": len(data)})
            dependency_roots.append({"root": str(root), "prefix": prefix, "files": files})

        archived_at = now().isoformat()
        marker = _json({"version": VERSION, "archive_id": archive_id, "complete": True})
        add("complete.marker", marker, limit=4096)
        manifest: dict[str, Any] = {
            "version": VERSION,
            "archive_id": archive_id,
            "agent_id": str(agent.id),
            "created_at": agent.created_at.isoformat(),
            "archived_at": archived_at,
            "session_id": agent.session_id,
            "source_session": {
                "path": str(source),
                "sha256": digest(source_data),
                "size": len(source_data),
            },
            "include_files": include_files,
            "missing_files": missing_files,
            "product_member": product_member,
            "dependency_roots": dependency_roots,
            "paths": paths,
            "members": {
                name: {"sha256": digest(data), "size": len(data)}
                for name, data in sorted(payloads.items())
            },
        }
        manifest_data = _json(manifest, pretty=True)
        budget.add(len(manifest_data), "manifest.json", MAX_MANIFEST_BYTES)
        payloads["manifest.json"] = manifest_data

        _mkdir_private(self.root, self.store.home)
        fd, temp_name = tempfile.mkstemp(prefix=f".{archive_id}.", suffix=".tmp", dir=self.root)
        try:
            os.close(fd)
            with zipfile.ZipFile(
                temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
            ) as archive:
                for name, data in sorted(payloads.items()):
                    self._check_open()
                    archive.writestr(_zip_info(name), data)
            with open(temp_name, "rb") as stream:
                os.fsync(stream.fileno())
            os.chmod(temp_name, 0o600)
            destination = self._path(archive_id)
            os.replace(temp_name, destination)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temp_name).unlink(missing_ok=True)

        summary = {
            "id": archive_id,
            "name": agent.name,
            "agent_id": str(agent.id),
            "created_at": agent.created_at.isoformat(),
            "archived_at": archived_at,
            "last_accessed_at": None,
            "last_resurrected_at": None,
            "status": "archived",
            "goal": agent.goal,
            "folder": agent.folder,
            "session_id": agent.session_id,
            "size": self._path(archive_id).stat().st_size,
            "files_included": include_files,
            "restorable": True,
        }
        return summary, agent_md

    @staticmethod
    def _allowed_member(name: str) -> bool:
        return (
            name
            in {
                "agent.md",
                "session.jsonl",
                "file_reference.json",
                "product/payload",
                "complete.marker",
                "manifest.json",
            }
            or name.startswith("files/")
            or name.startswith("dependencies/")
        )

    def _verify(
        self, archive_id: str, source: BinaryIO | None = None
    ) -> tuple[dict[str, Any], Mapping[str, bytes]]:
        path = self._path(archive_id)
        owned = source is None
        if owned:
            try:
                source = open_regular(path)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ArchiveCorrupt("Archive ZIP is missing or is not a regular file") from exc
        assert source is not None
        try:
            identity = _file_identity(os.fstat(source.fileno()))
            archive_sha256 = _stream_digest(source)
            total = 0
            with zipfile.ZipFile(source, "r") as archive:
                infos = archive.infolist()
                if not 1 <= len(infos) <= MAX_ZIP_MEMBERS:
                    raise ArchiveCorrupt("Archive has an invalid member count")
                names: set[str] = set()
                by_name: dict[str, zipfile.ZipInfo] = {}
                for info in infos:
                    name = _member_name(info.filename)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if (
                        name in names
                        or not self._allowed_member(name)
                        or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or (mode and not stat.S_ISREG(mode))
                    ):
                        raise ArchiveCorrupt("Archive contains a duplicate or unsupported member")
                    limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_MEMBER_BYTES
                    if info.file_size > limit or total + info.file_size > MAX_TOTAL_BYTES:
                        raise ArchiveCorrupt("Archive exceeds extraction safety limits")
                    names.add(name)
                    by_name[name] = info
                    total += info.file_size
                if "manifest.json" not in names:
                    raise ArchiveCorrupt("Archive manifest is missing")

                manifest_data = bytearray()
                with archive.open(by_name["manifest.json"], "r") as member:
                    while chunk := member.read(BLOCK):
                        if len(manifest_data) + len(chunk) > MAX_MANIFEST_BYTES:
                            raise ArchiveCorrupt("Archive manifest exceeds 1 MiB")
                        manifest_data.extend(chunk)
                if len(manifest_data) != by_name["manifest.json"].file_size:
                    raise ArchiveCorrupt("Archive manifest length is invalid")
                try:
                    manifest = json.loads(manifest_data)
                    expected = manifest["members"]
                    UUID(manifest["archive_id"])
                    UUID(manifest["agent_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ArchiveCorrupt("Archive manifest is invalid") from exc

                required = {
                    "agent.md",
                    "session.jsonl",
                    "file_reference.json",
                    "complete.marker",
                    "manifest.json",
                }
                if (
                    not isinstance(expected, dict)
                    or not required.issubset(names)
                    or manifest.get("version") != VERSION
                    or manifest["archive_id"] != archive_id
                    or set(expected) != names - {"manifest.json"}
                ):
                    raise ArchiveCorrupt("Archive manifest identity or source binding is invalid")
                descriptors: dict[str, dict[str, Any]] = {}
                for name, info in by_name.items():
                    checksum = hashlib.sha256()
                    count = 0
                    with archive.open(info, "r") as member:
                        while chunk := member.read(BLOCK):
                            count += len(chunk)
                            if count > info.file_size:
                                raise ArchiveCorrupt("Archive member exceeds its declared size")
                            checksum.update(chunk)
                    if count != info.file_size:
                        raise ArchiveCorrupt("Archive member length or CRC is invalid")
                    actual = {"sha256": checksum.hexdigest(), "size": count}
                    if name != "manifest.json":
                        descriptor = expected.get(name)
                        if (
                            not isinstance(descriptor, dict)
                            or set(descriptor) != {"sha256", "size"}
                            or not isinstance(descriptor.get("sha256"), str)
                            or not HEX64.fullmatch(descriptor["sha256"])
                            or descriptor != actual
                        ):
                            raise ArchiveCorrupt("Archive member checksum validation failed")
                    descriptors[name] = actual
            if (
                _file_identity(os.fstat(source.fileno())) != identity
                or _stream_digest(source) != archive_sha256
            ):
                raise ArchiveCorrupt("Archive ZIP changed during verification")
        except ArchiveCorrupt:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
            raise ArchiveCorrupt("Archive ZIP failed CRC or format validation") from exc
        finally:
            if owned:
                source.close()

        payloads = _VerifiedPayloads(path, identity, archive_sha256, descriptors)
        source_descriptor = expected.get("session.jsonl")
        if manifest.get("source_session", {}).get("sha256") != source_descriptor.get(
            "sha256"
        ) or manifest.get("source_session", {}).get("size") != source_descriptor.get("size"):
            raise ArchiveCorrupt("Archive manifest source binding is invalid")
        dependency_members: set[str] = set()
        roots = manifest.get("dependency_roots")
        if not isinstance(roots, list):
            raise ArchiveCorrupt("Archive dependency manifest is invalid")
        for root in roots:
            if (
                not isinstance(root, dict)
                or set(root) != {"root", "prefix", "files"}
                or not isinstance(root["root"], str)
                or not isinstance(root["prefix"], str)
                or not isinstance(root["files"], list)
            ):
                raise ArchiveCorrupt("Archive dependency root is invalid")
            for descriptor in root["files"]:
                if not isinstance(descriptor, dict) or set(descriptor) != {
                    "member",
                    "sha256",
                    "size",
                }:
                    raise ArchiveCorrupt("Archive dependency file descriptor is invalid")
                name = descriptor["member"]
                if (
                    not isinstance(name, str)
                    or not name.startswith(f"dependencies/{root['prefix']}/")
                    or name in dependency_members
                    or expected.get(name)
                    != {
                        "sha256": descriptor["sha256"],
                        "size": descriptor["size"],
                    }
                ):
                    raise ArchiveCorrupt("Archive dependency binding is invalid")
                dependency_members.add(name)
        if dependency_members != {name for name in payloads if name.startswith("dependencies/")}:
            raise ArchiveCorrupt("Archive dependency files are not fully described")
        try:
            marker = json.loads(payloads["complete.marker"])
            agent = decode(payloads["agent.md"].decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ArchiveCorrupt("Archive marker or agent metadata is invalid") from exc
        if (
            marker != {"archive_id": archive_id, "complete": True, "version": VERSION}
            or str(agent.id) != manifest["agent_id"]
            or agent.session_id != manifest.get("session_id")
        ):
            raise ArchiveCorrupt("Archive completion marker or agent identity is invalid")
        return manifest, payloads

    def _snapshot_path(self, request_id: str, suffix: str) -> Path:
        return self.jobs / f"{UUID(request_id)}.{suffix}"

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id",
            "archive_id",
            "agent_id",
            "name",
            "status",
            "include_files",
            "source",
            "source_sha256",
            "source_size",
            "session_id",
            "created_at",
            "started_at",
            "completed_at",
            "error",
            "archive",
        )
        return {key: job[key] for key in fields if key in job}

    def _job_matches(self, job: dict[str, Any], agent_id: str, payload_hash: str) -> bool:
        return (
            job.get("operation") == "archive"
            and job.get("agent_id") == agent_id
            and job.get("payload_hash") == payload_hash
        )

    def get_job(self, request_id: str | UUID) -> dict[str, Any]:
        selected = str(UUID(str(request_id)))
        with self._lock():
            index = self._read_index()
            receipt = self._receipt(index, selected)
            if receipt is not None:
                record = self._find(index, str(receipt.get("archive_id", "")))
                return self._public_job(self._receipt_job(receipt, record))
            job = self._read_job(selected)
            if job is None:
                raise FileNotFoundError("Agent archive job was not found")
            return self._public_job(job)

    @staticmethod
    def _receipt_job(receipt: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": VERSION,
            "id": receipt["request_id"],
            "request_id": receipt["request_id"],
            "payload_hash": receipt["payload_hash"],
            "operation": receipt["operation"],
            "archive_id": receipt["archive_id"],
            "agent_id": record["agent_id"],
            "name": record.get("name", ""),
            "status": "completed",
            "completed_at": receipt.get("completed_at"),
            "archive": dict(record),
        }

    def _reserve(
        self, agent_id: str, request: ArchiveRequest, *, submit: bool
    ) -> tuple[dict[str, Any], bool]:
        selected_agent = str(UUID(str(agent_id)))
        request_id = str(request.request_id)
        payload_hash = _payload_hash(
            {
                "operation": "archive",
                "agent_id": selected_agent,
                "include_files": request.include_files,
            }
        )
        with self.controller.lock:
            if self._closing.is_set():
                raise RuntimeError("Agent archive service is shutting down")
            with self._lock():
                index = self._read_index()
                receipt = self._receipt(index, request_id)
                if receipt is not None:
                    if (
                        receipt.get("operation") != "archive"
                        or receipt.get("payload_hash") != payload_hash
                    ):
                        raise RuntimeError("request_id was already used for a different operation")
                    record = self._find(index, str(receipt.get("archive_id", "")))
                    if record.get("agent_id") != selected_agent:
                        raise ArchiveCorrupt("Archive receipt ownership is invalid")
                    return self._receipt_job(receipt, record), False
                previous = self._read_job(request_id)
                if previous is not None:
                    if not self._job_matches(previous, selected_agent, payload_hash):
                        raise RuntimeError("request_id was already used for a different operation")
                    return dict(previous), False

            agent = self.store.get(selected_agent)
            require_not_restarting(agent)
            lease = getattr(agent, "agent_archive", None)
            if isinstance(lease, dict) and lease.get("status") in {"queued", "running"}:
                raise RuntimeError("An archive job is already queued or running for this agent")
            source = self.guard.eligible(agent, read_snapshot=False)
            if agent.status == Status.running:
                raise RuntimeError("Stop the agent before archiving it")
            if agent.pid is not None or self.controller.processes.alive(agent):
                raise RuntimeError("Stop the agent before archiving it")
            if self._pending_upload is not None and self._pending_upload(selected_agent):
                raise RuntimeError("Finish or cancel the agent's pending upload before archiving")
            with self.store.lock(agent.id):
                # Capture only small metadata and source identity while reserving. The
                # background worker owns all session reads and hashing.
                agent = self.store.get(agent.id)
                exact_record = _regular_bytes(
                    self.store.path(agent.id), MAX_RECORD_BYTES, "Agent metadata"
                )
                if decode(exact_record.decode("utf-8")) != agent:
                    raise RuntimeError("Agent metadata changed while reserving the archive")
                source_identity = _metadata_stat(source)

            archive_id = str(uuid4())
            created_at = now().isoformat()
            job = {
                "version": VERSION,
                "id": request_id,
                "request_id": request_id,
                "payload_hash": payload_hash,
                "operation": "archive",
                "agent_id": selected_agent,
                "archive_id": archive_id,
                "name": agent.name,
                "status": "queued",
                "include_files": request.include_files,
                "source": str(source),
                "source_stat": source_identity,
                "source_size": source_identity["size"],
                "session_id": agent.session_id,
                "created_at": created_at,
            }
            reservation = {
                "request_id": request_id,
                "archive_id": archive_id,
                "status": "queued",
                "source": str(source),
                "session_id": agent.session_id,
                "source_stat": source_identity,
                "created_at": created_at,
            }
            _mkdir_private(self.jobs, self.store.home)
            atomic_write(self._snapshot_path(request_id, "agent.md"), exact_record)
            os.chmod(self._snapshot_path(request_id, "agent.md"), 0o600)
            with self._lock():
                self._save_job(request_id, job)
            self.store.update(
                agent.id,
                {
                    "agent_archive": reservation,
                    "auto_resume": False,
                    "pid": None,
                },
            )
            self.controller.refresh()

        if submit:
            try:
                self._executor.submit(self._run, request_id, False)
            except Exception:
                self._fail_job(
                    request_id, RuntimeError("Could not start background archive worker")
                )
                raise
        return job, True

    def start(self, agent_id: str, request: ArchiveRequest) -> dict[str, Any]:
        job, _ = self._reserve(agent_id, request, submit=True)
        return {
            "job": self._public_job(job),
            **({"archive": dict(job["archive"])} if isinstance(job.get("archive"), dict) else {}),
        }

    def _set_running(self, request_id: str) -> dict[str, Any]:
        with self.controller.lock:
            with self._lock():
                job = self._read_job(request_id)
                if job is None:
                    raise FileNotFoundError("Agent archive job was not found")
            if job.get("status") == "completed":
                return job
            if job.get("status") == "failed":
                raise RuntimeError("Archive job has already failed")
            agent = self.store.get(job["agent_id"])
            lease = getattr(agent, "agent_archive", None)
            if not isinstance(lease, dict) or lease.get("request_id") != request_id:
                raise RuntimeError("Agent archive reservation ownership changed")
            if lease.get("archive_id") != job.get("archive_id"):
                raise RuntimeError("Agent archive reservation identity changed")
            lease = dict(lease) | {"status": "running"}
            self.store.update(agent.id, {"agent_archive": lease})
            running_record = self.store.path(agent.id).read_bytes()
            job.update(
                status="running",
                started_at=job.get("started_at") or now().isoformat(),
                leased_record_sha256=digest(running_record),
            )
            with self._lock():
                self._save_job(request_id, job)
            self.controller.refresh()
            return job

    def _file_index_snapshot(self, agent_id: str) -> tuple[FileIndex, str]:
        path = self.controller.files.path(agent_id)
        if not path.exists():
            return FileIndex(), digest(b"")
        if path.is_symlink():
            raise ValueError("File reference index must not be a symbolic link")
        data = _regular_bytes(path, MAX_INDEX_BYTES, "File reference index")
        try:
            index = FileIndex.model_validate_json(data)
        except ValueError as exc:
            raise ValueError("File references could not be read") from exc
        return index, digest(data)

    def _validate_final_ownership(
        self, agent: Agent, job: dict[str, Any], source_data: bytes, exact_record: bytes
    ) -> None:
        """Validate source and file-index generations under the record sidecar lock."""
        original = decode(exact_record.decode("utf-8"))
        if (
            agent.session_id != original.session_id
            or agent.session_file != original.session_file
            or agent.pid is not None
            or agent.auto_resume
            or self.controller.processes.alive(agent)
        ):
            raise RuntimeError("Stopped agent state changed while archival was in progress")
        source = Path(job["source"]).expanduser().absolute()
        current_source, current_stat = _source_snapshot(source)
        if (
            current_stat != job.get("source_stat")
            or current_source != source_data
            or digest(current_source) != job.get("source_sha256")
        ):
            raise RuntimeError("Session changed while archival was in progress")
        _, current_index_sha = self._file_index_snapshot(str(agent.id))
        if current_index_sha != job.get("file_index_sha256"):
            raise RuntimeError("File references changed while archival was in progress")

    def _complete_job(
        self,
        job: dict[str, Any],
        summary: dict[str, Any],
        source_data: bytes,
        exact_record: bytes,
    ) -> dict[str, Any]:
        request_id = job["request_id"]
        with self.controller.lock:
            removed = self.controller.forget(
                job["agent_id"],
                _archive_owner={
                    "request_id": job["request_id"],
                    "archive_id": job["archive_id"],
                },
                _expected_record_sha=job["leased_record_sha256"],
                _archive_check=lambda current: self._validate_final_ownership(
                    current, job, source_data, exact_record
                ),
            )
            if str(removed.id) != job["agent_id"]:
                raise RuntimeError("Forgotten agent identity did not match the verified archive")

        with self._lock():
            index = self._read_index()
            record = next(
                (item for item in index["archives"] if item.get("id") == summary["id"]), None
            )
            if record is None:
                record = dict(summary)
                index["archives"].append(record)
            record.update(status="archived", restorable=True)
            record.pop("error", None)
            self._save_receipt(index, request_id, "archive", job["archive_id"], job["payload_hash"])
            self._write_index(index)
            job.update(
                status="completed",
                completed_at=now().isoformat(),
                archive=dict(record),
            )
            job.pop("error", None)
            self._save_job(request_id, job)
        if self._cleanup is not None:
            try:
                self._cleanup(job["agent_id"])
            except Exception:
                log.warning("Could not clean archived agent artifacts", exc_info=True)
        return dict(record)

    def _finalize_forgotten(self, job: dict[str, Any]) -> dict[str, Any]:
        """Commit a verified job when a crash happened after the owned forget."""
        manifest, _ = self._verify(job["archive_id"])
        summary = job.get("summary")
        if (
            job.get("phase") != "archive_verified"
            or manifest.get("agent_id") != job.get("agent_id")
            or not isinstance(summary, dict)
            or summary.get("id") != job.get("archive_id")
        ):
            raise ArchiveCorrupt("Interrupted archive job is not safely finalizable")
        with self._lock():
            index = self._read_index()
            record = next(
                (item for item in index["archives"] if item.get("id") == summary["id"]), None
            )
            if record is None:
                record = dict(summary)
                index["archives"].append(record)
            record.update(status="archived", restorable=True)
            self._save_receipt(
                index,
                job["request_id"],
                "archive",
                job["archive_id"],
                job["payload_hash"],
            )
            self._write_index(index)
            job.update(status="completed", completed_at=now().isoformat(), archive=dict(record))
            job.pop("error", None)
            self._save_job(job["request_id"], job)
        if self._cleanup is not None:
            try:
                self._cleanup(job["agent_id"])
            except Exception:
                log.warning("Could not clean recovered archive artifacts", exc_info=True)
        return dict(record)

    def _fail_job(self, request_id: str, exc: BaseException) -> None:
        error = _safe_error(exc)
        with self._lock():
            job = self._read_job(request_id)
            if job is None or job.get("status") == "completed":
                return
        try:
            with self.controller.lock:
                agent = self.store.get(job["agent_id"])
                lease = getattr(agent, "agent_archive", None)
                if isinstance(lease, dict) and lease.get("request_id") == request_id:
                    self.store.update(
                        agent.id,
                        {"agent_archive": dict(lease) | {"status": "failed", "error": error}},
                    )
                    self.controller.refresh()
        except (FileNotFoundError, OSError, ValueError, RuntimeError):
            log.warning("Could not mark failed agent archive reservation %s", request_id)
        # Publish terminal job state only after the retained record is safely marked.
        # Pollers observing `failed` can therefore rely on auto-resume remaining disabled.
        with self._lock():
            latest = self._read_job(request_id)
            if latest is None or latest.get("status") == "completed":
                return
            latest.update(status="failed", completed_at=now().isoformat(), error=error)
            self._save_job(request_id, latest)

    def _requeue_closing_job(self, request_id: str) -> None:
        with self._lock():
            job = self._read_job(request_id)
            if job is None or job.get("status") == "completed":
                return
        with self.controller.lock:
            agent = self.store.get(job["agent_id"])
            lease = getattr(agent, "agent_archive", None)
            if isinstance(lease, dict) and lease.get("request_id") == request_id:
                self.store.update(agent.id, {"agent_archive": dict(lease) | {"status": "queued"}})
        with self._lock():
            latest = self._read_job(request_id)
            if latest is not None and latest.get("status") != "completed":
                latest["status"] = "queued"
                latest.pop("error", None)
                self._save_job(request_id, latest)

    def _run(self, request_id: str, raise_errors: bool = False) -> dict[str, Any] | None:
        job: dict[str, Any] | None = None
        try:
            job = self._set_running(request_id)
            if job.get("status") == "completed":
                return dict(job.get("archive", {}))
            self._check_open()
            exact_record = _regular_bytes(
                self._snapshot_path(request_id, "agent.md"),
                MAX_RECORD_BYTES,
                "Captured agent metadata",
            )
            source_data, source_identity = _source_snapshot(Path(job["source"]))
            if source_identity != job.get("source_stat"):
                raise RuntimeError("Session changed since archive reservation")
            header, _, _, _ = session_context(source_data)
            if not job.get("session_id") or str(header.get("id")) != job["session_id"]:
                raise ValueError("Saved session ID does not exactly match the agent record")
            with self.store.lock(job["agent_id"]):
                current = self.store.get(job["agent_id"])
                lease = getattr(current, "agent_archive", None)
                if (
                    not isinstance(lease, dict)
                    or lease.get("request_id") != request_id
                    or lease.get("archive_id") != job.get("archive_id")
                    or lease.get("status") != "running"
                ):
                    raise RuntimeError("Agent archive reservation ownership changed")
                file_index, file_index_sha = self._file_index_snapshot(job["agent_id"])
            job.update(
                source_sha256=digest(source_data),
                source_size=len(source_data),
                file_index_sha256=file_index_sha,
            )
            with self._lock():
                self._save_job(request_id, job)
            agent = decode(exact_record.decode("utf-8"))
            self._path(job["archive_id"]).unlink(missing_ok=True)
            summary, captured = self._build(
                agent,
                exact_record,
                Path(job["source"]),
                source_data,
                bool(job["include_files"]),
                job["archive_id"],
                file_index,
            )
            manifest, payloads = self._verify(job["archive_id"])
            if (
                manifest["agent_id"] != job["agent_id"]
                or payloads["agent.md"] != captured
                or payloads["session.jsonl"] != source_data
            ):
                raise ArchiveCorrupt("Verified archive does not match the captured source")
            job.update(summary=dict(summary), phase="archive_verified")
            with self._lock():
                self._save_job(request_id, job)
            self._check_open()
            return self._complete_job(job, summary, source_data, exact_record)
        except _ArchiveClosing:
            if job is not None:
                self._path(job["archive_id"]).unlink(missing_ok=True)
            self._requeue_closing_job(request_id)
            if raise_errors:
                raise
            return None
        except Exception as exc:
            self._fail_job(request_id, exc)
            if raise_errors:
                raise
            log.warning("Agent archive job %s failed: %s", request_id, _safe_error(exc))
            return None

    def archive(self, agent_id: str, request: ArchiveRequest) -> dict[str, Any]:
        """Synchronous worker entry retained for direct callers and focused tests."""
        job, created = self._reserve(agent_id, request, submit=False)
        if not created:
            if job.get("status") == "completed" and isinstance(job.get("archive"), dict):
                return dict(job["archive"])
            if job.get("status") == "failed":
                raise RuntimeError(str(job.get("error", "Archive job failed")))
        result = self._run(job["request_id"], True)
        assert result is not None
        return result

    def recover(self) -> None:
        """Resume durable queued/running jobs without doing archive I/O on startup."""
        if not self.jobs.exists() or self.jobs.is_symlink() or self._closing.is_set():
            return
        with self._lock():
            # Finished history is bounded by _save_job; every live transaction must be
            # considered even when there are more than the UI's 128 displayed records.
            paths = sorted(self.jobs.glob("*.json"))
            jobs = []
            for path in paths:
                try:
                    job = self._read_job(str(UUID(path.stem)))
                    if job is not None and job.get("status") in {"queued", "running", "restoring"}:
                        jobs.append(job)
                except (OSError, ValueError, ArchiveCorrupt):
                    log.warning("Could not recover agent archive job %s", path.stem)
        for job in jobs:
            try:
                if job.get("operation") == "resurrect":
                    # Resolve a commit receipt synchronously: a crash after the index commit
                    # must not resubmit restoration writes. Otherwise establish/confirm the
                    # durable reservation before a worker can create an agent.
                    if self._prepare_resurrection_run(job) is None:
                        self._executor.submit(self._run_resurrection, job)
                    continue
                with self.controller.lock:
                    try:
                        agent = self.store.get(job["agent_id"])
                    except FileNotFoundError:
                        agent = None
                    if agent is not None:
                        lease = getattr(agent, "agent_archive", None)
                        if (
                            not isinstance(lease, dict)
                            or lease.get("request_id") != job["request_id"]
                        ):
                            raise RuntimeError("Archive reservation is no longer owned by this job")
                if agent is None:
                    self._finalize_forgotten(job)
                else:
                    self._executor.submit(self._run, job["request_id"], False)
            except Exception as exc:
                self._fail_job(job["request_id"], exc)

    def list(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock():
            index = self._read_index()
            job_paths = (
                sorted(
                    self.jobs.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True
                )
                if self.jobs.exists() and not self.jobs.is_symlink()
                else []
            )
            jobs = []
            for path in job_paths[:128]:
                try:
                    job = self._read_job(str(UUID(path.stem)))
                    if job is not None:
                        jobs.append(self._public_job(job))
                except (OSError, ValueError, ArchiveCorrupt):
                    continue
            index_records = [dict(item) for item in index["archives"]]
        active = {str(agent.id) for agent in self.store.scan()}
        result = []
        for item in sorted(index_records, key=lambda value: value["archived_at"], reverse=True):
            missing = not self._path(item["id"]).is_file()
            item["missing_zip"] = missing
            item["restorable"] = (
                item.get("status") == "archived" and item["agent_id"] not in active and not missing
            )
            result.append(item)
        return {"archives": result, "jobs": jobs}

    def _restore_dependencies(
        self,
        archive_id: str,
        manifest: dict[str, Any],
        payloads: Mapping[str, bytes],
        session_path: Path,
    ) -> tuple[Path, dict[str, str], list[Path]]:
        base = self.store.sessions_dir / "restored" / archive_id
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            raise RuntimeError("Private restoration directory is invalid")
        paths = manifest.get("paths")
        source_value = manifest.get("source_session", {}).get("path")
        session_id = manifest.get("session_id")
        if not isinstance(paths, dict) or not isinstance(source_value, str):
            raise ArchiveCorrupt("Archive path map is invalid")
        if not isinstance(session_id, str) or not session_id:
            raise ArchiveCorrupt("Archive session identity is invalid")
        old_source = Path(source_value).expanduser().absolute()
        old_parent = old_source.parent
        context_key = _session_hash(session_id)
        spill_key = digest(session_id.encode("utf-8"))[:16]

        planned_paths: list[tuple[str, Path]] = []
        replacements: dict[str, str] = {}
        for name in payloads:
            if not name.startswith("dependencies/"):
                continue
            parts = PurePosixPath(name).parts
            original_value = paths.get(name)
            if not isinstance(original_value, str):
                raise ArchiveCorrupt("Dependency has no bound original path")
            original = Path(original_value).expanduser().absolute()
            if len(parts) >= 5 and parts[1:3] == ("global", "tool-input"):
                job_id, tail = parts[3], parts[4:]
                if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not tail:
                    raise ArchiveCorrupt("Tool-input dependency path is invalid")
                destination = self.store.home / "tool-input-compactions" / job_id / Path(*tail)
            elif len(parts) >= 5 and parts[1:3] == ("global", "tool-output"):
                job_id, tail = parts[3], parts[4:]
                if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not tail:
                    raise ArchiveCorrupt("Tool-output dependency path is invalid")
                destination = self.store.home / "tool-output-repairs" / job_id / Path(*tail)
            elif len(parts) >= 4 and parts[1:3] == ("session", "context-archives"):
                tail = parts[3:]
                expected = old_parent / "context-archives" / context_key / Path(*tail)
                if original != expected:
                    raise ArchiveCorrupt("History dependency path is not owned by the session")
                destination = base / "context-archives" / context_key / Path(*tail)
            elif len(parts) >= 4 and parts[1:3] == ("session", "output-spills"):
                tail = parts[3:]
                expected = old_parent / "output-spills" / f"session-{spill_key}" / Path(*tail)
                if original != expected:
                    raise ArchiveCorrupt("Output dependency path is not owned by the session")
                destination = base / "output-spills" / f"session-{spill_key}" / Path(*tail)
            else:
                raise ArchiveCorrupt("Dependency restoration prefix is invalid")
            if not destination.resolve().is_relative_to(self.store.home.resolve()):
                raise ArchiveCorrupt("Dependency restoration path escapes Swarm home")
            # An exact original session keeps its exact owned dependencies and paths. Only
            # private relocated sessions receive newly written session-scoped artifacts.
            if parts[1] == "session" and session_path == old_source:
                continue
            planned_paths.append((name, destination))
            replacements[original_value] = str(destination)

        old_context = old_parent / "context-archives" / context_key
        new_context = base / "context-archives" / context_key
        old_spills = old_parent / "output-spills" / f"session-{spill_key}"
        new_spills = base / "output-spills" / f"session-{spill_key}"
        replacements.update(
            {
                str(old_context): str(new_context),
                agent_path(old_context): agent_path(new_context),
                str(old_spills): str(new_spills),
                agent_path(old_spills): agent_path(new_spills),
            }
        )
        if session_path != old_source:
            replacements[str(old_source)] = str(session_path)
            replacements[agent_path(old_source)] = agent_path(session_path)

        def rebound(name: str, destination: Path) -> bytes:
            data = payloads[name]
            try:
                if name.startswith("dependencies/session/context-archives/") and name.endswith(
                    "/manifest.json"
                ):
                    value = json.loads(data)
                    if (
                        not isinstance(value, dict)
                        or value.get("session_id") != session_id
                        or value.get("archive_id") != destination.parent.name
                    ):
                        raise ArchiveCorrupt("History dependency manifest is invalid")
                    value["source"] = str(session_path)
                    value["agent_source"] = agent_path(session_path)
                    return _json(value, pretty=True)
                if name.startswith("dependencies/session/output-spills/") and name.endswith(
                    ".meta.json"
                ):
                    value = json.loads(data)
                    spill_id = destination.name.removesuffix(".meta.json")
                    if (
                        not isinstance(value, dict)
                        or value.get("id") != spill_id
                        or value.get("session_id") != session_id
                    ):
                        raise ArchiveCorrupt("Output dependency metadata is invalid")
                    expected = {
                        "file": destination.with_name(f"{spill_id}.txt"),
                        "json_file": destination.with_name(f"{spill_id}.json"),
                        "metadata_file": destination,
                    }
                    for field, target in expected.items():
                        old = value.get(field)
                        if not isinstance(old, str) or replacements.get(old) != str(target):
                            raise ArchiveCorrupt("Output dependency metadata path is invalid")
                        value[field] = str(target)
                    return _json(value, pretty=True)
                if name.endswith("/index.json") and name.startswith("dependencies/global/"):
                    value = json.loads(data)
                    if not isinstance(value, dict) or value.get("source") != str(old_source):
                        raise ArchiveCorrupt("Tool dependency index source is invalid")
                    value["source"] = str(session_path)
                    return _json(value, pretty=True)
            except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
                raise ArchiveCorrupt("Dependency metadata is invalid") from exc
            return data

        planned = [(name, destination, rebound(name, destination)) for name, destination in planned_paths]

        # Refuse all conflicts before creating anything. Rebound metadata is deterministic,
        # so an interrupted resurrection remains idempotent without accepting stale paths.
        for _name, destination, data in planned:
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    raise RuntimeError("Dependency restoration target is not a regular file")
                if _regular_digest(destination, MAX_MEMBER_BYTES, "Dependency target") != digest(
                    data
                ):
                    raise RuntimeError("Dependency restoration would overwrite different bytes")

        created: list[Path] = []
        try:
            _mkdir_private(base, self.store.home)
            for _name, destination, data in planned:
                if destination.exists():
                    continue
                _mkdir_private(destination.parent, self.store.home)
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                created.append(destination)
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            shutil.rmtree(base, ignore_errors=True)
            raise
        return base, replacements, created

    @staticmethod
    def _relocate_session(data: bytes, replacements: dict[str, str]) -> bytes:
        if not replacements:
            return data
        changed = False
        lines: list[bytes] = []

        def replace(value: Any) -> Any:
            nonlocal changed
            if isinstance(value, str):
                for old, new in replacements.items():
                    if old in value:
                        value = value.replace(old, new)
                        changed = True
                return value
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        try:
            for raw in data.splitlines():
                value = json.loads(raw)
                # Native projections include message and compaction text verbatim. Paths in
                # those records are historical/model-visible data, while extension-owned
                # custom records are the live lookup indexes that must follow restored files.
                if isinstance(value, dict) and value.get("type") == "custom":
                    value = replace(value)
                lines.append(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ArchiveCorrupt("Session cannot be safely relocated") from exc
        result = b"\n".join(lines) + (b"\n" if data.endswith(b"\n") else b"")
        if changed and len(result) > MAX_SOURCE_BYTES:
            raise ValueError("Relocated session exceeds 128 MiB")
        session_context(result)
        return result

    def _restore_files(
        self, archive_id: str, payloads: Mapping[str, bytes], agent: Agent
    ) -> tuple[FileIndex, list[Path], list[dict[str, str]]]:
        created: list[Path] = []
        try:
            value = json.loads(payloads["file_reference.json"])
            raw_files = value.get("files", [])
            if not isinstance(raw_files, list) or len(raw_files) > MAX_AGENT_FILES:
                raise ArchiveCorrupt("Archived file references are invalid")
            refs: list[FileReference] = []
            missing = list(value.get("missing_files", []))
            if not all(
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("filename"), str)
                for item in missing
            ):
                raise ArchiveCorrupt("Archived missing-file report is invalid")
            for raw_value in raw_files:
                if not isinstance(raw_value, dict):
                    raise ArchiveCorrupt("Archived file reference is invalid")
                raw = dict(raw_value)
                member = raw.pop("archive_member", None)
                checksum = raw.pop("sha256", None)
                old_path = raw.get("path")
                if member is not None:
                    expected_member = f"files/{raw.get('id')}/payload"
                    if member != expected_member or member not in payloads:
                        raise ArchiveCorrupt("Archived referenced-file bytes are invalid")
                    filename = str(raw.get("filename", ""))
                    if filename != Path(filename).name or (
                        _secret_name(filename) and not bool(raw.get("uploaded"))
                    ):
                        raise ArchiveCorrupt("Archived referenced-file name is unsafe")
                    directory = (
                        self.store.home / "files" / "resurrected" / archive_id / str(raw["id"])
                    )
                    destination = directory / filename
                    if not destination.resolve().is_relative_to(self.store.home.resolve()):
                        raise ArchiveCorrupt("Referenced-file restoration escapes Swarm home")
                    _mkdir_private(directory, self.store.home)
                    if destination.exists() or destination.is_symlink():
                        if (
                            destination.is_symlink()
                            or not destination.is_file()
                            or _regular_digest(destination, MAX_PRODUCT_BYTES, "Restored file")
                            != checksum
                        ):
                            raise RuntimeError("Referenced-file restoration target conflicts")
                    else:
                        data = payloads[member]
                        if digest(data) != checksum:
                            raise ArchiveCorrupt("Archived referenced-file bytes are invalid")
                        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        created.append(directory)
                    raw["path"] = str(destination)
                    raw["uploaded"] = False
                elif not isinstance(old_path, str):
                    raise ArchiveCorrupt("Archived reference path is invalid")
                else:
                    path = Path(old_path)
                    try:
                        info = path.stat(follow_symlinks=False)
                        available = (
                            not path.is_symlink()
                            and stat.S_ISREG(info.st_mode)
                            and info.st_size == raw.get("size")
                            and path.resolve(strict=True) == path
                        )
                    except (OSError, RuntimeError):
                        available = False
                    if not available:
                        missing.append(
                            {
                                "id": str(raw.get("id", "")),
                                "filename": str(raw.get("filename", "")),
                                "reason": "unavailable_at_restore",
                            }
                        )
                        continue
                refs.append(FileReference.model_validate(raw))
            return (
                FileIndex(revision=int(value.get("revision", 0)) + 1, files=refs),
                created,
                missing,
            )
        except Exception:
            for path in reversed(created):
                shutil.rmtree(path, ignore_errors=True)
            raise

    @staticmethod
    def _resurrection_matches(job: dict[str, Any], archive_id: str, payload_hash: str) -> bool:
        return (
            job.get("operation") == "resurrect"
            and job.get("archive_id") == archive_id
            and job.get("payload_hash") == payload_hash
        )

    def _owned_resurrection(
        self,
        agent: Agent,
        job: dict[str, Any],
        manifest: dict[str, Any],
        *,
        verify_source: bool = True,
    ) -> bool:
        if (
            str(agent.id) != job.get("agent_id")
            or getattr(agent, "resurrection_request_id", None) != job.get("request_id")
            or getattr(agent, "restored_from_archive", None) != job.get("archive_id")
            or getattr(agent, "resurrection_session_sha256", None)
            != job.get("restored_session_sha256")
            or agent.auto_resume
            or agent.pid is not None
            or agent.session_id != manifest.get("session_id")
            or not agent.session_file
        ):
            return False
        if not verify_source:
            return True
        try:
            return _regular_digest(
                Path(agent.session_file).expanduser().absolute(),
                MAX_SOURCE_BYTES,
                "Restored session",
            ) == job.get("restored_session_sha256")
        except (OSError, ValueError):
            return False

    def _resurrection_receipt_result(
        self,
        receipt: dict[str, Any],
        record: dict[str, Any],
        job: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return an exact completed restore only when its inert audit record still owns it."""
        request_id = str(receipt.get("request_id", ""))
        archive_id = str(receipt.get("archive_id", ""))
        if (
            receipt.get("operation") != "resurrect"
            or record.get("id") != archive_id
            or record.get("status") != "resurrected"
        ):
            raise ArchiveCorrupt("Resurrection receipt does not match archive state")
        agent = self.store.get(record["agent_id"])
        session_sha = getattr(agent, "resurrection_session_sha256", None)
        if (
            str(agent.id) != record.get("agent_id")
            or getattr(agent, "resurrection_request_id", None) != request_id
            or getattr(agent, "restored_from_archive", None) != archive_id
            or agent.session_id != record.get("session_id")
            or not isinstance(session_sha, str)
            or not HEX64.fullmatch(session_sha)
            or not agent.session_file
            or agent.auto_resume
            or agent.pid is not None
            or self.controller.processes.alive(agent)
        ):
            raise RuntimeError("Completed resurrection audit ownership changed")
        if job is not None and job.get("restored_session_sha256") not in {None, session_sha}:
            raise RuntimeError("Completed resurrection session ownership changed")
        try:
            if (
                _regular_digest(
                    Path(agent.session_file).expanduser().absolute(),
                    MAX_SOURCE_BYTES,
                    "Restored session",
                )
                != session_sha
            ):
                raise RuntimeError("Completed resurrection session ownership changed")
        except (OSError, ValueError) as exc:
            raise RuntimeError("Completed resurrection session ownership changed") from exc
        result = {
            "archive": dict(record),
            "agent": agent.model_dump(mode="json") | {"alive": False},
        }
        if job is not None and isinstance(job.get("missing_files"), list):
            result["missing_files"] = list(job["missing_files"])
        return result

    def _ensure_resurrection_reservation(
        self, index: dict[str, Any], record: dict[str, Any], job: dict[str, Any]
    ) -> None:
        """Commit/confirm the index half of a prepared resurrection transaction."""
        request_id = job["request_id"]
        payload_hash = job["payload_hash"]
        try:
            candidate = self.store.get(record["agent_id"])
        except FileNotFoundError:
            candidate = None
        if candidate is not None:
            if not job.get("restored_session_sha256") or not self._owned_resurrection(
                candidate, job, {"session_id": record.get("session_id")}, verify_source=False
            ):
                raise RuntimeError(
                    "An agent with the archived ID already exists; restore never overwrites"
                )
        status = record.get("status")
        owner = record.get("restoration_request_id")
        owner_hash = record.get("restoration_payload_hash")
        if status == "restoring" and owner not in {None, request_id}:
            raise RuntimeError("This archive is already being restored")
        if status == "restoring" and owner_hash not in {None, payload_hash}:
            raise RuntimeError("This archive is already being restored")
        if status not in {"archived", "restoring"}:
            raise RuntimeError("Archive is not available for restoration")
        if (
            status != "restoring"
            or owner != request_id
            or owner_hash != payload_hash
            or record.get("restorable") is not False
        ):
            record.update(
                status="restoring",
                restorable=False,
                restoration_request_id=request_id,
                restoration_payload_hash=payload_hash,
            )
            self._write_index(index)

    def _reserve_resurrection(
        self, archive_id: str, request_id: str, payload_hash: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        with self.controller.lock, self._lock():
            index = self._read_index()
            record = self._find(index, archive_id)
            old = self._receipt(index, request_id)
            if old is not None:
                if (
                    old.get("operation") != "resurrect"
                    or old.get("archive_id") != archive_id
                    or old.get("payload_hash") != payload_hash
                ):
                    raise RuntimeError("request_id was already used for a different operation")
                return {}, self._resurrection_receipt_result(old, record)
            previous = self._read_job(request_id)
            if previous is not None:
                if not self._resurrection_matches(previous, archive_id, payload_hash):
                    raise RuntimeError("request_id was already used for a different operation")
                self._ensure_resurrection_reservation(index, record, previous)
                return previous, None
            if record.get("status") == "restoring":
                raise RuntimeError("This archive is already being restored")
            try:
                self.store.get(record["agent_id"])
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(
                    "An agent with the archived ID already exists; restore never overwrites"
                )
            job = {
                "version": VERSION,
                "id": request_id,
                "request_id": request_id,
                "payload_hash": payload_hash,
                "operation": "resurrect",
                "archive_id": archive_id,
                "agent_id": record["agent_id"],
                "name": record.get("name", ""),
                "status": "restoring",
                # Durable before the index reservation: recovery may safely finish the
                # second half, but no worker may create an agent until it is committed.
                "phase": "prepared",
                "created_at": now().isoformat(),
            }
            self._save_job(request_id, job)
            self._ensure_resurrection_reservation(index, record, job)
            return job, None

    def _prepare_resurrection_run(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a durable receipt first, otherwise commit the index reservation."""
        with self.controller.lock, self._lock():
            index = self._read_index()
            record = self._find(index, job["archive_id"])
            receipt = self._receipt(index, job["request_id"])
            if receipt is not None:
                if (
                    receipt.get("operation") != "resurrect"
                    or receipt.get("archive_id") != job["archive_id"]
                    or receipt.get("payload_hash") != job["payload_hash"]
                ):
                    raise RuntimeError("request_id was already used for a different operation")
                result = self._resurrection_receipt_result(receipt, record, job)
                job.update(
                    status="completed",
                    phase="committed",
                    completed_at=receipt.get("completed_at") or now().isoformat(),
                    archive=dict(record),
                )
                job.pop("error", None)
                self._save_job(job["request_id"], job)
                return result
            self._ensure_resurrection_reservation(index, record, job)
            return None

    def _run_resurrection(self, job: dict[str, Any]) -> dict[str, Any]:
        selected = job["archive_id"]
        request_id = job["request_id"]
        completed = self._prepare_resurrection_run(job)
        if completed is not None:
            return completed
        manifest, payloads = self._verify(selected)
        if manifest["agent_id"] != job["agent_id"] or manifest["archive_id"] != selected:
            raise ArchiveCorrupt("Archive ZIP and index identities do not match")
        archived = decode(payloads["agent.md"].decode("utf-8"))
        if archived.backend != "pi" or not archived.managed:
            raise ArchiveCorrupt("Only an archived managed Pi agent can be resurrected")
        if not Path(archived.folder).expanduser().is_dir():
            raise RuntimeError(
                "Archived working folder is missing; use Edit restore when that feature is available. "
                "Swarm will not recreate a Git worktree automatically."
            )

        source_data = payloads["session.jsonl"]
        original = Path(str(manifest["source_session"]["path"])).expanduser().absolute()
        original_is_current = (
            original.is_file()
            and not original.is_symlink()
            and _regular_digest(original, MAX_SOURCE_BYTES, "Original session")
            == manifest["source_session"]["sha256"]
        )
        if original_is_current:
            paths = manifest.get("paths", {})
            members = manifest.get("members", {})
            for name in payloads:
                if not name.startswith("dependencies/session/"):
                    continue
                original_dependency = paths.get(name) if isinstance(paths, dict) else None
                descriptor = members.get(name) if isinstance(members, dict) else None
                try:
                    dependency_is_current = (
                        isinstance(original_dependency, str)
                        and isinstance(descriptor, dict)
                        and _regular_digest(
                            Path(original_dependency).expanduser().absolute(),
                            MAX_MEMBER_BYTES,
                            "Original dependency",
                        )
                        == descriptor.get("sha256")
                    )
                except (OSError, ValueError):
                    dependency_is_current = False
                if not dependency_is_current:
                    original_is_current = False
                    break
        base = self.store.sessions_dir / "restored" / selected
        session_path = original if original_is_current else base / "session.jsonl"
        base, replacements, dependency_files = self._restore_dependencies(
            selected, manifest, payloads, session_path
        )
        if not original_is_current:
            source_data = self._relocate_session(source_data, replacements)
        restored_sha = digest(source_data)
        job.update(
            phase="prepared",
            session_file=str(session_path),
            restored_session_sha256=restored_sha,
            restoration_base=str(base),
        )
        with self._lock():
            self._save_job(request_id, job)

        created_session = False
        created_files: list[Path] = []
        product_directory: Path | None = None
        product_created = False
        agent_created = False
        try:
            if session_path != original:
                if session_path.exists() or session_path.is_symlink():
                    if (
                        session_path.is_symlink()
                        or not session_path.is_file()
                        or digest(read_source(session_path)) != restored_sha
                    ):
                        raise RuntimeError("Restored session target conflicts")
                else:
                    fd = os.open(session_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(source_data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    created_session = True
            source_data = b""

            file_index, created_files, missing_files = self._restore_files(
                selected, payloads, archived
            )
            fields = archived.model_dump(mode="json")
            old_parent = fields.get("parent_id")
            if old_parent:
                try:
                    self.store.get(old_parent)
                except FileNotFoundError:
                    fields["parent_id"] = None
                    fields["archive_origin_parent_id"] = old_parent
            fields.update(
                {
                    "pid": None,
                    "status": Status.idle,
                    "auto_resume": False,
                    "wake_at": None,
                    "wake_delivery": None,
                    "child_wait": None,
                    "orchestration_stop": None,
                    "stop_pending": False,
                    "restart_job_id": None,
                    "restart_identity": None,
                    "agent_archive": None,
                    "meta_compaction": None,
                    "tool_input_compaction": None,
                    "tool_output_repair": None,
                    "context_archive": None,
                    "session_file": str(session_path),
                    "last_error": None,
                    "restored_from_archive": selected,
                    "resurrection_request_id": request_id,
                    "resurrection_session_sha256": restored_sha,
                }
            )
            if not fields.get("parent_id"):
                fields["spawn_state"] = None
            product = archived.product
            if product and product.kind == "file":
                if "product/payload" not in payloads:
                    fields["product"] = None
                else:
                    descriptor = manifest["members"].get("product/payload")
                    if descriptor != {"size": product.size, "sha256": product.sha256}:
                        raise ArchiveCorrupt("Archived product snapshot is invalid")
                    product_directory = (
                        self.store.home / "products" / str(archived.id) / str(product.id)
                    )
                    _mkdir_private(product_directory, self.store.home)
                    target = product_directory / "payload"
                    if target.exists() or target.is_symlink():
                        if (
                            target.is_symlink()
                            or not target.is_file()
                            or _regular_digest(target, MAX_PRODUCT_BYTES, "Product target")
                            != product.sha256
                        ):
                            raise RuntimeError("Product restoration target conflicts")
                    else:
                        data = payloads["product/payload"]
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        product_created = True

            restored = Agent.model_validate(fields)
            if restored.parent_id and restored.spawn_state == "ready":
                from .orchestration import verify_workspace

                verify_workspace(restored)
            self.guard.check_external(restored)
            try:
                current = self.store.get(restored.id)
            except FileNotFoundError:
                try:
                    self.store.create(restored)
                except Exception:
                    try:
                        current = self.store.get(restored.id)
                    except FileNotFoundError:
                        raise
                    if not self._owned_resurrection(current, job, manifest):
                        raise RuntimeError("Resurrection create left an unowned agent record")
                    restored = current
                agent_created = True
            else:
                if not self._owned_resurrection(current, job, manifest):
                    raise RuntimeError(
                        "An agent with the archived ID already exists; restore never overwrites"
                    )
                restored = current
                agent_created = True
            if file_index.files:
                self.controller.files.write(restored.id, file_index)

            job.update(
                phase="agent_created",
                restored_files=[ref.model_dump(mode="json") for ref in file_index.files],
                missing_files=missing_files,
            )
            with self._lock():
                self._save_job(request_id, job)

            current = self.store.get(restored.id)
            if not self._owned_resurrection(current, job, manifest):
                raise RuntimeError("Resurrected agent ownership changed before commit")
            with self.controller.lock:
                current = self.store.get(restored.id)
                if not self._owned_resurrection(current, job, manifest, verify_source=False):
                    raise RuntimeError("Resurrected agent ownership changed before commit")
                with self._lock():
                    index = self._read_index()
                    record = self._find(index, selected)
                    receipt = self._receipt(index, request_id)
                    if receipt is not None:
                        if (
                            receipt.get("operation") != "resurrect"
                            or receipt.get("archive_id") != selected
                            or receipt.get("payload_hash") != job["payload_hash"]
                        ):
                            raise RuntimeError(
                                "request_id was already used for a different operation"
                            )
                        result = self._resurrection_receipt_result(receipt, record, job)
                        timestamp = receipt.get("completed_at") or now().isoformat()
                    else:
                        if (
                            record.get("status") != "restoring"
                            or record.get("restoration_request_id") != request_id
                            or record.get("restoration_payload_hash") != job["payload_hash"]
                        ):
                            raise RuntimeError("Archive restoration reservation changed")
                        timestamp = now().isoformat()
                        record.update(
                            status="resurrected",
                            last_accessed_at=timestamp,
                            last_resurrected_at=timestamp,
                            restorable=False,
                        )
                        record.pop("error", None)
                        record.pop("restoration_request_id", None)
                        record.pop("restoration_payload_hash", None)
                        self._save_receipt(
                            index, request_id, "resurrect", selected, job["payload_hash"]
                        )
                        self._write_index(index)
                        result = {
                            "archive": dict(record),
                            "agent": restored.model_dump(mode="json") | {"alive": False},
                            "missing_files": missing_files,
                        }
                    job.update(
                        status="completed",
                        phase="committed",
                        completed_at=timestamp,
                        archive=dict(record),
                    )
                    job.pop("error", None)
                    self._save_job(request_id, job)
                self.controller.refresh()
            return result
        except Exception as exc:
            if agent_created:
                # A fault after the index/receipt commit is an already-completed
                # transaction. Do not overwrite the older durable job phase with an
                # in-memory `completed` mutation plus an error; recovery resolves the
                # exact receipt before resubmitting any work.
                with self._lock():
                    index = self._read_index()
                    receipt = self._receipt(index, request_id)
                    committed = bool(
                        receipt is not None
                        and receipt.get("operation") == "resurrect"
                        and receipt.get("archive_id") == selected
                        and receipt.get("payload_hash") == job.get("payload_hash")
                    )
                    if not committed:
                        job["error"] = _safe_error(exc)
                        self._save_job(request_id, job)
            else:
                if created_session:
                    session_path.unlink(missing_ok=True)
                for path in reversed(created_files):
                    shutil.rmtree(path, ignore_errors=True)
                if product_created and product_directory is not None:
                    shutil.rmtree(product_directory, ignore_errors=True)
                for path in reversed(dependency_files):
                    path.unlink(missing_ok=True)
                shutil.rmtree(base, ignore_errors=True)
            raise

    def resurrect(self, archive_id: str, request: ResurrectRequest) -> dict[str, Any]:
        selected = str(UUID(str(archive_id)))
        request_id = str(request.request_id)
        payload_hash = _payload_hash({"operation": "resurrect", "archive_id": selected})
        job, completed = self._reserve_resurrection(selected, request_id, payload_hash)
        if completed is not None:
            return completed
        return self._run_resurrection(job)

    def delete(self, archive_id: str) -> dict[str, Any]:
        selected = str(UUID(str(archive_id)))
        with self._lock():
            index = self._read_index()
            record = self._find(index, selected)
            if record.get("status") == "restoring":
                raise RuntimeError("Archive cannot be deleted while restoration is active")
            record["status"] = "deleting"
            record["restorable"] = False
            self._write_index(index)
            path = self._path(selected)
            path.unlink(missing_ok=True)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            index["archives"] = [item for item in index["archives"] if item["id"] != selected]
            index["receipts"] = [
                item for item in index["receipts"] if item.get("archive_id") != selected
            ]
            self._write_index(index)
            return {"id": selected, "deleted": True}

    def download(self, archive_id: str) -> tuple[dict[str, Any], BinaryIO]:
        selected = str(UUID(str(archive_id)))
        with self._lock():
            index = self._read_index()
            record = dict(self._find(index, selected))
            stream = open_regular(self._path(selected))
        try:
            # Verification can take seconds for a 512 MiB archive. The private open
            # descriptor remains readable if deletion races, without blocking list/delete.
            self._verify(selected, stream)
            stream.seek(0)
            with self._lock():
                index = self._read_index()
                current = next(
                    (item for item in index["archives"] if item.get("id") == selected), None
                )
                if current is not None:
                    timestamp = now().isoformat()
                    current["last_accessed_at"] = timestamp
                    record = dict(current)
                    self._write_index(index)
            return record, stream
        except Exception:
            stream.close()
            raise
