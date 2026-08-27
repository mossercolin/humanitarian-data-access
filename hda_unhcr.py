#!/usr/bin/env python3
"""Discover and query UNHCR Refugee Statistics through a bounded anonymous client."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence


SOURCE_ID = "unhcr_refugee_statistics"
INTERFACE_ID = "unhcr_refugee_stats_v1"
BASE_URL = "https://api.unhcr.org/population/v1"
DOCS_URL = "https://api.unhcr.org/docs/refugee-statistics.html"
OPERATIONS = (
    "asylum-applications", "asylum-decisions", "countries", "demographics",
    "footnotes", "idmc", "nowcasting", "population", "regions", "solutions",
    "unrwa", "years",
)
DEFAULT_MAX_RECORDS = 100
DEFAULT_STDOUT_RECORDS = 5
MAX_RECORDS = 10_000
PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\[\]]*$")
USER_AGENT = "hda-unhcr/1.0"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        facts = {
            "http_status": response.status,
            "content_type": response.headers.get("Content-Type"),
            "response_bytes": len(body),
        }
    try:
        return json.loads(body), facts
    except json.JSONDecodeError as exc:
        raise ValueError(f"server returned invalid JSON ({facts})") from exc


def discover() -> dict[str, Any]:
    return {
        "schema_version": "hda-unhcr-surface/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "base_endpoint": BASE_URL,
        "official_docs": DOCS_URL,
        "authentication": {"required": False, "class": "public anonymous"},
        "pagination": {"parameters": ["limit", "page"], "client_controls": ["limit", "page", "download"]},
        "operations": list(OPERATIONS),
        "diagnostics": ["Operation list is the bounded catalog published in the official v1 documentation."],
    }


def _parse_params(values: Sequence[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen = set()
    for value in values:
        if "=" not in value:
            raise ValueError(f"parameter must be NAME=VALUE: {value!r}")
        name, item = value.split("=", 1)
        if not PARAMETER_NAME.fullmatch(name):
            raise ValueError(f"invalid parameter name: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate parameter: {name}")
        if name in {"limit", "page", "download"}:
            raise ValueError(f"client controls parameter: {name}")
        seen.add(name)
        result.append((name, item))
    return result


def _records(payload: Any) -> list[Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("UNHCR response has no items list")
    return payload["items"]


def _write_json(path: pathlib.Path, payload: Any) -> str:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        pathlib.Path(temporary).replace(path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise
    return str(path)


def query(operation: str, parameters: list[tuple[str, str]], start_page: int, page_size: int,
          max_records: int, stdout_records: int, output: pathlib.Path | None,
          timeout: float) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}; run 'hda_unhcr.py list'")
    retrieved_at = _timestamp()
    records: list[Any] = []
    page = start_page
    pages_retrieved = 0
    network: list[dict[str, Any]] = []
    max_pages: int | None = None
    endpoint_url = f"{BASE_URL}/{operation}/"
    while len(records) < max_records:
        requested = min(page_size, max_records - len(records))
        query_params = [*parameters, ("limit", str(requested)), ("page", str(page))]
        url = endpoint_url + "?" + urllib.parse.urlencode(query_params)
        payload, facts = _request_json(url, timeout)
        batch = _records(payload)
        records.extend(batch[:requested])
        pages_retrieved += 1
        network.append({**facts, "page": page, "requested_limit": requested, "returned_items": len(batch)})
        raw_max_pages = payload.get("maxPages")
        max_pages = raw_max_pages if isinstance(raw_max_pages, int) else max_pages
        if len(batch) < requested or (max_pages is not None and page >= max_pages):
            break
        page += 1
    complete = {
        "schema_version": "hda-unhcr-result/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "operation": operation,
        "request_scope": {"parameters": dict(parameters), "start_page": start_page, "page_size": page_size, "max_records": max_records},
        "lineage": {"source": "UNHCR Refugee Statistics", "official_docs": DOCS_URL, "endpoint_url": endpoint_url, "retrieved_at_utc": retrieved_at},
        "network": {"anonymous": True, "requests": network, "pages_retrieved": pages_retrieved, "records_retrieved": len(records), "reported_max_pages": max_pages},
        "records": records,
    }
    artifact_path = _write_json(output, complete) if output else None
    return {
        **{key: complete[key] for key in ("schema_version", "source_id", "interface_id", "operation", "request_scope", "lineage", "network")},
        "result": {"returned_count": len(records), "returned_to_stdout": min(len(records), stdout_records), "truncated_for_stdout": len(records) > stdout_records, "complete_result_artifact": artifact_path, "records": records[:stdout_records]},
        "diagnostics": ["Parameter values are passed through to the selected documented operation; limit, page, and CSV download remain client-controlled."],
    }


def _positive(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list documented v1 operations")
    query_parser = subparsers.add_parser("query", help="query a documented data operation")
    query_parser.add_argument("operation")
    query_parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    query_parser.add_argument("--start-page", type=_positive, default=1)
    query_parser.add_argument("--page-size", type=_positive, default=100)
    query_parser.add_argument("--max-records", type=_positive, default=DEFAULT_MAX_RECORDS)
    query_parser.add_argument("--stdout-records", type=_positive, default=DEFAULT_STDOUT_RECORDS)
    query_parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        if args.command == "list":
            result = discover()
        else:
            if args.max_records > MAX_RECORDS:
                raise ValueError(f"max records cannot exceed {MAX_RECORDS}")
            result = query(args.operation.strip("/"), _parse_params(args.param), args.start_page, args.page_size, args.max_records, args.stdout_records, args.output, args.timeout)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        print()
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA UNHCR operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
