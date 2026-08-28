#!/usr/bin/env python3
"""Discover and query GDACS through its bounded anonymous REST interface."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url


SOURCE_ID = "gdacs"
INTERFACE_ID = "gdacs_rest"
BASE_URL = "https://www.gdacs.org/gdacsapi"
SWAGGER_URL = f"{BASE_URL}/swagger/v1/swagger.json"
DOCS_URL = f"{BASE_URL}/swagger/index.html"
DEFAULT_STDOUT_RECORDS = 5
USER_AGENT = "hda-gdacs/1.0"
API_RESPONSE_LIMIT = 16 * 1024 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, API_RESPONSE_LIMIT, "GDACS API response", response.headers)
        facts = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "final_response_url": response.geturl()}
    try:
        return remote_json_loads(body), facts
    except ValueError as exc:
        raise ValueError(f"server returned invalid JSON ({facts})") from exc


def _swagger(timeout: float) -> dict[str, Any]:
    payload, _ = _request_json(SWAGGER_URL, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        raise ValueError("GDACS Swagger document has no paths object")
    return payload


def _operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for path, item in spec["paths"].items():
        operation = item.get("get") if isinstance(item, dict) else None
        if path.lower().startswith("/api/") and isinstance(operation, dict):
            result[path.removeprefix("/")] = {"path": path, "operation": operation}
    return result


def discover(timeout: float) -> dict[str, Any]:
    spec = _swagger(timeout)
    endpoints = []
    for endpoint, details in sorted(_operations(spec).items()):
        parameters = []
        for item in details["operation"].get("parameters") or []:
            schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
            parameters.append({"name": item.get("name"), "required": bool(item.get("required")), "type": schema.get("type") or item.get("type")})
        endpoints.append({"endpoint": endpoint, "parameters": parameters})
    return {"schema_version": "hda-gdacs-surface/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "base_endpoint": f"{BASE_URL}/api/", "official_docs": DOCS_URL, "swagger_url": SWAGGER_URL, "authentication": {"required": False, "class": "public anonymous"}, "endpoint_count": len(endpoints), "endpoints": endpoints}


def _parse_params(values: Sequence[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value or not value.split("=", 1)[0]:
            raise ValueError(f"parameter must be NAME=VALUE: {value!r}")
        name, item = value.split("=", 1)
        if name in result:
            raise ValueError(f"duplicate parameter: {name}")
        result[name] = item
    return result


def _coerce(value: str, definition: dict[str, Any]) -> Any:
    schema = definition.get("schema") if isinstance(definition.get("schema"), dict) else {}
    kind = schema.get("type") or definition.get("type")
    if kind == "integer": return int(value)
    if kind == "number": return float(value)
    if kind == "boolean":
        if value.lower() not in {"true", "false"}: raise ValueError(f"expected true or false, got {value!r}")
        return value.lower() == "true"
    return value


def _records(payload: Any) -> tuple[list[Any], str]:
    if isinstance(payload, dict) and isinstance(payload.get("features"), list): return payload["features"], "features"
    if isinstance(payload, dict) and isinstance(payload.get("data"), list): return payload["data"], "data"
    if isinstance(payload, list): return payload, "root"
    return [payload], "single_object"


def _write_json(path: pathlib.Path, payload: Any, force: bool = False) -> str:
    if path.exists() and not force: raise FileExistsError(f"output already exists: {path}; use --force to replace it")
    path = path.resolve(); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2); stream.write("\n")
        pathlib.Path(temporary).replace(path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True); raise
    return str(path)


def query(endpoint: str, raw_params: dict[str, str], max_records: int, stdout_records: int,
          output: pathlib.Path | None, timeout: float, force: bool = False) -> dict[str, Any]:
    if output is not None and output.exists() and not force: raise FileExistsError(f"output already exists: {output}; use --force to replace it")
    operations = _operations(_swagger(timeout))
    if endpoint not in operations: raise ValueError(f"unknown endpoint {endpoint!r}; run 'hda_gdacs.py list'")
    definitions = {item.get("name"): item for item in operations[endpoint]["operation"].get("parameters") or [] if item.get("name")}
    unknown = sorted(set(raw_params) - set(definitions))
    if unknown: raise ValueError(f"unsupported parameter(s) for {endpoint}: {unknown}")
    missing = sorted(name for name, item in definitions.items() if item.get("required") and name not in raw_params)
    if missing: raise ValueError(f"missing required parameter(s) for {endpoint}: {missing}")
    params = {name: _coerce(value, definitions[name]) for name, value in raw_params.items()}
    endpoint_url = f"{BASE_URL}/{operations[endpoint]['path'].lstrip('/')}"
    source_url = endpoint_url + ("?" + urllib.parse.urlencode(params) if params else "")
    retrieved_at = _timestamp()
    payload, facts = _request_json(source_url, timeout)
    all_records, envelope = _records(payload)
    selected = all_records[:max_records]
    artifact_path = _write_json(output, payload, force) if output else None
    page_size = next((int(v) for k, v in params.items() if k.lower() == "pagesize"), None)
    page_number = next((int(v) for k, v in params.items() if k.lower() == "pagenumber"), None)
    return {
        "schema_version": "hda-gdacs-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "operation": endpoint,
        "request_scope": {"parameters": params, "max_records": max_records},
        "lineage": {"source": "Global Disaster Alert and Coordination System (GDACS)", "official_docs": DOCS_URL, "swagger_url": SWAGGER_URL, "endpoint_url": endpoint_url, "source_url": source_url, "retrieved_at_utc": retrieved_at},
        "network": {"anonymous": True, **facts, "response_envelope": envelope, "response_record_count": len(all_records), "requested_page_size": page_size, "requested_page_number": page_number, "may_have_more": page_size is not None and len(all_records) >= page_size},
        "result": {"returned_count": len(selected), "returned_to_stdout": min(len(selected), stdout_records), "truncated_for_stdout": len(selected) > stdout_records or len(selected) < len(all_records), "complete_response_artifact": artifact_path, "records": selected[:stdout_records]},
        "diagnostics": ["may_have_more is conservative when a full requested page is returned because this response exposes no total count."],
    }


def _positive(value: str) -> int:
    result = int(value)
    if result < 1: raise argparse.ArgumentTypeError("must be at least 1")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="discover GET endpoints and parameters from live Swagger")
    query_parser = subparsers.add_parser("query", help="query any discovered GET endpoint")
    query_parser.add_argument("endpoint"); query_parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    query_parser.add_argument("--max-records", type=_positive, default=100); query_parser.add_argument("--stdout-records", type=_positive, default=DEFAULT_STDOUT_RECORDS); query_parser.add_argument("--output", type=pathlib.Path); query_parser.add_argument("--force", action="store_true", help="deliberately replace an existing output artifact")
    parser.add_argument("--timeout", type=float, default=30.0); args = parser.parse_args(argv)
    try:
        if args.timeout <= 0: raise ValueError("timeout must be positive")
        result = discover(args.timeout) if args.command == "list" else query(args.endpoint.strip("/"), _parse_params(args.param), args.max_records, args.stdout_records, args.output, args.timeout, args.force)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2); print(); return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA GDACS operation failed: {exc}\n")


if __name__ == "__main__": raise SystemExit(main())
