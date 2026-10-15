"""Latest-only advertised products; local files are snapshotted, never served by raw path."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Product
from .paths import local_path

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger(__name__)
MAX_PRODUCT_BYTES = 128 * 1024 * 1024


class ProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=120)
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_source(self) -> ProductRequest:
        if (self.url is None) == (self.path is None):
            raise ValueError("specify exactly one product URL or local file path")
        return self


def open_regular(path: Path) -> BinaryIO:
    if path.is_symlink():
        raise ValueError("product storage must not be a symbolic link")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("product must be a regular file, not a directory or device")
        return os.fdopen(fd, "rb")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


class Products:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.store = controller.store
        self.root = self.store.home / "products"

    def directory(self, agent_id: UUID, product_id: UUID | None = None) -> Path:
        # Only validated UUID path components; neither request nor record supplies a storage path.
        agent_dir = self.root / str(UUID(str(agent_id)))
        target = agent_dir / str(UUID(str(product_id))) if product_id else agent_dir
        for part in (self.root, agent_dir, target):
            if part.is_symlink():
                raise ValueError("product storage directories must not be symbolic links")
        if not target.resolve().is_relative_to(self.store.home.resolve()):
            raise ValueError("product storage escapes Swarm home")
        return target

    def collect(self) -> None:
        """Delete superseded/orphaned snapshots, never originals; also used on restart."""
        with self.controller.lock:
            agents = self.store.scan()
            if self.store.errors or not self.root.exists():
                return
            if self.root.is_symlink():
                log.warning("Refusing to clean symlinked product storage")
                return
            current = {
                str(a.id): str(a.product.id)
                for a in agents
                if a.product and a.product.kind == "file"
            }
            try:
                directories = list(self.root.iterdir())
            except OSError:
                log.warning("Could not inspect product storage for cleanup")
                return
            for agent_dir in directories:
                try:
                    agent_id = UUID(agent_dir.name)
                    if agent_dir != self.directory(agent_id) or agent_dir.is_symlink():
                        continue
                    if not agent_dir.is_dir():
                        continue
                    for product_dir in agent_dir.iterdir():
                        try:
                            product_id = UUID(product_dir.name)
                        except ValueError:
                            continue  # Ignore unrelated files, never arbitrary paths.
                        if current.get(str(agent_id)) == product_dir.name:
                            continue
                        expected = self.directory(agent_id, product_id)
                        if product_dir != expected or product_dir.is_symlink():
                            continue
                        if product_dir.is_dir():
                            shutil.rmtree(product_dir)
                except (OSError, ValueError):
                    log.warning("Could not clean old product snapshots under %s", agent_dir)

    def publish(self, agent_id: str, request: ProductRequest) -> dict:
        with self.controller.lock:
            agent = self.store.get(agent_id)
            new_directory = None
            try:
                if request.url is not None:
                    # Validate without contacting the URL: links never cause server-side fetches.
                    product = Product(
                        kind="link",
                        label=request.label.strip()
                        or urlsplit(request.url).hostname
                        or "Open product",
                        url=request.url,
                    )
                else:
                    raw = request.path
                    assert raw is not None
                    # Relative server paths are relative to this agent's workspace, not the daemon.
                    source = (
                        local_path(raw)
                        if (
                            Path(raw).expanduser().is_absolute()
                            or PureWindowsPath(raw).is_absolute()
                        )
                        else local_path(agent.folder) / raw
                    )
                    source = source.resolve(strict=True)
                    with open_regular(source) as stream:
                        before = os.fstat(stream.fileno())
                        if before.st_size > MAX_PRODUCT_BYTES:
                            raise ValueError("product file exceeds the 128 MiB limit")
                        product = Product(
                            kind="file",
                            label=request.label.strip() or source.name[:120],
                            filename=source.name,
                            size=0,
                            sha256="0" * 64,
                        )
                        candidate = self.directory(agent.id, product.id)
                        candidate.mkdir(parents=True, mode=0o700)
                        new_directory = candidate
                        checksum, size = hashlib.sha256(), 0
                        payload = new_directory / "payload"
                        fd = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as output:
                            while block := stream.read(1024 * 1024):
                                size += len(block)
                                if size > MAX_PRODUCT_BYTES:
                                    raise ValueError("product file exceeds the 128 MiB limit")
                                checksum.update(block)
                                output.write(block)
                            output.flush()
                            os.fsync(output.fileno())
                        after = os.fstat(stream.fileno())
                        if (
                            size != before.st_size
                            or before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns
                        ):
                            raise RuntimeError(
                                "Source changed while publishing; previous product retained"
                            )
                        # Flush the directory entries before publishing the record pointer.
                        for directory in (new_directory, new_directory.parent, self.root):
                            try:
                                fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                            except OSError:
                                continue
                            try:
                                os.fsync(fd)
                            except OSError:
                                pass  # Directory fsync is unavailable on some platforms.
                            finally:
                                os.close(fd)
                        product = product.model_copy(
                            update={"size": size, "sha256": checksum.hexdigest()}
                        )
                self.store.update(agent.id, {"product": product.model_dump(mode="json")})
            except Exception:
                if new_directory is not None:
                    # It is newly allocated and was not committed to a successful record update.
                    # If record replacement succeeded before a later I/O error, preserve its bytes.
                    current = self.store.get(agent.id).product
                    if not current or current.id != product.id:
                        shutil.rmtree(new_directory, ignore_errors=True)
                raise
            self.collect()
            self.controller.refresh()
            return {"agent_id": str(agent.id), "product": product.model_dump(mode="json")}

    def clear(self, agent_id: str) -> dict:
        with self.controller.lock:
            agent = self.store.get(agent_id)
            self.store.update(agent.id, {"product": None})
            self.collect()
            self.controller.refresh()
            return {"agent_id": str(agent.id), "product": None}

    def download(self, agent_id: str, product_id: str) -> tuple[Product, BinaryIO]:
        with self.controller.lock:
            agent = self.store.get(agent_id)
            product = agent.product
            if not product or product.kind != "file" or str(product.id) != product_id:
                raise FileNotFoundError(
                    "This product is no longer the agent's current downloadable product"
                )
            payload = self.directory(agent.id, product.id) / "payload"
            stream = open_regular(payload)
            try:
                info = os.fstat(stream.fileno())
                if info.st_size != product.size or info.st_size > MAX_PRODUCT_BYTES:
                    raise ValueError("product snapshot size mismatch")
                checksum, size = hashlib.sha256(), 0
                while block := stream.read(min(1024 * 1024, product.size - size + 1)):
                    size += len(block)
                    if size > product.size:
                        raise ValueError("product snapshot changed while reading")
                    checksum.update(block)
                if size != product.size or checksum.hexdigest() != product.sha256:
                    raise ValueError("product snapshot checksum mismatch")
                stream.seek(0)
            except Exception:
                stream.close()
                raise
            return product, stream
