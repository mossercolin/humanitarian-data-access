#!/usr/bin/env python3
"""Bounded anonymous CERF Projects by Year retrieval."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url

# This client reads CERF Projects by Year: the caller's year fills the
# endpoint path for an anonymous GET with no credentials. The 1 MiB
# response-body limit is separate from the first-N record window (max 10)
# and the model-facing stdout limit (default 256 KiB).

SOURCE_ID = "ocha_cerf"
INTERFACE_ID = "cerf_data_api"
ENDPOINT = "https://cerfgms-webapi.unocha.org/v1/project/year/{year}.json"
DOCS_URL = "https://cerfgms-webapi.unocha.org/"
DATA_LIMIT = 1024 * 1024
MAX_STDOUT_RECORDS = 10
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
EMPTY_SCOPE = "No records returned under the current CERF public access scope."


# Read bounded JSON and record observed status, content type, response bytes,
# byte ceiling and final URL. query() expects a top-level array of project
# objects, not a wrapped page or a paginated result structure.
def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": "hda-cerf/1.0"})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, DATA_LIMIT, "CERF projects response", response.headers)
        network = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": DATA_LIMIT, "final_response_url": response.geturl()}
    return remote_json_loads(body), network


def query(year: int, stdout_records: int = MAX_STDOUT_RECORDS, timeout: float = 30.0) -> dict[str, Any]:
    # Four-digit selector syntax, not a claim about available project years.
    if type(year) is not int or not 1000 <= year <= 9999:
        raise ValueError("year must be a four-digit integer between 1000 and 9999")
    if type(stdout_records) is not int or not 1 <= stdout_records <= MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout_records must be an integer between 1 and {MAX_STDOUT_RECORDS}")
    if type(timeout) not in (int, float) or not 0 < timeout < float('inf'):
        raise ValueError("timeout must be positive and finite")
    # The year is the only data selector. The result identifies the precise
    # request and CERF interface, counts all response objects, then exposes
    # only the first N in publisher order; no server-side page is requested.
    # retrieved_at_utc is HDA retrieval time, not a project/allocation date.
    # Empty results do not establish CERF-wide absence or coverage.
    parameters = {"year": str(year)}
    source_url = ENDPOINT.format(year=year)
    payload, network = _request_json(source_url, timeout)
    if not isinstance(payload, list):
        raise ValueError("CERF projects response must be a top-level array")
    if any(not isinstance(record, dict) for record in payload):
        raise ValueError("CERF projects array contains a non-object project")
    records = payload
    shown = min(len(records), stdout_records)
    return {
        "schema_version": "hda-cerf-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "request_scope": {"endpoint": ENDPOINT, "parameters": parameters, "access_scope": "anonymous/public", "requests_requested": 1},
        "lineage": {"source": "CERF", "official_docs": DOCS_URL, "endpoint": ENDPOINT, "source_url": source_url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        "network": {**network, "anonymous": True, "response_record_count": len(records), "pagination": "none established", "next_followed": False},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown, "records": records[:shown], "scope_note": EMPTY_SCOPE if not records else "Records returned under the current CERF public access scope."},
        "semantic_boundary": ["CERF-native field names and values are preserved. Retrieval time is not project/allocation date. Status, amount categories, dates, sectors, countries, and beneficiaries are not reinterpreted.", "This year query does not establish source-global coverage or absence."],
    }


def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    normal = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(normal) <= limit:
        return normal
    # If formatted JSON exceeds stdout, replace it with a complete envelope
    # instead of partial JSON. Keep source/interface, request scope, lineage,
    # network facts, counts and limit diagnostics; omit project records, the
    # normal scope note and semantic boundaries. Fail if this cannot fit.
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
    q = sub.add_parser("query", help="read public Projects by Year")
    q.add_argument("--year", type=int, required=True)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT)
    args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        result = query(args.year, args.stdout_records, args.timeout)
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA CERF operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
