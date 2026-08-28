#!/usr/bin/env python3
"""Bounded anonymous access to WHO FLUMART VIW_FNT through public xMart OData."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import read_limited, require_http_url


SOURCE_ID = "who_whdh_xmart"
INTERFACE_ID = "who_whdh_xmart_public"
BASE_URL = "https://xmart-api-public.who.int"
MART = "FLUMART"
OBJECT = "VIW_FNT"
METADATA_URL = f"{BASE_URL}/{MART}/$metadata"
OBJECT_URL = f"{BASE_URL}/{MART}/{OBJECT}"
DOCS_URL = "https://extranet.who.int/xmart4/docs/xmart_api/use_API.html"
METADATA_LIMIT = 1024 * 1024
DATA_LIMIT = 1024 * 1024
MAX_TOP = 100
MAX_STDOUT_RECORDS = 10
USER_AGENT = "hda-who-xmart/1.0"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request(url: str, accept: str, limit: int, operation: str, timeout: float) -> tuple[bytes, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": accept, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = read_limited(response, limit, operation, response.headers)
        return body, {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": limit}


def _metadata(timeout: float) -> tuple[ET.Element, dict[str, Any]]:
    body, network = _request(METADATA_URL, "application/xml", METADATA_LIMIT, "WHO xMart metadata response", timeout)
    try:
        return ET.fromstring(body), network
    except ET.ParseError as exc:
        raise ValueError(f"WHO xMart metadata response was invalid XML ({network})") from exc


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _object_schema(root: ET.Element) -> dict[str, Any]:
    entity = next((item for item in root.iter() if _local(item) == "EntityType" and item.get("Name") == OBJECT), None)
    entity_set = next((item for item in root.iter() if _local(item) == "EntitySet" and item.get("Name") == OBJECT), None)
    if entity is None or entity_set is None:
        raise ValueError(f"live {MART} metadata lacks the {OBJECT} entity type or entity set")
    properties = [{"name": item.get("Name"), "type": item.get("Type")} for item in entity if _local(item) == "Property"]
    if not properties:
        raise ValueError(f"live {MART} metadata exposes no properties for {OBJECT}")
    return {"mart": MART, "object": OBJECT, "entity_type": entity_set.get("EntityType"), "properties": properties}


def discover(timeout: float) -> dict[str, Any]:
    root, network = _metadata(timeout)
    return {"schema_version": "hda-who-xmart-discovery/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "source": "WHO World Health Data Hub / xMart", "production_root": BASE_URL, "authentication": {"required": False, "class": "public anonymous"}, "schema": _object_schema(root), "lineage": {"official_docs": DOCS_URL, "metadata_url": METADATA_URL, "retrieved_at_utc": _timestamp()}, "network": network}


def query(select: Sequence[str], filter_expression: str, top: int, skip: int, stdout_records: int, timeout: float) -> dict[str, Any]:
    root, metadata_network = _metadata(timeout)
    schema = _object_schema(root)
    available = {item["name"] for item in schema["properties"]}
    selected = list(select)
    if not selected or any(not name or name not in available for name in selected):
        raise ValueError(f"$select fields must be present in live {MART}/{OBJECT} metadata")
    if not filter_expression.strip():
        raise ValueError("$filter must be non-empty")
    if top < 1 or top > MAX_TOP:
        raise ValueError(f"$top must be between 1 and {MAX_TOP}")
    if skip < 0:
        raise ValueError("$skip must be non-negative")
    parameters = {"$select": ",".join(selected), "$filter": filter_expression, "$top": str(top)}
    if skip:
        parameters["$skip"] = str(skip)
    source_url = OBJECT_URL + "?" + urllib.parse.urlencode(parameters)
    body, data_network = _request(source_url, "application/json", DATA_LIMIT, "WHO xMart data response", timeout)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WHO xMart data response was invalid JSON ({data_network})") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise ValueError("WHO xMart response lacks the OData value record array")
    records = payload["value"]
    if not records:
        raise ValueError("WHO xMart response contained no records for the exact query scope")
    shown = min(len(records), stdout_records)
    return {
        "schema_version": "hda-who-xmart-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "request_scope": {"production_root": BASE_URL, "mart": MART, "object": OBJECT, "odata_query": parameters},
        "lineage": {"source": "WHO World Health Data Hub / xMart", "official_docs": DOCS_URL, "metadata_url": METADATA_URL, "object_url": OBJECT_URL, "source_url": source_url, "retrieved_at_utc": _timestamp()},
        "network": {"anonymous": True, "metadata": metadata_network, "data": data_network, "response_record_count": len(records), "odata_context": payload.get("@odata.context")},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown, "records": records[:shown]},
        "semantic_boundary": ["WHO-native field names and values are preserved. HDA does not infer date meaning, denominators, currentness, geography equivalence, or indicator comparability."],
    }


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1: raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0: raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help=f"inspect live {MART} metadata for {OBJECT}")
    q = sub.add_parser("query", help=f"run one bounded {MART}/{OBJECT} OData query")
    q.add_argument("--select", required=True, help="comma-separated native properties")
    q.add_argument("--filter", required=True, dest="filter_expression", help="publisher-native OData filter")
    q.add_argument("--top", required=True, type=_positive)
    q.add_argument("--skip", type=_nonnegative, default=0)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0: raise ValueError("timeout must be positive")
        if args.command == "discover": result = discover(args.timeout)
        else:
            if args.stdout_records > MAX_STDOUT_RECORDS: raise ValueError(f"--stdout-records cannot exceed {MAX_STDOUT_RECORDS}")
            result = query(args.select.split(","), args.filter_expression, args.top, args.skip, args.stdout_records, args.timeout)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2); print(); return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA WHO xMart operation failed: {exc}\n")


if __name__ == "__main__": raise SystemExit(main())
