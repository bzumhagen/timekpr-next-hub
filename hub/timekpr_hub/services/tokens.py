"""Hashing bearer tokens (device tokens, admin session/invite tokens)
before they touch the database -- only the hash is ever stored, so a
database leak doesn't hand out usable credentials.
"""

from __future__ import annotations

import hashlib


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
