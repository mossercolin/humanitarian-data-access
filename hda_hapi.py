#!/usr/bin/env python3
"""Discover and query HDX HAPI through one bounded, endpoint-agnostic interface."""

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


OPENAPI_URL = "https://hapi.humdata.org/openapi.json"
BASE_URL = "https://hapi.humdata.org/api/v2"
MAX_PAGE_SIZE = 10_000
MAX_RECORDS = 100_000
DEFAULT_STDOUT_RECORDS = 5
MAX_LINEAGE_RESOURCES = 20
# The live HAPI edge returned HTTP 202 with an empty body for custom and
# Some intermediary paths reject Python's default user agent even though the
# documented curl-style access works.
USER_AGENT = "curl/8.7.1"
API_RESPONSE_LIMIT = 16 * 1024 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, API_RESPONSE_LIMIT, "HDX HAPI API response", response.headers)
        try:
            payload = remote_json_loads(body)
        except ValueError as exc:
            raise ValueError(
                f"server returned invalid JSON (HTTP {response.status}, content-type {response.headers.get('Content-Type')!r}, bytes {len(body)})"
            ) from exc
    if not isinstance(payload, dict):
        raise ValueError("server returned a non-object JSON response")
    return payload


def _openapi(timeout: float) -> dict[str, Any]:
    spec = _request_json(OPENAPI_URL, timeout)
    if not isinstance(spec.get("paths"), dict):
        raise ValueError("HDX HAPI OpenAPI document has no paths object")
    return spec


def _resolve_parameter(spec: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    reference = item.get("$ref")
    if not reference:
        return item
    return spec["components"]["parameters"][reference.rsplit("/", 1)[-1]]


def _operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for path, path_item in spec["paths"].items():
        if not path.startswith("/api/v2/") or path.endswith("encode_app_identifier"):
            continue
        operation = path_item.get("get")
        if not isinstance(operation, dict):
            continue
        endpoint = path.removeprefix("/api/v2/")
        parameters = [_resolve_parameter(spec, item) for item in operation.get("parameters", [])]
        result[endpoint] = {"path": path, "operation": operation, "parameters": parameters}
    return result


def discover(timeout: float) -> dict[str, Any]:
    spec = _openapi(timeout)
    endpoints = []
    for endpoint, details in sorted(_operations(spec).items()):
        parameters = []
        for item in details["parameters"]:
            name = item.get("name")
            if name == "app_identifier":
                continue
            schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
            parameters.append({
                "name": name,
                "required": bool(item.get("required", False)),
                "type": schema.get("type"),
                "allowed_values": schema.get("enum"),
            })
        endpoints.append({"endpoint": endpoint, "parameters": parameters})
    return {
        "schema_version": "hda-hapi-surface/v1",
        "source": "HDX HAPI",
        "base_endpoint": BASE_URL,
        "openapi_url": OPENAPI_URL,
        "api_version": spec.get("info", {}).get("version"),
        "discovered_at_utc": _timestamp(),
        "authentication": {
            "required": True,
            "accepted_configuration": ["HDA_HAPI_APP_IDENTIFIER", "HMCP_HDX_APP_ID", "existing OpenClaw humanitarian-hapi configuration"],
            "secret": False,
        },
        "pagination": {"parameters": ["limit", "offset"], "maximum_page_size": MAX_PAGE_SIZE},
        "response_formats": ["json", "csv"],
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
    }


def _configured_identifier() -> str | None:
    for name in ("HDA_HAPI_APP_IDENTIFIER", "HMCP_HDX_APP_ID"):
        if os.environ.get(name):
            return os.environ[name]
    config = pathlib.Path.home() / ".openclaw" / "openclaw.json"
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
        value = payload["mcp"]["servers"]["humanitarian-hapi"]["env"]["HMCP_HDX_APP_ID"]
        return value if isinstance(value, str) and value else None
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _coerce(value: str, schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "integer":
        return int(value)
    if kind == "number":
        return float(value)
    if kind == "boolean":
        if value.lower() not in {"true", "false"}:
            raise ValueError(f"expected true or false, got {value!r}")
        return value.lower() == "true"
    return value


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


def _write_json(path: pathlib.Path, payload: dict[str, Any], force: bool = False) -> str:
    path = path.resolve()
    if path.exists() and not force:
        raise FileExistsError(f"output already exists: {path}; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return str(path)


def query(
    endpoint: str,
    raw_params: dict[str, str],
    page_size: int,
    max_records: int,
    stdout_records: int,
    output: pathlib.Path | None,
    timeout: float,
    force: bool = False,
) -> dict[str, Any]:
    if output is not None and output.exists() and not force:
        raise FileExistsError(f"output already exists: {output}; use --force to replace it")
    spec = _openapi(timeout)
    operations = _operations(spec)
    if endpoint not in operations:
        raise ValueError(f"unknown endpoint {endpoint!r}; run 'hda_hapi.py list'")
    definitions = {item.get("name"): item for item in operations[endpoint]["parameters"]}
    forbidden = {"app_identifier", "output_format", "limit", "offset"}
    unknown = sorted(set(raw_params) - set(definitions))
    if unknown:
        raise ValueError(f"unsupported parameter(s) for {endpoint}: {unknown}")
    supplied_forbidden = sorted(set(raw_params) & forbidden)
    if supplied_forbidden:
        raise ValueError(f"client controls parameter(s): {supplied_forbidden}")
    params = {}
    for name, value in raw_params.items():
        schema = definitions[name].get("schema") if isinstance(definitions[name].get("schema"), dict) else {}
        params[name] = _coerce(value, schema)
    identifier = _configured_identifier()
    if not identifier:
        raise ValueError("HDX HAPI app identifier unavailable; set HDA_HAPI_APP_IDENTIFIER or HMCP_HDX_APP_ID")

    retrieved_at = _timestamp()
    records: list[dict[str, Any]] = []
    offset = 0
    page_count = 0
    last_page_size = 0
    while len(records) < max_records:
        requested = min(page_size, max_records - len(records))
        request_params = {**params, "output_format": "json", "limit": requested, "offset": offset}
        authenticated = {**request_params, "app_identifier": identifier}
        url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(authenticated)}"
        payload = _request_json(url, timeout)
        page = payload.get("data")
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise ValueError("HDX HAPI response has no object-list data envelope")
        records.extend(page)
        page_count += 1
        last_page_size = len(page)
        offset += len(page)
        if len(page) < requested:
            break

    may_have_more = last_page_size == min(page_size, max_records - (len(records) - last_page_size))
    resource_ids = sorted({str(item["resource_hdx_id"]) for item in records if item.get("resource_hdx_id")})
    resource_metadata = []
    if endpoint != "metadata/resource":
        for resource_id in resource_ids[:MAX_LINEAGE_RESOURCES]:
            metadata_params = urllib.parse.urlencode({
                "resource_hdx_id": resource_id,
                "output_format": "json",
                "limit": 1,
                "offset": 0,
                "app_identifier": identifier,
            })
            metadata_payload = _request_json(f"{BASE_URL}/metadata/resource?{metadata_params}", timeout)
            metadata_rows = metadata_payload.get("data")
            if isinstance(metadata_rows, list) and metadata_rows and isinstance(metadata_rows[0], dict):
                resource_metadata.append(metadata_rows[0])
    lineage = {
        "source": "HDX HAPI",
        "base_endpoint": BASE_URL,
        "endpoint": endpoint,
        "request_parameters": params,
        "retrieved_at_utc": retrieved_at,
        "returned_count": len(records),
        "resource_hdx_ids": resource_ids,
        "resource_metadata": resource_metadata,
        "resource_metadata_lookup_limit": MAX_LINEAGE_RESOURCES,
        "resource_metadata_truncated": len(resource_ids) > MAX_LINEAGE_RESOURCES,
        "resource_metadata_endpoint": f"{BASE_URL}/metadata/resource",
    }
    complete = {
        "schema_version": "hda-hapi-result/v1",
        "request_scope": {"endpoint": endpoint, "parameters": params, "page_size": page_size, "max_records": max_records},
        "lineage": lineage,
        "pagination": {
            "pages_retrieved": page_count,
            "records_retrieved": len(records),
            "last_page_size": last_page_size,
            "stopped_at_max_records": len(records) >= max_records,
            "may_have_more": may_have_more,
            "next_offset": offset if may_have_more else None,
            "total_available": None,
        },
        "records": records,
    }
    artifact_path = _write_json(output, complete, force) if output else None
    stdout_truncated = len(records) > stdout_records
    return {
        **{key: complete[key] for key in ("schema_version", "request_scope", "lineage", "pagination")},
        "result": {
            "returned_to_stdout": min(len(records), stdout_records),
            "truncated_for_stdout": stdout_truncated,
            "complete_result_artifact": artifact_path,
            "records": records[:stdout_records],
        },
    }


def _positive(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="discover endpoints and accepted parameters from live OpenAPI")
    query_parser = subparsers.add_parser("query", help="query any discovered endpoint")
    query_parser.add_argument("endpoint")
    query_parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    query_parser.add_argument("--page-size", type=_positive, default=1000)
    query_parser.add_argument("--max-records", type=_positive, default=1000)
    query_parser.add_argument("--stdout-records", type=_positive, default=DEFAULT_STDOUT_RECORDS)
    query_parser.add_argument("--output", type=pathlib.Path)
    query_parser.add_argument("--force", action="store_true", help="deliberately replace an existing output artifact")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        if args.command == "list":
            result = discover(args.timeout)
        else:
            if args.page_size > MAX_PAGE_SIZE:
                raise ValueError(f"page size cannot exceed {MAX_PAGE_SIZE}")
            if args.max_records > MAX_RECORDS:
                raise ValueError(f"max records cannot exceed {MAX_RECORDS}")
            result = query(args.endpoint.strip("/"), _parse_params(args.param), args.page_size, args.max_records, args.stdout_records, args.output, args.timeout, args.force)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        print()
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA HAPI operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
