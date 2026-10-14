#!/usr/bin/env python3
"""One bounded anonymous FEWS NET marketpricefacts JSON page."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url

SOURCE_ID = "fews_net"
INTERFACE_ID = "fews_fdw_public"
ENDPOINT = "https://fdw.fews.net/api/marketpricefacts/"
DOCS_URL = "https://help.fews.net/fde/v3/fews-net-api"
DATA_LIMIT = 1024 * 1024
MAX_PAGE_SIZE = 100
MAX_STDOUT_RECORDS = 10
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
EMPTY_SCOPE = "No records returned under the current FEWS NET public access scope."


def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": "hda-fews/1.0"})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, DATA_LIMIT, "FEWS NET marketpricefacts response", response.headers)
        network = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": DATA_LIMIT, "final_response_url": response.geturl()}
    return remote_json_loads(body), network


def query(country_code: str, page_size: int = 5, offset: int = 0, stdout_records: int = MAX_STDOUT_RECORDS, timeout: float = 30.0) -> dict[str, Any]:
    # Read one anonymous/public FEWS NET marketpricefacts page. The caller supplies
    # an uppercase two-letter country code; shape validation does not establish
    # authoritative country support or publisher coverage. Request fields are fixed
    # to fields=simple and format=json.
    if not re.fullmatch(r"[A-Z]{2}", country_code):
        raise ValueError("country_code must be a two-letter uppercase country code")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if not 1 <= stdout_records <= MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout_records must be between 1 and {MAX_STDOUT_RECORDS}")
    if not 0 < timeout < float('inf'):
        raise ValueError("timeout must be positive and finite")
    parameters = {"country_code": country_code, "page_size": str(page_size), "offset": str(offset), "fields": "simple", "format": "json"}
    source_url = ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    payload, network = _request_json(source_url, timeout)
    # The expected object has a results array of record objects, preserving native
    # publisher fields without humanitarian interpretation. A recognizable "no data
    # found" response is access-scoped; an otherwise valid empty results array succeeds.
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        # Preserve recognizable publisher negatives as access-scoped errors, not global absence.
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        if isinstance(detail, str) and "no data found" in detail.lower():
            raise ValueError(f"{EMPTY_SCOPE} Publisher reported: {detail}")
        raise ValueError("FEWS NET response lacks the results record array")
    records = payload["results"]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("FEWS NET results array contains a non-object record")
    # page_size and offset select the requested publisher page; len(records) is what
    # arrived, while stdout_records selects local stdout. Pagination links are not followed.
    shown = min(len(records), stdout_records, page_size)
    # Request scope, source/interface identity, network observations, and retrieval time
    # accompany native records. HDA retrieval time is not publisher publication,
    # observation, or market-price period; a public success or empty page is not global coverage.
    return {
        "schema_version": "hda-fews-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "request_scope": {"endpoint": ENDPOINT, "parameters": parameters, "access_scope": "anonymous/public", "pages_requested": 1},
        "lineage": {"source": "FEWS NET", "official_docs": DOCS_URL, "endpoint": ENDPOINT, "source_url": source_url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        "network": {**network, "anonymous": True, "response_record_count": len(records), "publisher_pagination": {key: payload[key] for key in ("count", "next", "previous") if key in payload}, "next_followed": False},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown, "records": records[:shown], "scope_note": EMPTY_SCOPE if not records else "Records returned under the current FEWS NET public access scope."},
        "semantic_boundary": ["FEWS-native field names and values are preserved. Retrieval time is not publication, period, or collection time.", "Anonymous access may omit permissioned data; this page does not establish source-global coverage or absence."],
    }


def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    # Normal output is UTF-8 JSON plus a newline. The HTTP response body has a
    # 1 MiB limit (DATA_LIMIT); model-facing stdout defaults to 256 KiB
    # (DEFAULT_STDOUT_BYTES_LIMIT) and may be increased through the CLI.
    # Oversized serialized stdout becomes a provenance/count envelope without records;
    # if that envelope also exceeds the stdout limit, fail.
    normal = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(normal) <= limit:
        return normal
    envelope = {"schema_version": "hda-output-limit-envelope/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "request_scope": result["request_scope"], "provenance": {"lineage": result["lineage"], "network": result["network"]}, "counts": {"response_record_count": result["network"]["response_record_count"]}, "stdout_bytes_limit": limit, "normal_serialized_bytes": len(normal), "output_limit_exceeded": True, "data_omitted": True, "reason": "Publisher records/data were omitted because the serialized model-facing stdout limit was exceeded."}
    emitted = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(emitted) > limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit")
    return emitted


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    q = sub.add_parser("query", help="read one public marketpricefacts page (fields=simple)")
    q.add_argument("--country-code", required=True)
    q.add_argument("--page-size", type=_positive, default=5)
    q.add_argument("--offset", type=int, default=0)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT)
    args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        result = query(args.country_code, args.page_size, args.offset, args.stdout_records, args.timeout)
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA FEWS NET operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
