"""Time-ordered, URL-safe identifiers.

Layout: <prefix>-<10 chars of millisecond timestamp><12 random chars>, all in
Crockford base32 (no I, L, O, U). Lexicographic order == creation order, which
keeps listings naturally sorted without an extra index attribute.
"""

from __future__ import annotations

import re
import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_ID_RE = re.compile(r"^net-[0-9a-hjkmnp-tv-z]{22}$")


def _encode(value: int, width: int) -> str:
    chars = []
    for _ in range(width):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def new_network_id(now_ms: int | None = None) -> str:
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    rand = secrets.randbits(60)
    return f"net-{_encode(ts, 10)}{_encode(rand, 12)}"


def is_network_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))
