"""Opaque refresh token generation/hashing (docs/system-design.md section 18.2).

The token handed to the client is a random 256-bit value; only its sha256 hash is
ever stored (`refresh_tokens.token_hash`), so a database leak doesn't hand out
usable tokens.
"""

import hashlib
import secrets


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
