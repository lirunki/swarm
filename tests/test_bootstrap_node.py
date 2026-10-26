import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def bootstrap_node():
    source = Path(__file__).resolve().parents[1] / "scripts/bootstrap_node.py"
    spec = importlib.util.spec_from_file_location("bootstrap_node", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.headers = {"Content-Length": str(len(value))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def make_tar(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for kind, name, value in entries:
            member = tarfile.TarInfo(name)
            if kind == "dir":
                member.type = tarfile.DIRTYPE
                member.mode = 0o4755  # The installer must not propagate setuid.
                bundle.addfile(member)
            elif kind == "file":
                member.size = len(value)
                member.mode = 0o4755
                bundle.addfile(member, io.BytesIO(value))
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = value
                bundle.addfile(member)
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = value
                bundle.addfile(member)
            else:
                raise AssertionError(f"unknown fixture entry: {kind}")
    return output.getvalue()


def network_for(
    bootstrap_node, archive, *, manifest=None, filename="node-v24.1.2-linux-x64.tar.gz"
):
    digest = hashlib.sha256(archive).hexdigest()
    manifest = manifest if manifest is not None else f"{digest}  {filename}\n".encode()
    responses = {
        bootstrap_node.MANIFEST_URL: manifest,
        bootstrap_node.DIST_URL + filename: archive,
    }

    def open_url(url):
        return Response(responses[url])

    return open_url


def assert_no_staging_files(parent):
    assert list(parent.iterdir()) == []


def test_successful_verified_extraction(tmp_path, bootstrap_node):
    root = "node-v24.1.2-linux-x64"
    archive = make_tar(
        [
            ("dir", root, None),
            ("dir", f"{root}/bin", None),
            ("file", f"{root}/bin/node", b"tiny node"),
            ("dir", f"{root}/lib", None),
            ("file", f"{root}/lib/npm.js", b"npm"),
            ("symlink", f"{root}/bin/npm", "../lib/npm.js"),
        ]
    )
    destination = tmp_path / "node"

    result = bootstrap_node.install_node(
        destination,
        system="Linux",
        machine="x86_64",
        open_url=network_for(bootstrap_node, archive),
    )

    assert result == destination
    assert (destination / "bin/node").read_bytes() == b"tiny node"
    assert (destination / "bin/npm").is_symlink()
    assert (destination / "bin/npm").read_bytes() == b"npm"
    assert (destination / "bin/node").stat().st_mode & 0o6000 == 0
    assert {item.name for item in tmp_path.iterdir()} == {"node"}


def test_checksum_failure_cleans_staging(tmp_path, bootstrap_node):
    root = "node-v24.1.2-linux-x64"
    archive = make_tar([("dir", root, None)])
    filename = f"{root}.tar.gz"
    bad_manifest = f"{'0' * 64}  {filename}\n".encode()

    with pytest.raises(bootstrap_node.BootstrapError, match="checksum mismatch"):
        bootstrap_node.install_node(
            tmp_path / "node",
            system="Linux",
            machine="x86_64",
            open_url=network_for(bootstrap_node, archive, manifest=bad_manifest),
        )

    assert_no_staging_files(tmp_path)


@pytest.mark.parametrize(
    "manifest",
    [
        b"not a checksum manifest\n",
        f"{'a' * 64} node-v24.1.2-linux-x64.tar.gz\n".encode(),
        b"\xff",
    ],
)
def test_malformed_manifest_is_rejected(tmp_path, bootstrap_node, manifest):
    def open_url(_url):
        return Response(manifest)

    with pytest.raises(bootstrap_node.BootstrapError, match="manifest"):
        bootstrap_node.install_node(
            tmp_path / "node", system="Linux", machine="x86_64", open_url=open_url
        )
    assert_no_staging_files(tmp_path)


@pytest.mark.parametrize(("system", "machine"), [("Windows", "x86_64"), ("Linux", "riscv64")])
def test_unsupported_platform_does_not_use_network(tmp_path, bootstrap_node, system, machine):
    def unexpected(_url):
        pytest.fail("network must not be used for an unsupported platform")

    with pytest.raises(bootstrap_node.BootstrapError, match="unsupported platform"):
        bootstrap_node.install_node(
            tmp_path / "node", system=system, machine=machine, open_url=unexpected
        )


@pytest.mark.parametrize(
    "bad_entry",
    [
        ("file", "node-v24.1.2-linux-x64/../../escaped", b"bad"),
        ("file", "/absolute", b"bad"),
    ],
)
def test_traversal_is_rejected_and_cleaned(tmp_path, bootstrap_node, bad_entry):
    root = "node-v24.1.2-linux-x64"
    archive = make_tar([("dir", root, None), bad_entry])

    with pytest.raises(bootstrap_node.BootstrapError, match="archive"):
        bootstrap_node.install_node(
            tmp_path / "node",
            system="Linux",
            machine="x86_64",
            open_url=network_for(bootstrap_node, archive),
        )

    assert_no_staging_files(tmp_path)
    assert not (tmp_path.parent / "escaped").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_escaping_links_are_rejected_and_cleaned(tmp_path, bootstrap_node, kind):
    root = "node-v24.1.2-linux-x64"
    archive = make_tar([("dir", root, None), (kind, f"{root}/escape", "../../outside")])

    with pytest.raises(bootstrap_node.BootstrapError, match="link escapes"):
        bootstrap_node.install_node(
            tmp_path / "node",
            system="Linux",
            machine="x86_64",
            open_url=network_for(bootstrap_node, archive),
        )

    assert_no_staging_files(tmp_path)


def test_existing_destination_is_untouched_without_network(tmp_path, bootstrap_node):
    destination = tmp_path / "node"
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_text("original")

    def unexpected(_url):
        pytest.fail("network must not be used when the destination exists")

    with pytest.raises(FileExistsError):
        bootstrap_node.install_node(destination, open_url=unexpected)
    assert sentinel.read_text() == "original"


def test_partial_archive_download_is_cleaned(tmp_path, bootstrap_node):
    root = "node-v24.1.2-linux-x64"
    filename = f"{root}.tar.gz"
    manifest = f"{'a' * 64}  {filename}\n".encode()

    class BrokenResponse(Response):
        def read(self, _size=-1):
            if self.tell() == 0:
                return super().read(5)
            raise OSError("connection lost")

    def open_url(url):
        if url == bootstrap_node.MANIFEST_URL:
            return Response(manifest)
        return BrokenResponse(b"partial archive")

    with pytest.raises(bootstrap_node.BootstrapError, match="download failed"):
        bootstrap_node.install_node(
            tmp_path / "node", system="Linux", machine="x86_64", open_url=open_url
        )

    assert_no_staging_files(tmp_path)


def test_symlink_may_not_leave_the_node_root_even_within_staging(tmp_path, bootstrap_node):
    root = "node-v24.1.2-linux-x64"
    archive = make_tar(
        [
            ("dir", root, None),
            ("dir", f"{root}/bin", None),
            ("symlink", f"{root}/bin/npm", "../../outside"),
        ]
    )
    with pytest.raises(bootstrap_node.BootstrapError, match="escapes the Node directory"):
        bootstrap_node.install_node(
            tmp_path / "node",
            system="Linux",
            machine="x86_64",
            open_url=network_for(bootstrap_node, archive),
        )
    assert_no_staging_files(tmp_path)


def test_member_count_and_expansion_limits_are_checked_during_scan(
    tmp_path, bootstrap_node, monkeypatch
):
    root = "node-v24.1.2-linux-x64"
    archive = make_tar([("dir", root, None), ("file", f"{root}/large", b"0123456789")])
    monkeypatch.setattr(bootstrap_node, "MAX_EXTRACTED_BYTES", 5)
    with pytest.raises(bootstrap_node.BootstrapError, match="expands beyond"):
        bootstrap_node.install_node(
            tmp_path / "node",
            system="Linux",
            machine="x86_64",
            open_url=network_for(bootstrap_node, archive),
        )
    assert_no_staging_files(tmp_path)
    monkeypatch.setattr(bootstrap_node, "MAX_EXTRACTED_BYTES", 1000)
    monkeypatch.setattr(bootstrap_node, "MAX_MEMBERS", 1)
    with pytest.raises(bootstrap_node.BootstrapError, match="too many entries"):
        bootstrap_node.install_node(
            tmp_path / "node",
            system="Linux",
            machine="x86_64",
            open_url=network_for(bootstrap_node, archive),
        )
    assert_no_staging_files(tmp_path)


def test_insecure_and_credential_redirects_are_never_followed(bootstrap_node):
    import urllib.error

    handler = bootstrap_node._HTTPSRedirectHandler()
    for url in ["http://nodejs.org/file", "https://user:secret@nodejs.org/file"]:
        with pytest.raises(urllib.error.URLError, match="insecure redirect"):
            handler.redirect_request(None, None, 302, "redirect", {}, url)


def test_manifest_size_limit_is_enforced_before_download(bootstrap_node):
    response = Response(b"0123456789")
    with pytest.raises(bootstrap_node.BootstrapError, match="allowed size"):
        bootstrap_node._download(bootstrap_node.MANIFEST_URL, 5, lambda _: response)


def test_darwin_arm64_selects_its_own_verified_archive(tmp_path, bootstrap_node):
    root = "node-v24.1.2-darwin-arm64"
    archive = make_tar(
        [
            ("dir", root, None),
            ("dir", f"{root}/bin", None),
            ("file", f"{root}/bin/node", b"mac binary"),
        ]
    )
    destination = bootstrap_node.install_node(
        tmp_path / "node",
        system="Darwin",
        machine="arm64",
        open_url=network_for(bootstrap_node, archive, filename=root + ".tar.gz"),
    )
    assert (destination / "bin/node").read_bytes() == b"mac binary"
