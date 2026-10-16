"""Generate local self-signed TLS certificates without replacing existing identities."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path


def certificate_names(hosts: list[str]) -> list[str]:
    """Build validated OpenSSL SAN entries (never interpolate arbitrary config text)."""
    names = []
    for host in ["localhost", "127.0.0.1", "::1", socket.gethostname(), *hosts]:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                dns = host.rstrip(".").encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise ValueError(f"invalid certificate hostname: {host!r}") from exc
            if len(dns) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in dns.split(".")
            ):
                raise ValueError(f"invalid certificate hostname: {host!r}")
            entry = f"DNS:{dns}"
        else:
            if address.is_unspecified:
                continue  # A bind wildcard is not a client destination.
            if "%" in host:
                raise ValueError("certificate IP addresses must not include a scope ID")
            entry = f"IP:{address}"
        if entry not in names:
            names.append(entry)
    return names


def generate_if_absent(
    certfile: Path, keyfile: Path, *, hosts: list[str], days: int = 365
) -> bool:
    """Generate a matched pair, or reuse both files; never repair/overwrite half a pair."""
    if days < 1 or days > 36500:
        raise ValueError("certificate validity must be between 1 and 36500 days")
    names = certificate_names(hosts)
    if certfile.resolve() == keyfile.resolve():
        raise ValueError("certificate and private key must use different paths")
    if certfile.is_symlink() or keyfile.is_symlink():
        raise ValueError("automatic certificates must not be symbolic links")
    present = [path.exists() for path in (certfile, keyfile)]
    if all(present):
        if not all(path.is_file() for path in (certfile, keyfile)):
            raise ValueError("certificate and private key must be regular files")
        return False
    if any(present):
        raise ValueError(
            "only one TLS file exists; refusing to overwrite it. Restore the matching "
            "certificate/key or choose two new paths"
        )
    openssl = shutil.which("openssl")
    if not openssl:
        raise ValueError("certificate generation requires OpenSSL; install the openssl command")
    for parent in {certfile.parent, keyfile.parent}:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Staging stays private, including while OpenSSL writes an unencrypted key.
    with tempfile.TemporaryDirectory(prefix=".swarm-tls-", dir=keyfile.parent) as temporary:
        staging = Path(temporary)
        key = staging / "privkey.pem"
        cert = staging / "cert.pem"
        config = staging / "openssl.cnf"
        config.write_text(
            "[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = extensions\n"
            "[dn]\nCN = Swarm local TLS\n[extensions]\n"
            "basicConstraints = critical,CA:FALSE\n"
            "keyUsage = critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            f"subjectAltName = {','.join(names)}\n",
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
                 "-days", str(days), "-keyout", str(key), "-out", str(cert),
                 "-config", str(config)],
                check=True, capture_output=True, timeout=60,
            )
            # Verify the output is a loadable, matched pair before publishing it.
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError("OpenSSL could not generate a valid TLS certificate/key pair") from exc
        published = []
        try:
            for source, destination in ((key, keyfile), (cert, certfile)):
                # Exclusive creation handles concurrent starts without clobbering files.
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                published.append(destination)
                with os.fdopen(fd, "wb") as output:
                    output.write(source.read_bytes())
        except OSError:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    return True
