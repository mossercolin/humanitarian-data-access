#!/usr/bin/env python3
"""Bounded anonymous INFORM country/model scores retrieval."""

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

# This client exposes one fixed public INFORM Countries/Scores operation.
# Workflow, ISO3, and indicator are passed as the operation's native selectors;
# decoded score rows are preserved rather than normalized into risk conclusions.
# A successful or empty response is limited to this supported endpoint and
# selector combination, not a statement about all INFORM models, indicators,
# countries, or the meaning and comparability of their scores.
SOURCE_ID = "inform"
INTERFACE_ID = "inform_web_api"
ENDPOINT = "https://drmkc.jrc.ec.europa.eu/inform-index/API/InformAPI/Countries/Scores/"
DOCS_URL = "https://drmkc.jrc.ec.europa.eu/inform-index/API/InformAPI/"
# Bound the network response independently of the record and byte limits on stdout.
DATA_LIMIT = 1024 * 1024
MAX_STDOUT_RECORDS = 10
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
EMPTY_SCOPE = "This exact INFORM query returned no records."


def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    # Make one anonymous JSON request; bound the body before JSON decoding.
    # Capture observed HTTP facts separately from the publisher records.
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": "hda-inform/1.0"})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, DATA_LIMIT, "INFORM scores response", response.headers)
        network = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": DATA_LIMIT, "final_response_url": response.geturl()}
    return remote_json_loads(body), network


def query(workflow_id: int, iso3: str, indicator_id: str, stdout_records: int = MAX_STDOUT_RECORDS, timeout: float = 30.0) -> dict[str, Any]:
    # Callers supply INFORM-native selectors; these checks validate type and
    # syntax/range, not whether the selected identifiers exist at INFORM.
    if type(workflow_id) is not int or not 1 <= workflow_id <= 2147483647:
        raise ValueError("workflow_id must be a positive native Int32 integer")
    if not isinstance(iso3, str) or re.fullmatch(r"[A-Z]{3}", iso3) is None:
        raise ValueError("iso3 must be exactly three uppercase ASCII letters")
    if not isinstance(indicator_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", indicator_id) is None:
        raise ValueError("indicator_id must be one explicit native identifier (letters, digits, underscore, dot or hyphen)")
    if type(stdout_records) is not int or not 1 <= stdout_records <= MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout_records must be an integer between 1 and {MAX_STDOUT_RECORDS}")
    if type(timeout) not in (int, float) or not 0 < timeout < float('inf'):
        raise ValueError("timeout must be positive and finite")
    # Encode the explicit selectors under the publisher's parameter names.
    parameters = {"WorkflowId": workflow_id, "Iso3": iso3, "IndicatorId": indicator_id}
    source_url = ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    payload, network = _request_json(source_url, timeout)
    # Expect a top-level array of publisher records, each a JSON object.
    if not isinstance(payload, list):
        raise ValueError("INFORM scores response must be a top-level array")
    if any(not isinstance(record, dict) for record in payload):
        raise ValueError("INFORM scores array contains a non-object record")
    records = payload
    # Select a stdout record window after receiving the bounded response.
    shown = min(len(records), stdout_records)
    # Keep request scope, observed network facts and source/retrieval lineage
    # distinct. WorkflowId selects a workflow, not an API version; the
    # retrieval time is not a data period. Publisher values keep their
    # native meaning; empty results concern only these selectors.
    return {
        "schema_version": "hda-inform-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "operation": "Countries/Scores",
        "request_scope": {"operation": "Countries/Scores", "endpoint": ENDPOINT, "parameters": parameters, "access_scope": "anonymous/public", "requests_requested": 1},
        "lineage": {"source": "INFORM", "official_docs": DOCS_URL, "endpoint": ENDPOINT, "source_url": source_url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        "network": {**network, "anonymous": True, "response_record_count": len(records), "pagination": "none established", "next_followed": False},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown, "records": records[:shown], "scope_note": EMPTY_SCOPE if not records else "Records returned by this exact INFORM query."},
        "semantic_boundary": ["INFORM-native fields and values are preserved without calculation, ranking, normalization or interpretation. Scores, indicator meaning, model/release identity, geography and reference periods remain publisher-defined. Retrieval time is not a reference period; WorkflowId is not an API version.", "This exact query does not establish coverage of other workflows, indicators, countries, Severity, Climate Change or Warning. Empty results establish only that this query returned no records."],
    }


def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    # Emit the normal JSON result when its serialized bytes fit the limit.
    # Otherwise retain identity, scope, lineage, network and record counts,
    # but omit publisher records and the normal result/semantic_boundary.
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
    q = sub.add_parser("query", help="read public native country/model scores")
    q.add_argument("--workflow-id", type=_positive, required=True)
    q.add_argument("--iso3", required=True)
    q.add_argument("--indicator-id", required=True)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT)
    args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        result = query(args.workflow_id, args.iso3, args.indicator_id, args.stdout_records, args.timeout)
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA INFORM operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
