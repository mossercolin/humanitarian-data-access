"""Shared safety boundary for remote HTTP data used by HDA.

This module sits between HDA and data received from the Internet. Its job is to
turn remote input into something the rest of HDA can use without silently
accepting unsafe addresses, unexpected redirects, oversized downloads,
incomplete transfers, or unusually large/non-standard JSON numbers.

The flow is intentionally simple:

1. Remote JSON is decoded under deterministic numeric limits.
2. Remote URLs are checked and reduced to an "origin" (scheme, host, port).
3. HTTP access is allowed only to the starting origin and explicitly approved
   redirect origins; HTTPS is never allowed to downgrade to HTTP.
4. Response bodies are read either in one bounded operation or as caller-managed
   streaming chunks.
5. Declared response lengths are checked so truncated transfers are not mistaken
   for complete data.

The implementation uses only Python's standard library. Policy refusals and
remote-input validation failures are expressed as ValueError subclasses so
callers receive deterministic failures instead of silently accepting bad input.
"""

# Keep type descriptions unevaluated at module load for typing compatibility;
# this affects annotations only, not HTTP behavior.
from __future__ import annotations

# http.client supplies the transfer-level exceptions used to recognize a body
# that ended incorrectly or could not be read as a valid HTTP response.
import http.client

# json performs the actual JSON decoding; HDA supplies custom numeric handlers
# below so remote numbers cannot bypass the limits defined by this module.
import json

# math.isfinite lets HDA reject floating-point values such as infinity that are
# technically representable by Python but are not acceptable remote JSON data.
import math

# URL tools serve two different roles here: urllib.parse separates a remote
# address into scheme/host/port for trust decisions, while urllib.request opens
# the HTTP connection and gives HDA a hook to approve or refuse redirects.
import urllib.parse
import urllib.request

# Typing names describe accepted shapes: Iterable covers additional approved
# origin URLs, BinaryIO a byte-producing response stream, Mapping header-like
# key/value data, and Any JSON or callback objects with varying concrete types.
from collections.abc import Iterable
from typing import Any, BinaryIO, Mapping


class ResponseTooLarge(ValueError):
    """The remote body is larger than the caller-approved byte limit.

    This is a deliberate safety refusal: the caller chose a maximum amount of
    remote data for one operation, and this exception means that boundary would
    have been crossed.
    """


class IncompleteResponse(ValueError):
    """The remote HTTP body did not arrive as a complete usable response.

    HDA raises this when the transport fails while reading or when a server says
    a body has a certain length but fewer bytes actually arrive. The purpose is
    to prevent truncated remote data from being treated as complete data.
    """


# Remote JSON numbers can be extremely large even when the overall document is
# syntactically valid. HDA therefore fixes a deterministic token ceiling here
# instead of relying only on interpreter configuration that may vary by machine.
# The same limit is used by both integer and floating-point token handlers.
MAX_REMOTE_JSON_NUMERIC_TOKEN = 4300


# Integer tokens are checked before Python converts them into integer objects.
# A leading minus sign describes the sign, not another numeric digit, so it is
# excluded from the digit count. Tokens beyond the shared ceiling are refused.
def _remote_json_int(token: str) -> int:
    if len(token) - token.startswith("-") > MAX_REMOTE_JSON_NUMERIC_TOKEN:
        raise ValueError(
            f"remote JSON integer token exceeds {MAX_REMOTE_JSON_NUMERIC_TOKEN} decimal digits"
        )
    return int(token)


# Floating-point tokens receive the same length defense, then one additional
# semantic check: after conversion, the value must be finite. This keeps remote
# infinity-like values out of HDA even if Python can represent them internally.
def _remote_json_float(token: str) -> float:
    if len(token) > MAX_REMOTE_JSON_NUMERIC_TOKEN:
        raise ValueError(
            f"remote JSON float token exceeds {MAX_REMOTE_JSON_NUMERIC_TOKEN} characters"
        )
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("remote JSON float is not finite")
    return value


# Python's JSON decoder can recognize non-standard constants such as NaN and
# Infinity. HDA deliberately rejects them so remote data must use standard JSON
# numeric values rather than interpreter-specific extensions.
def _reject_remote_json_constant(token: str) -> Any:
    raise ValueError(f"remote JSON non-standard numeric constant is not permitted: {token}")


def remote_json_loads(document: str | bytes | bytearray) -> Any:
    """Turn an untrusted remote JSON document into Python data under HDA rules.

    WHAT: decode JSON received from a remote source.
    WHY: ordinary decoding alone would also accept numeric forms or token sizes
    that HDA does not want to trust blindly.
    HOW: the decoder is given the bounded integer/float handlers above and a
    rejector for non-standard numeric constants. Excessive parser nesting is
    translated into a normal validation failure for the caller.

    The returned value is the parsed document that later source-specific code
    can inspect; no source-specific schema assumptions are made here.
    """
    try:
        return json.loads(
            document,
            parse_int=_remote_json_int,
            parse_float=_remote_json_float,
            parse_constant=_reject_remote_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("remote JSON exceeds parser nesting capacity") from exc


# ---------------------------------------------------------------------------
# URL AND ORIGIN VALIDATION
#
# Before HDA opens a connection, it establishes that the address is really an
# absolute HTTP(S) address with a usable host and port. The next helper then
# converts that address into an "origin": scheme + hostname + port. Origins are
# what the redirect policy compares, because paths on the same trusted server do
# not represent a new network destination.
# ---------------------------------------------------------------------------


def require_http_url(url: str) -> str:
    """Validate that a remote address is a usable absolute HTTP(S) URL.

    WHAT: check the network address before it can participate in HTTP access.
    WHY: malformed addresses, non-web schemes, missing hosts, or invalid ports
    must fail before they can influence redirect/trust decisions.
    HOW: split the address into standard URL components, require HTTP or HTTPS,
    require both an authority and actual hostname, and force explicit ports to
    pass Python's port validation.

    The original URL text is returned unchanged after validation so the network
    layer can use exactly the address the caller supplied.
    """
    # Phase 1: parse the text into URL components. Some malformed authorities
    # are rejected by the parser itself, and HDA exposes those as validation
    # failures rather than letting them escape as parser-specific surprises.
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"remote HTTP(S) URL is invalid: {url!r}") from exc

    # Phase 2: accept only the two network schemes this module is designed to
    # open. Local files and every other scheme stay outside this remote boundary.
    if parsed.scheme.lower() not in {"http", "https"}:
        scheme = parsed.scheme or "(missing)"
        raise ValueError(f"remote URL scheme must be http or https; got {scheme!r}")

    # Phase 3: require a real network destination. A textual authority alone is
    # not enough if it does not resolve into an actual hostname.
    if not parsed.netloc:
        raise ValueError("remote HTTP(S) URL must include a network location")
    if parsed.hostname is None:
        raise ValueError("remote HTTP(S) URL must include a hostname")

    # Phase 4: asking the URL parser for the port performs its range/syntax
    # validation. The value itself is not changed here; explicit ports, including
    # port 0, remain explicit and are interpreted later by the origin helper.
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"remote HTTP(S) URL has an invalid port: {url!r}") from exc
    return url


def _origin(url: str) -> tuple[str, str, int]:
    """Reduce a validated URL to the network identity used for trust decisions.

    WHAT: produce the tuple (scheme, hostname, port).
    WHY: redirect approval must compare network destinations, not full paths.
    HOW: validate and split the URL, normalize scheme/hostname case, and assign
    the conventional port only when the caller omitted a port entirely.

    Explicit ports remain distinct. For example, an explicit ``:0`` is not
    silently rewritten as port 80 or 443.
    """
    # Start from the same public URL validation contract used by callers, then
    # extract the components needed to identify a network origin.
    parsed = urllib.parse.urlsplit(require_http_url(url))
    scheme = parsed.scheme.lower()
    if parsed.hostname is None:
        raise ValueError("remote HTTP(S) URL must include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"remote HTTP(S) URL has an invalid port: {url!r}") from exc

    # An omitted port means the normal port for the chosen scheme. An explicit
    # port is part of the origin identity and must be preserved exactly.
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parsed.hostname.lower(), port


# ---------------------------------------------------------------------------
# REDIRECT TRUST POLICY AND NETWORK OPEN
#
# A caller starts from one reviewed origin and may optionally approve additional
# origins that a legitimate source is known to redirect to. Every redirect is
# checked before it is followed. The final response is checked once more as a
# backstop so HDA never returns a response from an origin outside that approved
# set. HTTPS is never allowed to downgrade to plain HTTP.
# ---------------------------------------------------------------------------


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Apply HDA's origin allow-list whenever urllib proposes an HTTP redirect.

    The handler receives the already-approved origin set from ``open_http``.
    For each redirect it identifies the previous and proposed destinations,
    refuses HTTPS-to-HTTP downgrades, and refuses any destination outside that
    set. Only a redirect that passes both trust decisions is handed back to
    urllib to continue normally.
    """

    def __init__(self, allowed_origins: set[tuple[str, str, int]]):
        # Store the complete origin allow-list once so every redirect in this
        # request is judged against exactly the same reviewed destinations.
        self.allowed_origins = allowed_origins

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Mapping[str, str], newurl: str) -> urllib.request.Request | None:
        # Phase 1: translate both sides of the redirect into origin identities.
        previous = _origin(req.full_url)
        destination = _origin(newurl)

        # Phase 2: preserve transport security. An HTTPS request may not be
        # redirected onto unencrypted HTTP even if that HTTP origin was listed.
        if previous[0] == "https" and destination[0] != "https":
            raise ValueError(f"refusing HTTPS-to-HTTP redirect to {newurl!r}")

        # Phase 3: preserve destination trust. Redirects may reach only the
        # starting origin or additional origins explicitly approved by the caller.
        if destination not in self.allowed_origins:
            raise ValueError(f"refusing redirect to unapproved origin {destination!r}")

        # The trust checks are complete; urllib can now build/follow the redirect
        # using its normal HTTP behavior.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_http(request: urllib.request.Request | str, timeout: float,
              allowed_origins: Iterable[str] = ()) -> Any:
    """Open an HTTP(S) request without letting redirects escape reviewed origins.

    WHAT: perform the remote HTTP open used by HDA callers.
    WHY: a server may redirect after the caller chooses a URL, so validating only
    the starting address would not be enough to preserve the trust decision.
    HOW: validate the starting origin, build an approved origin set from it plus
    caller-supplied redirect destinations, install the redirect policy above,
    open the request, and verify the final response origin once more.

    ``allowed_origins`` therefore means additional redirect destinations that
    the caller has explicitly reviewed. It never authorizes an HTTPS-to-HTTP
    downgrade. The returned response remains the caller's response object to
    read and close.
    """
    # Phase 1: determine the origin the caller intentionally chose, then expand
    # the allow-list only with additional origins the caller explicitly supplied.
    initial_url = request.full_url if isinstance(request, urllib.request.Request) else request
    initial_origin = _origin(initial_url)
    origins = {initial_origin, *(_origin(url) for url in allowed_origins)}

    # Phase 2: open the request through the redirect handler. Redirects are
    # therefore checked before urllib follows them.
    response = urllib.request.build_opener(_TrustedRedirectHandler(origins)).open(request, timeout=timeout)

    # Phase 3: independently inspect where the response actually ended. This is
    # a deliberate defense-in-depth backstop, not a replacement for per-redirect
    # checks. If a transport path bypassed redirect handling, it still cannot
    # hand an unapproved final origin back to HDA.
    final_url = response.geturl()
    final_origin = _origin(final_url)
    if initial_origin[0] == "https" and final_origin[0] != "https":
        response.close()
        raise ValueError(f"refusing HTTPS-to-HTTP final response URL {final_url!r}")
    if final_origin not in origins:
        response.close()
        raise ValueError(f"refusing final response from unapproved origin {final_origin!r}")

    # At this point both the redirect path and final destination satisfy HDA's
    # trust policy. The caller receives the still-open response for bounded read.
    return response


# ---------------------------------------------------------------------------
# RESPONSE-LENGTH INTERPRETATION
#
# HTTP servers may declare the expected body size in Content-Length. HDA treats
# a usable non-negative declaration as evidence for two checks: reject a body
# known in advance to exceed the caller's limit, and later detect a body that
# ended before the declared number of bytes arrived. Missing or malformed length
# information is treated as unavailable, so actual byte counting remains the
# final size boundary.
# ---------------------------------------------------------------------------


def _declared_length(headers: Mapping[str, str] | None) -> int | None:
    """Return a usable non-negative Content-Length, or report that none is usable.

    WHAT: interpret the server's declared body length.
    WHY: callers can use a valid declaration for early size refusal and for
    detecting truncated transfers, but malformed metadata must not crash this
    low-level helper or be mistaken for trustworthy size information.
    HOW: read Content-Length, convert it to an integer, and keep it only when it
    is non-negative. Missing, malformed, or negative values become ``None``.
    """
    # No headers means there is no declared length available for early or final
    # completeness checks.
    if headers is None:
        return None

    # Extract and interpret the declaration. Invalid text is deliberately treated
    # as absent rather than as reliable size information.
    value = headers.get("Content-Length")
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


# ---------------------------------------------------------------------------
# ONE-SHOT BODY READING
#
# For responses expected to fit comfortably in memory, HDA reads the body once.
# The caller provides the maximum acceptable byte count. HDA first uses a valid
# Content-Length for an early refusal, then reads at most one byte beyond the
# limit so an oversized body can be detected without consuming it without bound.
# Finally, declared length is compared with bytes received to detect truncation.
# ---------------------------------------------------------------------------


def read_limited(
    stream: BinaryIO,
    limit: int,
    operation: str,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """Read one complete remote body without crossing the caller's byte limit.

    WHAT: obtain a non-streaming response body as bytes.
    WHY: remote servers control how much data they send, so HDA must enforce the
    operation's memory/size boundary and must not accept a truncated body.
    HOW: require a positive limit, reject an excessive declared length before
    reading, read only ``limit + 1`` bytes to detect overflow, normalize transfer
    failures, and compare received bytes with any usable Content-Length.

    A successful return is the complete body, known not to exceed ``limit``.
    """
    # Phase 1: the caller's configured boundary must itself be meaningful.
    if limit < 1:
        raise ValueError(f"{operation} byte limit must be positive")

    # Phase 2: when the server truthfully declares an already-excessive body,
    # refuse before allocating/reading that body at all.
    declared = _declared_length(headers)
    if declared is not None and declared > limit:
        raise ResponseTooLarge(
            f"{operation} exceeds the {limit}-byte limit (Content-Length: {declared})"
        )

    # Phase 3: read no more than the allowed body plus one detection byte. That
    # extra byte answers one question only: did the remote body exceed the limit?
    try:
        body = stream.read(limit + 1)
    except (http.client.IncompleteRead, http.client.HTTPException, OSError) as exc:
        raise IncompleteResponse(f"{operation} HTTP response body was incomplete or malformed") from exc

    # Phase 4: if the detection byte exists, the caller's limit was crossed.
    if len(body) > limit:
        raise ResponseTooLarge(f"{operation} exceeds the {limit}-byte limit")

    # Phase 5: a body can be under the size limit yet still be incomplete. When
    # the server declared a longer body than actually arrived, reject truncation.
    if declared is not None and len(body) < declared:
        raise IncompleteResponse(
            f"{operation} HTTP response body was incomplete "
            f"(Content-Length: {declared}, received: {len(body)})"
        )

    # All one-shot size and completeness checks passed; this body is ready for
    # the source-specific parser that called this shared boundary.
    return body


# ---------------------------------------------------------------------------
# STREAMING BODY READING
#
# Large responses may be processed piece by piece instead of loaded all at once.
# In that mode this module guarantees each requested chunk size is positive and
# normalizes transfer failures. The caller owns the streaming loop and total-size
# accounting, then calls require_complete_length() with the total bytes received
# so a truncated declared body can still be refused.
# ---------------------------------------------------------------------------


def read_http_chunk(stream: BinaryIO, size: int, operation: str) -> bytes:
    """Read one caller-sized piece of a larger HTTP response safely.

    WHAT: perform one read within a caller-managed streaming loop.
    WHY: size 0 can make a loop appear not to progress or resemble EOF, while a
    negative size can request all remaining data from the stream.
    HOW: require a positive size so each read stays bounded, then translate
    incomplete/malformed transport failures into ``IncompleteResponse``.

    This function does not decide the whole response size. After the stream ends,
    the caller passes its accumulated byte count to ``require_complete_length``.
    """
    # Phase 1: protect the streaming contract. Zero can resemble EOF or stalled
    # progress; a negative size can request all remaining data. Positive sizes
    # keep each individual read bounded.
    if size <= 0:
        raise ValueError(f"{operation} chunk size must be positive")

    # Phase 2: perform exactly one streaming read and normalize transfer-level
    # failures so callers do not need to understand several HTTP/OS exceptions.
    try:
        return stream.read(size)
    except (http.client.IncompleteRead, http.client.HTTPException, OSError) as exc:
        raise IncompleteResponse(f"{operation} HTTP response body was incomplete or malformed") from exc


def require_complete_length(actual: int, headers: Mapping[str, str] | None, operation: str) -> None:
    """Finish the streaming path by checking the total bytes that actually arrived.

    WHAT: compare the caller's final streaming byte count with Content-Length.
    WHY: successful individual chunk reads do not prove that the server delivered
    the entire body it declared.
    HOW: interpret the same Content-Length rule used by one-shot reads and refuse
    only when a usable declaration says more bytes should have arrived.

    Returning normally means this declared-length completion check passed; the
    caller can continue with its already-streamed result.
    """
    # Phase 1: recover any usable declared total from the response metadata.
    declared = _declared_length(headers)

    # Phase 2: compare the declaration with the caller's accumulated byte count.
    # Fewer received bytes means the remote response ended prematurely.
    if declared is not None and actual < declared:
        raise IncompleteResponse(
            f"{operation} HTTP response body was incomplete "
            f"(Content-Length: {declared}, received: {actual})"
        )
