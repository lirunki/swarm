#!/usr/bin/env python3
"""Install an official Node 24 binary distribution into a local directory."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

DIST_URL = "https://nodejs.org/dist/latest-v24.x/"
MANIFEST_URL = DIST_URL + "SHASUMS256.txt"
MAX_MANIFEST_BYTES = 1 << 20
MAX_ARCHIVE_BYTES = 128 << 20
MAX_EXTRACTED_BYTES = 512 << 20
MAX_MEMBERS = 50_000
_CHUNK_SIZE = 64 << 10

OpenURL = Callable[[str], object]


class BootstrapError(RuntimeError):
    """A safe, user-facing bootstrap failure."""


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            raise urllib.error.URLError("refusing an insecure redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTPS_OPENER = urllib.request.build_opener(_HTTPSRedirectHandler())


def open_https(url: str):
    """Open an HTTPS URL using normal certificate validation and HTTPS-only redirects."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise BootstrapError("refusing a non-HTTPS or credential-bearing URL")
    response = _HTTPS_OPENER.open(
        urllib.request.Request(url, headers={"User-Agent": "swarm-node-bootstrap/1"}), timeout=30
    )
    final_url = urlsplit(response.geturl())
    if final_url.scheme != "https":
        response.close()
        raise BootstrapError("refusing an insecure redirect")
    return response


def _response_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BootstrapError("invalid Content-Length from download server") from exc


def _download(url: str, limit: int, open_url: OpenURL, output=None) -> bytes | None:
    try:
        with open_url(url) as response:  # type: ignore[attr-defined]
            length = _response_length(response)
            if length is not None and (length < 0 or length > limit):
                raise BootstrapError("download exceeds the allowed size")
            total = 0
            chunks = [] if output is None else None
            while True:
                chunk = response.read(min(_CHUNK_SIZE, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise BootstrapError("download exceeds the allowed size")
                if output is None:
                    chunks.append(chunk)
                else:
                    output.write(chunk)
            return b"".join(chunks) if chunks is not None else None
    except BootstrapError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise BootstrapError(f"download failed: {exc}") from exc


def platform_tag(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    """Return Node's OS and architecture tags for this host."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    os_tag = {"Linux": "linux", "Darwin": "darwin"}.get(system)
    arch_tag = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if os_tag is None or arch_tag is None:
        raise BootstrapError(f"unsupported platform: {system} {machine}")
    return os_tag, arch_tag


def select_archive(manifest: bytes, os_tag: str, arch_tag: str) -> tuple[str, str]:
    """Select exactly one Node 24 archive and checksum from a checksum manifest."""
    try:
        text = manifest.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BootstrapError("malformed Node checksum manifest") from exc
    pattern = re.compile(
        rf"^([0-9a-f]{{64}})  (node-v24\.\d+\.\d+-{re.escape(os_tag)}-"
        rf"{re.escape(arch_tag)}\.tar\.gz)$"
    )
    matches = [match.groups() for line in text.splitlines() if (match := pattern.fullmatch(line))]
    if len(matches) != 1:
        raise BootstrapError("Node checksum manifest has no unique matching archive")
    checksum, filename = matches[0]
    return filename, checksum


def _member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BootstrapError(f"unsafe archive path: {name!r}")
    return path


def _link_target(member_path: PurePosixPath, target: str, *, hardlink: bool) -> PurePosixPath:
    target_path = PurePosixPath(target)
    if not target or target_path.is_absolute():
        raise BootstrapError("unsafe archive link")
    combined = target_path if hardlink else member_path.parent / target_path
    parts: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise BootstrapError("archive link escapes extraction directory")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise BootstrapError("unsafe archive link")
    return PurePosixPath(*parts)


def _safe_mode(mode: int, *, directory: bool = False) -> int:
    mode &= 0o755
    return mode or (0o755 if directory else 0o644)


def extract_archive(archive: Path, output: Path, expected_root: str) -> Path:
    """Extract a validated tarball without trusting tar metadata or link traversal."""
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = []
            total_size = 0
            for member in bundle:
                if len(members) >= MAX_MEMBERS:
                    raise BootstrapError("archive contains too many entries")
                if member.isreg():
                    total_size += member.size
                    if total_size > MAX_EXTRACTED_BYTES:
                        raise BootstrapError("archive expands beyond the allowed size")
                members.append(member)

            indexed: dict[PurePosixPath, tarfile.TarInfo] = {}
            hardlinks: list[tuple[tarfile.TarInfo, PurePosixPath, PurePosixPath]] = []
            symlinks: list[tuple[tarfile.TarInfo, PurePosixPath, PurePosixPath]] = []
            for member in members:
                path = _member_path(member.name)
                if path.parts[0] != expected_root or path in indexed:
                    raise BootstrapError("archive has an unexpected or duplicate path")
                if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
                    raise BootstrapError("archive contains a special file")
                indexed[path] = member
                if member.issym() or member.islnk():
                    target = _link_target(path, member.linkname, hardlink=member.islnk())
                    if target.parts[0] != expected_root:
                        raise BootstrapError("archive link escapes the Node directory")
                    (hardlinks if member.islnk() else symlinks).append((member, path, target))

            root_path = PurePosixPath(expected_root)
            root_member = indexed.get(root_path)
            if root_member is None or not root_member.isdir():
                raise BootstrapError("archive is missing its top-level directory")

            for path, member in indexed.items():
                for parent in path.parents:
                    if parent == PurePosixPath("."):
                        break
                    parent_member = indexed.get(parent)
                    if parent_member is not None and not parent_member.isdir():
                        raise BootstrapError("archive path descends through a non-directory")
                destination = output.joinpath(*path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise BootstrapError("could not read an archive member")
                    with source, destination.open("xb") as target_file:
                        shutil.copyfileobj(source, target_file, _CHUNK_SIZE)
                    os.chmod(destination, _safe_mode(member.mode))

            for _member, path, target in hardlinks:
                target_member = indexed.get(target)
                if target_member is None or not target_member.isreg():
                    raise BootstrapError("archive hard link does not target a regular file")
                destination = output.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(output.joinpath(*target.parts), destination)

            for member, path, _target in symlinks:
                destination = output.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, destination)

            directories = ((path, member) for path, member in indexed.items() if member.isdir())
            for path, member in sorted(
                directories, key=lambda item: len(item[0].parts), reverse=True
            ):
                os.chmod(output.joinpath(*path.parts), _safe_mode(member.mode, directory=True))
    except BootstrapError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BootstrapError(f"could not extract Node archive: {exc}") from exc
    return output / expected_root


def install_node(
    destination: Path | str,
    *,
    system: str | None = None,
    machine: str | None = None,
    open_url: OpenURL = open_https,
) -> Path:
    """Download, verify, safely stage, and atomically install Node."""
    destination = Path(destination)
    if not destination.is_absolute():
        raise BootstrapError("destination must be an absolute path")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"destination already exists: {destination}")

    os_tag, arch_tag = platform_tag(system, machine)
    destination.parent.mkdir(parents=True, exist_ok=True)
    print("Fetching Node 24 checksums...", file=sys.stderr)
    manifest = _download(MANIFEST_URL, MAX_MANIFEST_BYTES, open_url)
    assert manifest is not None
    filename, expected_hash = select_archive(manifest, os_tag, arch_tag)

    work = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        archive = work / filename
        print(f"Downloading {filename}...", file=sys.stderr)
        with archive.open("xb") as output:
            _download(DIST_URL + filename, MAX_ARCHIVE_BYTES, open_url, output)
        with archive.open("rb") as source:
            actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
        if actual_hash != expected_hash:
            raise BootstrapError("Node archive checksum mismatch")

        print("Verifying and extracting Node...", file=sys.stderr)
        expected_root = filename.removesuffix(".tar.gz")
        staged = extract_archive(archive, work / "unpacked", expected_root)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"destination already exists: {destination}")
        staged.rename(destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"Node installed at {destination}", file=sys.stderr)
    return destination


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"Usage: {Path(sys.argv[0]).name} /absolute/path/to/node", file=sys.stderr)
        return 2
    try:
        install_node(args[0])
    except (BootstrapError, FileExistsError) as exc:
        print(f"Node bootstrap failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
