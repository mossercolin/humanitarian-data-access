"""Small standard-library boundaries for HDA HTTP reads."""

from __future__ import annotations

import urllib.parse
import urllib.request
import http.client
import json
import math
from collections.abc import Iterable
from typing import Any, BinaryIO, Mapping


class ResponseTooLarge(ValueError):
    """Raised when a remote response exceeds its operation-specific limit."""


class IncompleteResponse(ValueError):
    """Raised when a remote HTTP response body did not complete."""


MAX_REMOTE_JSON_NUMERIC_TOKEN = 4300


def _remote_json_int(token: str) -> int:
    if len(token) - token.startswith("-") > MAX_REMOTE_JSON_NUMERIC_TOKEN:
        raise ValueError("remote JSON integer token exceeds 4300 decimal digits")
    return int(token)


def _remote_json_float(token: str) -> float:
    if len(token) > MAX_REMOTE_JSON_NUMERIC_TOKEN:
        raise ValueError("remote JSON float token exceeds 4300 characters")
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("remote JSON float is not finite")
    return value


def _reject_remote_json_constant(token: str) -> Any:
    raise ValueError(f"remote JSON non-standard numeric constant is not permitted: {token}")


def remote_json_loads(document: str | bytes | bytearray) -> Any:
    """Decode remote JSON with bounded, finite numeric tokens."""
    try:
        return json.loads(
            document,
            parse_int=_remote_json_int,
            parse_float=_remote_json_float,
            parse_constant=_reject_remote_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("remote JSON exceeds parser nesting capacity") from exc


def require_http_url(url: str) -> str:
    """Return *url* when it is absolute HTTP(S), otherwise refuse it."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        scheme = parsed.scheme or "(missing)"
        raise ValueError(f"remote URL scheme must be http or https; got {scheme!r}")
    if not parsed.netloc:
        raise ValueError("remote HTTP(S) URL must include a network location")
    return url


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(require_http_url(url))
    scheme = parsed.scheme.lower()
    if parsed.hostname is None:
        raise ValueError("remote HTTP(S) URL must include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"remote HTTP(S) URL has an invalid port: {url!r}") from exc
    return scheme, parsed.hostname.lower(), port or (443 if scheme == "https" else 80)


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origins: set[tuple[str, str, int]]):
        self.allowed_origins = allowed_origins

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Mapping[str, str], newurl: str) -> urllib.request.Request | None:
        previous = _origin(req.full_url)
        destination = _origin(newurl)
        if previous[0] == "https" and destination[0] != "https":
            raise ValueError(f"refusing HTTPS-to-HTTP redirect to {newurl!r}")
        if destination not in self.allowed_origins:
            raise ValueError(f"refusing redirect to unapproved origin {destination!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_http(request: urllib.request.Request | str, timeout: float,
              allowed_origins: Iterable[str] = ()) -> Any:
    """Open HTTP(S) with redirect destinations restricted to reviewed origins."""
    initial_url = request.full_url if isinstance(request, urllib.request.Request) else request
    initial_origin = _origin(initial_url)
    origins = {initial_origin, *(_origin(url) for url in allowed_origins)}
    response = urllib.request.build_opener(_TrustedRedirectHandler(origins)).open(request, timeout=timeout)
    final_url = response.geturl()
    final_origin = _origin(final_url)
    if initial_origin[0] == "https" and final_origin[0] != "https":
        response.close()
        raise ValueError(f"refusing HTTPS-to-HTTP final response URL {final_url!r}")
    if final_origin not in origins:
        response.close()
        raise ValueError(f"refusing final response from unapproved origin {final_origin!r}")
    return response


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
    try:
        body = stream.read(limit + 1)
    except (http.client.IncompleteRead, http.client.HTTPException, OSError) as exc:
        raise IncompleteResponse(f"{operation} HTTP response body was incomplete or malformed") from exc
    if len(body) > limit:
        raise ResponseTooLarge(
            f"{operation} exceeds the {limit}-byte limit; "
            "use the applicable larger-limit option if this resource is expected"
        )
    if declared is not None and len(body) < declared:
        raise IncompleteResponse(
            f"{operation} HTTP response body was incomplete "
            f"(Content-Length: {declared}, received: {len(body)})"
        )
    return body


def read_http_chunk(stream: BinaryIO, size: int, operation: str) -> bytes:
    """Read one streaming response chunk with deterministic transfer failures."""
    try:
        return stream.read(size)
    except (http.client.IncompleteRead, http.client.HTTPException, OSError) as exc:
        raise IncompleteResponse(f"{operation} HTTP response body was incomplete or malformed") from exc


def require_complete_length(actual: int, headers: Mapping[str, str] | None, operation: str) -> None:
    """Reject a completed stream shorter than a usable declared Content-Length."""
    declared = _declared_length(headers)
    if declared is not None and actual < declared:
        raise IncompleteResponse(
            f"{operation} HTTP response body was incomplete "
            f"(Content-Length: {declared}, received: {actual})"
        )
