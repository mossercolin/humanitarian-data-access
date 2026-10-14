#!/usr/bin/env python3
"""One bounded anonymous IFRC GO event JSON page."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url

# This client exposes one anonymous IFRC GO v2 event-listing page.
# Source/interface identities and URLs are local declarations, not API data.
# Four separate limits: 1 MiB HTTP body; local request limit at most 1000
# (not a verified publisher maximum); at most 10 stdout records; and
# serialized stdout defaulting to 256 KiB, configurable upward only.
SOURCE_ID = "ifrc_go"
INTERFACE_ID = "ifrc_go_public"
ENDPOINT = "https://goadmin.ifrc.org/api/v2/event/"
DOCS_URL = "https://goadmin.ifrc.org/api/"
DATA_LIMIT = 1024 * 1024
MAX_PAGE_SIZE = 1000
MAX_STDOUT_RECORDS = 10
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
EMPTY_SCOPE = "No records returned under the current IFRC GO public access scope."


def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    # Issue an anonymous JSON GET; bound the response body before parsing.
    # Network observations describe this response, including bytes and final URL.
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": "hda-ifrc-go/1.0"})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, DATA_LIMIT, "IFRC GO event response", response.headers)
        network = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": DATA_LIMIT, "final_response_url": response.geturl()}
    return remote_json_loads(body), network


def query(limit: int = 50, offset: int = 0, stdout_records: int = MAX_STDOUT_RECORDS, timeout: float = 30.0) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be an integer between 1 and {MAX_PAGE_SIZE}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(stdout_records) is not int or not 1 <= stdout_records <= MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout_records must be an integer between 1 and {MAX_STDOUT_RECORDS}")
    if not 0 < timeout < float('inf'):
        raise ValueError("timeout must be positive and finite")
    # limit/offset go to IFRC GO; stdout_records controls only local display.
    # The publisher returns one results page plus count/next/previous metadata.
    parameters = {"limit": str(limit), "offset": str(offset)}
    source_url = ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    payload, network = _request_json(source_url, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("IFRC GO response lacks the results record array")
    if (type(payload.get("count")) is not int or payload["count"] < 0
            or any(key not in payload or (payload[key] is not None and not isinstance(payload[key], str))
                   for key in ("next", "previous"))):
        raise ValueError("IFRC GO response has malformed publisher pagination")
    records = payload["results"]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("IFRC GO results array contains a non-object record")
    # Never follow next; retain the first selected records with native values.
    shown = min(len(records), stdout_records, limit)
    # Report request scope separately from response/pagination facts and lineage.
    # Retrieval time is not event, publication, or collection time. An empty
    # page establishes only absence within this anonymous single-page request.
    return {
        "schema_version": "hda-ifrc-go-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "request_scope": {"endpoint": ENDPOINT, "parameters": parameters, "access_scope": "anonymous/public", "pages_requested": 1},
        "lineage": {"source": "IFRC GO", "official_docs": DOCS_URL, "endpoint": ENDPOINT, "source_url": source_url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        "network": {**network, "anonymous": True, "response_record_count": len(records), "publisher_pagination": {key: payload[key] for key in ("count", "next", "previous") if key in payload}, "next_followed": False},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown, "records": records[:shown], "scope_note": EMPTY_SCOPE if not records else "Records returned under the current IFRC GO public access scope."},
        "semantic_boundary": ["IFRC-native field names and values are preserved. Retrieval time is not publication, period, or collection time.", "Anonymous access may omit permissioned data; this page does not establish source-global coverage or absence."],
    }


def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    # Serialize selected output as UTF-8 JSON under the configured byte ceiling.
    # On overflow, retain scope, lineage, network facts and response count,
    # but omit records, result metadata and semantic-boundary notes. If that
    # smaller envelope also exceeds the ceiling, fail rather than truncate.
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
    q = sub.add_parser("query", help="read one public event page")
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--offset", type=int, default=0)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT)
    args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        result = query(args.limit, args.offset, args.stdout_records, args.timeout)
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA IFRC GO operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
