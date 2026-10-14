#!/usr/bin/env python3
"""Discover and query GDACS through its bounded anonymous REST interface."""
# `list` fetches live Swagger and summarizes eligible public GET paths.
# `query` fetches Swagger again, makes a separate data GET, and then selects
# local record windows for stdout; it does not interpret humanitarian meaning.
# An optional file stores the complete parsed response from that one data GET.
# Serialized stdout has a separate byte ceiling and may omit details entirely.

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
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
USER_AGENT = "hda-gdacs/1.0"
API_RESPONSE_LIMIT = 16 * 1024 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# The shared HTTP helper enforces a 16 MiB body-read ceiling for each response:
# both the Swagger fetch and the later data fetch, not their combined transfer.
# Status, content type, read byte count and final URL are observed HTTP facts;
# invalid JSON raises an error carrying those response facts.
def _request_json(url: str, timeout: float) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, API_RESPONSE_LIMIT, "GDACS API response", response.headers)
        facts = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "final_response_url": response.geturl()}
    try:
        return remote_json_loads(body), facts
    except ValueError as exc:
        raise ValueError(f"server returned invalid JSON ({facts})") from exc


# Retrieve the fixed live Swagger URL; only a top-level `paths` object is
# required. This is not full OpenAPI/Swagger reference resolution.
def _swagger(timeout: float) -> dict[str, Any]:
    payload, _ = _request_json(SWAGGER_URL, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        raise ValueError("GDACS Swagger document has no paths object")
    return payload


# Select directly declared GET operations under /api/ (case-insensitive prefix).
# Referenced path items and other HTTP methods are not followed or selected.
def _operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for path, item in spec["paths"].items():
        operation = item.get("get") if isinstance(item, dict) else None
        if path.lower().startswith("/api/") and isinstance(operation, dict):
            result[path.removeprefix("/")] = {"path": path, "operation": operation}
    return result


# Report only directly declared operation-level parameter names, required
# flags and primitive types. Path-level parameters and `$ref` entries are not
# resolved; unresolved references may appear with null parameter fields.
# Endpoint details/count come from live Swagger, whereas HDA identity, base
# URL, docs URL and the anonymous-access claim are locally declared.
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


# CLI parameters must be NAME=VALUE with a nonempty, nonrepeated name.
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


# Convert direct declared integer, number and boolean types only; other
# values remain text. No enum, format, range or humanitarian filter validation.
# URL encoding later stringifies these values: e.g. true -> True, 5 as a
# `number` -> 5.0; this is not publisher-preferred syntax normalization.
def _coerce(value: str, definition: dict[str, Any]) -> Any:
    schema = definition.get("schema") if isinstance(definition.get("schema"), dict) else {}
    kind = schema.get("type") or definition.get("type")
    if kind == "integer": return int(value)
    if kind == "number": return float(value)
    if kind == "boolean":
        if value.lower() not in {"true", "false"}: raise ValueError(f"expected true or false, got {value!r}")
        return value.lower() == "true"
    return value


# Mechanically choose `features` list before `data` list, then a root list;
# any other JSON value (including other wrapper objects) is one record.
def _records(payload: Any) -> tuple[list[Any], str]:
    if isinstance(payload, dict) and isinstance(payload.get("features"), list): return payload["features"], "features"
    if isinstance(payload, dict) and isinstance(payload.get("data"), list): return payload["data"], "data"
    if isinstance(payload, list): return payload, "root"
    return [payload], "single_object"


# Save the full parsed response for this request, re-serialized as JSON,
# not original HTTP bytes. The artifact does not include HDA result lineage.
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


# Each query re-fetches live Swagger before its separate data request;
# the Swagger fetch's response facts are not retained in the query result.
# Only direct, named operation-level parameters are checked for unknown or
# missing-required names. Path-level and `$ref` declarations are not enforced.
# All provided parameters go into the query string; path templates remain
# literal, and the endpoint host/base are locally fixed rather than read
# from Swagger. GDACS decides whether the resulting request is acceptable.
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
    # Local clock sampled BEFORE the data GET, not a publisher publication time.
    retrieved_at = _timestamp()
    payload, facts = _request_json(source_url, timeout)
    # The complete bounded data response is read and parsed before any local
    # `max_records` or `stdout_records` selection; neither limits remote size.
    all_records, envelope = _records(payload)
    selected = all_records[:max_records]
    artifact_path = _write_json(output, payload, force) if output else None
    page_size = next((int(v) for k, v in params.items() if k.lower() == "pagesize"), None)
    page_number = next((int(v) for k, v in params.items() if k.lower() == "pagenumber"), None)
    # Counts describe this received response, not all GDACS data. The local
    # selected count can exceed the proposed stdout record count; byte-limit
    # fallback may subsequently emit zero records without revising counters.
    # `truncated_for_stdout` also reports trimming by `max_records`.
    # `may_have_more` is true only with supplied `pagesize` and at least that
    # many received records; false cannot establish source completeness.
    # There is no automatic pagination or inferred humanitarian comparability.
    # `network` mixes observed HTTP facts, locally derived counts/page hints,
    # and the locally asserted `anonymous` flag. `source_url` is the constructed
    # request URL; `final_response_url` is the URL after any redirects.
    return {
        "schema_version": "hda-gdacs-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "operation": endpoint,
        "request_scope": {"parameters": params, "max_records": max_records},
        "lineage": {"source": "Global Disaster Alert and Coordination System (GDACS)", "official_docs": DOCS_URL, "swagger_url": SWAGGER_URL, "endpoint_url": endpoint_url, "source_url": source_url, "retrieved_at_utc": retrieved_at},
        "network": {"anonymous": True, **facts, "response_envelope": envelope, "response_record_count": len(all_records), "requested_page_size": page_size, "requested_page_number": page_number, "may_have_more": page_size is not None and len(all_records) >= page_size},
        "result": {"returned_count": len(selected), "returned_to_stdout": min(len(selected), stdout_records), "truncated_for_stdout": len(selected) > stdout_records or len(selected) < len(all_records), "complete_response_artifact": artifact_path, "records": selected[:stdout_records]},
        "diagnostics": ["may_have_more is conservative when a full requested page is returned because this response exposes no total count."],
    }


# First serialize normal output. If it exceeds the stdout byte ceiling,
# replace it wholesale: `list` omits endpoints; `query` omits records.
# The alternate envelope also drops normal diagnostics and changes schema
# version. Preserved counts/flags describe the normal candidate, NOT records
# actually emitted by fallback (`data_omitted` marks that omission).
# If even the fallback exceeds the ceiling, fail without writing to stdout;
# an optional data artifact may already have been saved by `query`.
def _stdout_bytes(result: dict[str, Any], command: str, limit: int) -> bytes:
    def serialize(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    normal = serialize(result)
    if len(normal) <= limit:
        return normal
    envelope = {
        "schema_version": "hda-output-limit-envelope/v1",
        "source_id": SOURCE_ID, "interface_id": INTERFACE_ID,
        "stdout_bytes_limit": limit, "normal_serialized_bytes": len(normal),
        "output_limit_exceeded": True, "data_omitted": True,
        "reason": "Endpoints or result records were omitted because the serialized model-facing stdout limit was exceeded.",
    }
    if command == "query":
        for key in ("operation", "request_scope", "lineage", "network"):
            envelope[key] = result[key]
        envelope["result"] = {key: value for key, value in result["result"].items() if key != "records"}
    else:
        envelope["operation"] = "list"
        for key in ("base_endpoint", "official_docs", "swagger_url", "authentication", "endpoint_count"):
            envelope[key] = result[key]
    emitted = serialize(envelope)
    if len(emitted) > limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit")
    return emitted


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
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT, help=f"serialized stdout byte ceiling (minimum/default: {DEFAULT_STDOUT_BYTES_LIMIT})")
    parser.add_argument("--timeout", type=float, default=30.0); args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        if args.timeout <= 0: raise ValueError("timeout must be positive")
        result = discover(args.timeout) if args.command == "list" else query(args.endpoint.strip("/"), _parse_params(args.param), args.max_records, args.stdout_records, args.output, args.timeout, args.force)
        sys.stdout.buffer.write(_stdout_bytes(result, args.command, args.stdout_bytes_limit)); return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA GDACS operation failed: {exc}\n")


if __name__ == "__main__": raise SystemExit(main())
