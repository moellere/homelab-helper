"""Deterministic finding fingerprints.

Per the locked schema decisions: ``sha256((kind || "|" || target_type || "|" ||
target_id || "|" || root_cause_token).encode("utf-8")).hexdigest()[:16]``. 16 hex
chars = 64 bits of dedup space; pipe separator avoids ambiguity across
``target_id`` values that might contain colons or slashes.

The ``root_cause_token`` is per-finding-kind logic — callers compute it and
pass it in. This module supplies only the hashing primitive so every finding
generator hashes consistently.
"""

from __future__ import annotations

import hashlib

_FINGERPRINT_LEN = 16


def make_fingerprint(
    kind: str,
    target_type: str,
    target_id: str,
    root_cause_token: str,
) -> str:
    """Return a stable 16-hex-char fingerprint for a finding.

    Inputs are joined with ``|`` and UTF-8 encoded before hashing so the digest
    is portable across platforms and Python versions.
    """

    payload = f"{kind}|{target_type}|{target_id}|{root_cause_token}".encode()
    return hashlib.sha256(payload).hexdigest()[:_FINGERPRINT_LEN]


__all__ = ["make_fingerprint"]
