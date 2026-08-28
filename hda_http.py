"""Small standard-library boundaries for HDA HTTP reads."""

from __future__ import annotations

import urllib.parse
from typing import BinaryIO, Mapping


class ResponseTooLarge(ValueError):
    """Raised when a remote response exceeds its operation-specific limit."""


def require_http_url(url: str) -> str:
    """Return *url* when it is absolute HTTP(S), otherwise refuse it."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        scheme = parsed.scheme or "(missing)"
        raise ValueError(f"remote URL scheme must be http or https; got {scheme!r}")
    if not parsed.netloc:
        raise ValueError("remote HTTP(S) URL must include a network location")
    return url


def _declared_length(headers: Mapping[str, str] | None) -> int | None:
    if headers is None:
        return None
    value = headers.get("Content-Length")
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def read_limited(
    stream: BinaryIO,
    limit: int,
    operation: str,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """Read at most *limit* bytes plus one detection byte, or refuse."""
    if limit < 1:
        raise ValueError(f"{operation} byte limit must be positive")
    declared = _declared_length(headers)
    if declared is not None and declared > limit:
        raise ResponseTooLarge(
            f"{operation} exceeds the {limit}-byte limit (Content-Length: {declared}); "
            "use the applicable larger-limit option if this resource is expected"
        )
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise ResponseTooLarge(
            f"{operation} exceeds the {limit}-byte limit; "
            "use the applicable larger-limit option if this resource is expected"
        )
    return body
