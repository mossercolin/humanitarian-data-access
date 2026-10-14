#!/usr/bin/env python3
"""Bounded anonymous CBPF PF Project Summary v4 retrieval."""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url

SOURCE_ID = "ocha_cbpf"
INTERFACE_ID = "cbpf_api_public"
ENDPOINT = "https://cbpfapi.unocha.org/vo2/odata/GlobalGenericDataExtract"
DOCS_URL = "https://cbpfapi.unocha.org/vo2/"
DATA_LIMIT = 1024 * 1024
MAX_STDOUT_RECORDS = 10
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
EMPTY_SCOPE = "This exact CBPF query returned no records."

# One anonymous public CBPF query selects PF Project Summary v4 for a caller-chosen
# pool fund and allocation-year range. Native JSON records are returned in a
# bounded model-facing window, alongside request scope and source provenance.
# The operation name and its non-caller parameters are intentionally pinned: this
# client describes that one published view, rather than a general CBPF query API.
# Its result is evidence of the response to the recorded fund/year request only;
# it cannot establish complete CBPF, fund, allocation, or project coverage.

# Retrieve one JSON response and retain the observed HTTP details. The 1 MiB
# response-body limit bounds data read from the server, independently of the
# separate byte ceiling applied later to model-facing stdout.
def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": "hda-cbpf/1.0"})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, DATA_LIMIT, "CBPF projects response", response.headers)
        network = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": DATA_LIMIT, "final_response_url": response.geturl()}
    return remote_json_loads(body), network


def query(pool_fund: str, allocation_years: str, stdout_records: int = MAX_STDOUT_RECORDS, timeout: float = 30.0) -> dict[str, Any]:
    if not isinstance(allocation_years, str) or re.fullmatch(r"[0-9]{4}_[0-9]{4}", allocation_years) is None:
        raise ValueError("allocation_years must be exactly YYYY_YYYY")
    start_year, end_year = map(int, allocation_years.split("_"))
    if start_year > end_year:
        raise ValueError("allocation_years start year must not exceed end year")
    if not isinstance(pool_fund, str) or not pool_fund.strip():
        raise ValueError("pool_fund must be a non-empty query-parameter value")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in pool_fund):
        raise ValueError("pool_fund must not contain control characters or unpaired surrogates")
    if type(stdout_records) is not int or not 1 <= stdout_records <= MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout_records must be an integer between 1 and {MAX_STDOUT_RECORDS}")
    if type(timeout) not in (int, float) or not 0 < timeout < float('inf'):
        raise ValueError("timeout must be positive and finite")
    # The caller selects PoolfundCodeAbbrv and AllocationYears; SPCode,
    # ShowAllPooledFunds and FundTypeId are fixed for this operation. URL encoding
    # places all five values into the request query string.
    parameters = {"SPCode": "PF_PROJ_SUMMARY_V4", "PoolfundCodeAbbrv": pool_fund, "ShowAllPooledFunds": "0", "AllocationYears": allocation_years, "FundTypeId": "1"}
    source_url = ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    payload, network = _request_json(source_url, timeout)
    if not isinstance(payload, list):
        raise ValueError("CBPF projects response must be a top-level array")
    if any(not isinstance(record, dict) for record in payload):
        raise ValueError("CBPF projects array contains a non-object project")
    # The required top-level array contains source-native JSON objects; their
    # decoded fields are kept without reinterpretation. Counts describe this
    # response, not global coverage; zero records are scoped to this query.
    # The UTC retrieval timestamp records access, not project/allocation time.
    records = payload
    shown = min(len(records), stdout_records)
    return {
        "schema_version": "hda-cbpf-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "operation": "PF_PROJ_SUMMARY_V4",
        "request_scope": {"operation": "PF_PROJ_SUMMARY_V4", "endpoint": ENDPOINT, "parameters": parameters, "access_scope": "anonymous/public", "requests_requested": 1},
        "lineage": {"source": "CBPF", "official_docs": DOCS_URL, "endpoint": ENDPOINT, "source_url": source_url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        "network": {**network, "anonymous": True, "response_record_count": len(records), "pagination": "none established", "next_followed": False},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown, "records": records[:shown], "scope_note": EMPTY_SCOPE if not records else "Records returned by this exact CBPF query."},
        "semantic_boundary": ["CBPF-native field names and values are preserved. Retrieval time is not project/allocation date. Status, amount categories, dates, sectors, organizations, geography, beneficiaries, and identifiers are not reinterpreted.", "This fund/allocation-year query does not establish source-global coverage or absence."],
    }


# Serialize as UTF-8 JSON before checking the configurable stdout-byte ceiling.
# If normal output is too large, emit only a metadata/provenance envelope and
# omit records; fail if even that smaller envelope exceeds the ceiling.
def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
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
    q = sub.add_parser("query", help="read public PF Project Summary v4")
    q.add_argument("--pool-fund", required=True)
    q.add_argument("--allocation-years", required=True)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT)
    args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        result = query(args.pool_fund, args.allocation_years, args.stdout_records, args.timeout)
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA CBPF operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
