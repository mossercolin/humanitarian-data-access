#!/usr/bin/env python3
"""Discover and query HDX HAPI through one bounded, endpoint-agnostic interface."""

# HAPI-specific flow: fetch live OpenAPI to find v2 GET endpoints instead of
# maintaining a catalog for individual humanitarian topics. The v2 URL, JSON
# "data" envelope, and resource metadata convention remain HDA-specific.
# "list" reports live endpoint/operation-parameter facts alongside some local
# declarations; its parameter list is not a list of caller-settable options.
# "query" checks parameter names, makes limited scalar conversions, obtains a
# non-secret app identifier, and fetches JSON pages up to a record ceiling.
# It then attaches bounded resource metadata as retrieval lineage, optionally
# saves all retrieved records, and emits a smaller, byte-bounded stdout view.
# Neither a retrieved result nor lineage proves source-wide completeness,
# humanitarian meaning, validity, or cross-source comparability.

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


SOURCE_ID = "hdx_hapi"
INTERFACE_ID = "hdx_hapi_v2"
OPENAPI_URL = "https://hapi.humdata.org/openapi.json"
BASE_URL = "https://hapi.humdata.org/api/v2"
MAX_PAGE_SIZE = 10_000
MAX_RECORDS = 100_000
DEFAULT_STDOUT_RECORDS = 5
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
MAX_LINEAGE_RESOURCES = 20
# Keep this curl-style User-Agent as an existing HAPI compatibility choice.
USER_AGENT = "curl/8.7.1"
API_RESPONSE_LIMIT = 16 * 1024 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# The 16 MiB limit applies independently to each HTTP response (OpenAPI, data
# page, or metadata lookup); it does not cap total bytes across a query.
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


# Only operation-level parameter references into components/parameters are
# resolved; path-level parameters and schema-level references are not expanded.
def _resolve_parameter(spec: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    reference = item.get("$ref")
    if not reference:
        return item
    return spec["components"]["parameters"][reference.rsplit("/", 1)[-1]]


# Select HAPI v2 GET operations, not every OpenAPI path or HTTP method. The
# app-identifier encoding utility is deliberately outside this data surface.
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


# Discovery gets endpoints, operation parameters, types/enums, and API version
# from live OpenAPI. Authentication metadata, response formats, and page-size
# limit below are HDA declarations, not extracted live schema capabilities.
# Only app_identifier is hidden: output_format, limit, and offset may still be
# listed even though query() reserves them. "required" and "allowed_values"
# describe source schema metadata; query() does not enforce them all.
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
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
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


# The first nonempty identifier wins: HDA_HAPI_APP_IDENTIFIER, then
# HMCP_HDX_APP_ID, then the humanitarian-hapi MCP server entry in
# ~/.openclaw/openclaw.json. That last path is retained as host compatibility;
# an absent, unreadable, or malformed configuration silently yields no value.
# list/discover needs no identifier; query does. This is client identification,
# not a secret API key.
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


# Caller values are strings. Convert only inline integer/number/boolean types;
# booleans accept true/false (case-insensitive) only. This is not full OpenAPI
# validation: required fields, enums, ranges, formats, arrays, and referenced
# schemas are not comprehensively enforced here.
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
    # Every query re-reads the live operation definitions before checking
    # caller-supplied names. Reserved names remain under HDA's control.
    spec = _openapi(timeout)
    operations = _operations(spec)
    if endpoint not in operations:
        raise ValueError(f"unknown endpoint {endpoint!r}; run 'hda_hapi.py list'")
    definitions = {item.get("name"): item for item in operations[endpoint]["parameters"]}
    forbidden = {"app_identifier", "output_format", "limit", "offset"}
    supplied_forbidden = sorted(set(raw_params) & forbidden)
    if supplied_forbidden:
        raise ValueError(f"client controls parameter(s): {supplied_forbidden}")
    unknown = sorted(set(raw_params) - set(definitions))
    if unknown:
        raise ValueError(f"unsupported parameter(s) for {endpoint}: {unknown}")
    params = {}
    for name, value in raw_params.items():
        schema = definitions[name].get("schema") if isinstance(definitions[name].get("schema"), dict) else {}
        params[name] = _coerce(value, schema)
    identifier = _configured_identifier()
    if not identifier:
        raise ValueError("HDX HAPI app identifier unavailable; set HDA_HAPI_APP_IDENTIFIER or HMCP_HDX_APP_ID")

    # HDA forces JSON and starts at offset zero. Stop on a short page (assuming
    # the source honors the requested limit) or the local max_records ceiling.
    # Offset pagination is not a stable snapshot; concurrent source changes may
    # duplicate or omit records across pages. The timestamp marks query start.
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

    # A full final page makes "may_have_more" possible, not certain. The source
    # total is unknown. next_offset is descriptive only: the CLI has no option
    # to resume at that offset.
    may_have_more = last_page_size == min(page_size, max_records - (len(records) - last_page_size))
    # Preserve all distinct source resource IDs. For lineage, inspect only the
    # first 20 IDs in lexical order via sequential metadata/resource requests;
    # skip these requests when already querying metadata/resource. Missing/empty
    # metadata rows are skipped, but a request failure aborts the whole query.
    # The truncation flag describes the 20-ID ceiling, not skipped responses.
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
    # Lineage records source/request provenance and attached raw metadata, not
    # a semantic judgment. Request parameters here are coerced caller values,
    # excluding HDA-controlled paging/format and the app identifier.
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
    # "Complete" means all records retrieved within max_records, not the
    # complete HAPI dataset. This optional artifact has top-level records;
    # stdout instead nests a record window under "result".
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
    # Write the optional artifact only after pagination and all lineage lookups
    # succeed; without --output, there is no persisted full retrieved result.
    artifact_path = _write_json(output, complete, force) if output else None
    # These counts describe the intended record-count stdout window. A later
    # serialized-byte fallback may omit all those records without revising them.
    stdout_truncated = len(records) > stdout_records
    return {
        **{key: complete[key] for key in ("schema_version", "request_scope", "lineage", "pagination")},
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "result": {
            "returned_to_stdout": min(len(records), stdout_records),
            "truncated_for_stdout": stdout_truncated,
            "complete_result_artifact": artifact_path,
            "records": records[:stdout_records],
        },
    }


# Bound the final serialized stdout, after remote retrieval and any artifact
# write. If the window is too large, emit an output-limit envelope instead of
# records/endpoints, retaining relevant scope/lineage/pagination and marking
# data_omitted; its retained record counts refer to the intended window, not
# records actually emitted. The same byte fallback applies to list and query.
# An envelope that itself exceeds the limit fails rather than being truncated.
def _stdout_bytes(result: dict[str, Any], command: str, limit: int) -> bytes:
    def serialize(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    normal = serialize(result)
    if len(normal) <= limit:
        return normal
    envelope = {
        "schema_version": "hda-output-limit-envelope/v1",
        "source_schema_version": result["schema_version"],
        "source_id": result["source_id"],
        "interface_id": result["interface_id"],
        "stdout_bytes_limit": limit,
        "normal_serialized_bytes": len(normal),
        "output_limit_exceeded": True,
        "data_omitted": True,
        "reason": "Endpoints or result records were omitted because the serialized model-facing stdout limit was exceeded.",
    }
    if command == "list":
        for key in (
            "source", "base_endpoint", "openapi_url", "api_version",
            "discovered_at_utc", "authentication", "pagination",
            "response_formats", "endpoint_count",
        ):
            envelope[key] = result[key]
    else:
        for key in ("request_scope", "lineage", "pagination"):
            envelope[key] = result[key]
        envelope["result"] = {
            key: result["result"][key]
            for key in ("returned_to_stdout", "truncated_for_stdout", "complete_result_artifact")
        }
    emitted = serialize(envelope)
    if len(emitted) > limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit")
    return emitted


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
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT,
                        help=f"serialized UTF-8 stdout ceiling including newline (minimum/default: {DEFAULT_STDOUT_BYTES_LIMIT})")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        # This configurable ceiling may be raised but not lowered below 256 KiB.
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
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
        sys.stdout.buffer.write(_stdout_bytes(result, args.command, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA HAPI operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
