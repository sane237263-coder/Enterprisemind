"""Offline TLS certificate and key checks for deployment preflight.

This checks local file access, certificate dates and SAN identity, and whether
OpenSSL can load the certificate with its private key. On Linux it also checks
the key's permission bits. It does not verify chain trust, the container
consumer's UID access, a configured listener, or a TLS handshake.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
import stat
import sys
import time
from pathlib import Path


def _inspect_file(
    path: Path | None,
    *,
    missing_label: str,
    type_label: str,
    unreadable_label: str,
    errors: list[str],
) -> os.stat_result | None:
    if path is None:
        errors.append(missing_label)
        return None
    try:
        file_stat = path.stat()
    except (OSError, ValueError):
        errors.append(missing_label)
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        errors.append(type_label)
        return None
    try:
        with path.open("rb") as stream:
            stream.read(1)
    except (OSError, ValueError):
        errors.append(unreadable_label)
        return None
    return file_stat


def _key_mode_is_safe(mode: int) -> bool:
    return bool(mode & stat.S_IRUSR) and not bool(mode & 0o077)


def _host_matches_san(public_host: str, subject_alt_names: tuple[tuple[str, str], ...]) -> bool:
    try:
        wanted_ip = ipaddress.ip_address(public_host)
    except ValueError:
        wanted_ip = None

    for kind, value in subject_alt_names:
        if kind == "DNS" and wanted_ip is None and value.casefold() == public_host.casefold():
            return True
        if kind == "IP Address" and wanted_ip is not None:
            try:
                if ipaddress.ip_address(value) == wanted_ip:
                    return True
            except ValueError:
                continue
    return False


def _no_key_password() -> bytes:
    """Reject encrypted keys without prompting or trying an implicit password."""
    raise ValueError("encrypted private keys are not accepted")


def check_tls_contract(
    cert_path: Path | None,
    key_path: Path | None,
    public_host: str,
) -> list[str]:
    """Return fixed, redacted labels for TLS material that fails offline checks."""
    errors: list[str] = []
    cert_stat = _inspect_file(
        cert_path,
        missing_label="tls_cert_missing",
        type_label="tls_cert_not_regular_file",
        unreadable_label="tls_cert_unreadable",
        errors=errors,
    )
    key_stat = _inspect_file(
        key_path,
        missing_label="tls_key_missing",
        type_label="tls_key_not_regular_file",
        unreadable_label="tls_key_unreadable",
        errors=errors,
    )

    if cert_stat is not None:
        try:
            decoded = ssl._ssl._test_decode_cert(str(cert_path))
        except (AttributeError, OSError, OverflowError, ssl.SSLError, TypeError, ValueError):
            errors.append("tls_cert_decode_failed")
        else:
            try:
                not_before = ssl.cert_time_to_seconds(decoded["notBefore"])
                not_after = ssl.cert_time_to_seconds(decoded["notAfter"])
            except (KeyError, TypeError, ValueError, OverflowError):
                errors.append("tls_cert_validity_unavailable")
            else:
                current_time = time.time()
                if not_before > current_time:
                    errors.append("tls_cert_not_yet_valid")
                if not_after < current_time:
                    errors.append("tls_cert_expired")

            if not public_host:
                errors.append("tls_public_host_missing")
            elif not _host_matches_san(public_host, tuple(decoded.get("subjectAltName", ()))):
                errors.append("tls_cert_san_mismatch")

    if key_stat is not None and sys.platform.startswith("linux"):
        if not _key_mode_is_safe(key_stat.st_mode):
            errors.append("tls_key_permissions_unsafe")

    if cert_stat is not None and key_stat is not None:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                certfile=str(cert_path),
                keyfile=str(key_path),
                password=_no_key_password,
            )
        except (AttributeError, OSError, OverflowError, ssl.SSLError, TypeError, ValueError):
            errors.append("tls_cert_key_pair_invalid")

    return errors
