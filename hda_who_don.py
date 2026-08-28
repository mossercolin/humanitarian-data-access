#!/usr/bin/env python3
"""Bounded anonymous access to WHO Disease Outbreak News through public OData."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from hda_http import read_limited, require_http_url


SOURCE_ID = "who_disease_outbreak_news"
INTERFACE_ID = "who_don_odata_public"
ENDPOINT = "https://www.who.int/api/hubs/diseaseoutbreaknews"
DATA_LIMIT = 2 * 1024 * 1024
MAX_TOP = 100
MAX_SKIP = 10000
MAX_STDOUT_RECORDS = 10
USER_AGENT = "hda-who-don/1.0"
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(parameters: Mapping[str, str], timeout: float) -> tuple[dict[str, Any], dict[str, Any], str]:
    source_url = ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    request = urllib.request.Request(
        require_http_url(source_url),
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = read_limited(response, DATA_LIMIT, "WHO DON OData response", response.headers)
        network = {
            "http_status": response.status,
            "content_type": response.headers.get("Content-Type"),
            "response_bytes": len(body),
            "response_limit_bytes": DATA_LIMIT,
        }
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"WHO DON OData response was invalid JSON ({network})") from exc
    if not isinstance(payload, dict):
        raise ValueError("WHO DON OData response was not a JSON object")
    return payload, network, source_url


def query(
    select: Sequence[str],
    top: int,
    skip: int,
    stdout_records: int,
    timeout: float,
    filter_expression: str | None = None,
    order_expression: str | None = None,
) -> dict[str, Any]:
    selected = list(select)
    if not selected or any(not _FIELD.fullmatch(field) for field in selected):
        raise ValueError("$select must contain comma-separated native field names")
    if len(set(selected)) != len(selected):
        raise ValueError("$select field names must not be duplicated")
    if top < 1 or top > MAX_TOP:
        raise ValueError(f"$top must be between 1 and {MAX_TOP}")
    if skip < 0 or skip > MAX_SKIP:
        raise ValueError(f"$skip must be between 0 and {MAX_SKIP}")
    if stdout_records < 1 or stdout_records > MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout records must be between 1 and {MAX_STDOUT_RECORDS}")
    if filter_expression is not None and not filter_expression.strip():
        raise ValueError("$filter must be non-empty when supplied")
    if order_expression is not None and not order_expression.strip():
        raise ValueError("$orderby must be non-empty when supplied")

    parameters = {"$select": ",".join(selected)}
    if filter_expression is not None:
        parameters["$filter"] = filter_expression
    if order_expression is not None:
        parameters["$orderby"] = order_expression
    parameters.update({"$top": str(top), "$skip": str(skip), "$count": "true"})
    payload, data_network, source_url = _request_json(parameters, timeout)
    records = payload.get("value")
    if not isinstance(records, list):
        raise ValueError("WHO DON OData response lacks the native value record array")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("WHO DON OData value array contains a non-object record")
    shown = min(len(records), stdout_records)
    return {
        "schema_version": "hda-who-don-result/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "request_scope": {"endpoint": ENDPOINT, "odata_query": parameters},
        "lineage": {
            "source": "WHO Disease Outbreak News",
            "endpoint": ENDPOINT,
            "source_url": source_url,
            "retrieved_at_utc": _timestamp(),
        },
        "network": {
            "anonymous": True,
            "data": data_network,
            "response_record_count": len(records),
            "odata_count": payload.get("@odata.count"),
            "odata_context": payload.get("@odata.context"),
            "odata_next_link": payload.get("@odata.nextLink"),
        },
        "result": {
            "returned_to_stdout": shown,
            "truncated_for_stdout": len(records) > shown,
            "records": records[:shown],
        },
        "semantic_boundary": [
            "WHO-native fields, including narrative HTML and regionscountries arrays, are preserved unchanged.",
            "HDA does not extract or infer cases, deaths, CFR, dates, or locations from narrative text.",
        ],
    }


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select", required=True, help="comma-separated native fields")
    parser.add_argument("--filter", dest="filter_expression", help="publisher-native OData filter")
    parser.add_argument("--orderby", dest="order_expression", help="publisher-native OData orderby expression")
    parser.add_argument("--top", required=True, type=_positive)
    parser.add_argument("--skip", type=_nonnegative, default=0)
    parser.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        result = query(
            args.select.split(","), args.top, args.skip, args.stdout_records, args.timeout,
            args.filter_expression, args.order_expression,
        )
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        print()
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA WHO DON operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
