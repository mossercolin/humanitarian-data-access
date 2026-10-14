#!/usr/bin/env python3
"""Discover and query OCHA HPC through one bounded, resource-agnostic interface."""

# Flow: fresh Swagger discovery -> a selected public GET -> one JSON response ->
# local record selection -> optional complete-response artifact -> bounded stdout.
# These are distinct scopes. The remote response is read before record windowing;
# a result window or smaller stdout does not reduce network transfer. Lineage
# describes this retrieval, not source-wide completeness or humanitarian meaning.
# Swagger supplies available operations and selected metadata, while this client
# declares its own authentication and pagination descriptions.

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


SOURCE_ID = "ocha_hpc"
INTERFACE_ID = "ocha_hpc_v2_public"
SOURCE = "OCHA HPC"
BASE_URL = "https://api.hpc.tools/v2/public"
SWAGGER_URL = "https://api.hpc.tools/api-docs"
API_RESPONSE_LIMIT = 16 * 1024 * 1024
DEFAULT_MAX_RECORDS = 10
DEFAULT_STDOUT_RECORDS = 5
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
MAX_RECORDS = 10_000
MAX_RELATIONSHIPS = 100
USER_AGENT = "hda-hpc/1.0"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str, timeout: float) -> Any:
    """Read one remote JSON body under the 16 MiB response-byte ceiling.

    The same bound applies to Swagger and data responses. Local record and stdout
    limits do not reduce the bytes needed to receive this response.
    """
    request = urllib.request.Request(
        require_http_url(url),
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, API_RESPONSE_LIMIT, "HPC API response", response.headers)
        try:
            return remote_json_loads(body)
        except ValueError as exc:
            raise ValueError(
                f"server returned invalid JSON (HTTP {response.status}, "
                f"content-type {response.headers.get('Content-Type')!r}, bytes {len(body)})"
            ) from exc


def _swagger(timeout: float) -> dict[str, Any]:
    """Fetch live Swagger afresh for each list or query; there is no spec cache."""
    payload = _request_json(SWAGGER_URL, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        raise ValueError("OCHA HPC Swagger document has no paths object")
    return payload


def _public_operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select only GET operations under /v2/public/, keeping templated paths.

    Other methods and paths are excluded; the returned endpoint identifier is
    the path remainder, including any {parameter} placeholders.
    """
    operations = {}
    prefix = "/v2/public/"
    for path, path_item in spec["paths"].items():
        operation = path_item.get("get") if isinstance(path_item, dict) else None
        if path.startswith(prefix) and isinstance(operation, dict):
            operations[path.removeprefix(prefix)] = {"path": path, "operation": operation}
    return operations


def _parameter_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "location": item.get("in"),
        "required": bool(item.get("required", False)),
        "type": item.get("type") or (item.get("schema") or {}).get("type"),
    }


def discover(timeout: float) -> dict[str, Any]:
    """Describe public GET routes from fresh Swagger, not a cached catalog.

    Parameters come only from inline operation-level definitions; path-item
    parameters and $ref definitions are not resolved. Response formats collect
    operation-level "produces" declarations, not negotiated response types:
    requests always ask for JSON. The authentication and pagination descriptions
    in this output are local declarations, not deductions from Swagger.
    """
    spec = _swagger(timeout)
    endpoints = []
    formats = set()
    for endpoint, details in sorted(_public_operations(spec).items()):
        operation = details["operation"]
        formats.update(operation.get("produces") or [])
        endpoints.append({
            "endpoint": endpoint,
            "parameters": [_parameter_summary(item) for item in operation.get("parameters") or []],
        })
    return {
        "schema_version": "hda-hpc-surface/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "source": SOURCE,
        "base_endpoint": BASE_URL,
        "swagger_url": SWAGGER_URL,
        "api_version": spec.get("info", {}).get("version"),
        "discovered_at_utc": _timestamp(),
        "authentication": {"required": False, "class": "public anonymous"},
        "pagination": {"method": "none_observed", "advertised_parameters": []},
        "response_formats": sorted(formats),
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
    }


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
    """Check simple declared integer, number or boolean syntax only.

    This does not enforce enums, ranges, patterns, formats or complex schemas.
    The request builder deliberately discards the converted value and sends the
    original string after URL encoding.
    """
    kind = definition.get("type") or (definition.get("schema") or {}).get("type")
    if kind in {"integer", "number"}:
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        if value.lower() not in {"true", "false"}:
            raise ValueError(f"expected true or false, got {value!r}")
        return value.lower() == "true"
    return value


def _build_request(
    endpoint: str, raw_params: dict[str, str], operations: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Construct a URL using inline operation-level Swagger parameters only.

    Reject unknown or missing required parameter names and locations other than
    path/query; unresolved $ref and path-item parameter definitions are not
    enforced. Values get limited parsing checks, then retain their original text
    (percent-encoded in the path or encoded as query-string parameters).
    """
    if endpoint not in operations:
        raise ValueError(f"unknown endpoint {endpoint!r}; run 'hda_hpc.py list'")
    definitions = {
        item.get("name"): item
        for item in operations[endpoint]["operation"].get("parameters") or []
        if item.get("name")
    }
    unknown = sorted(set(raw_params) - set(definitions))
    if unknown:
        raise ValueError(f"unsupported parameter(s) for {endpoint}: {unknown}")
    missing = sorted(
        name for name, definition in definitions.items()
        if definition.get("required") and name not in raw_params
    )
    if missing:
        raise ValueError(f"missing required parameter(s) for {endpoint}: {missing}")

    literal_params: dict[str, Any] = {}
    rendered_path = operations[endpoint]["path"]
    query_params = []
    for name, raw_value in raw_params.items():
        definition = definitions[name]
        _coerce(raw_value, definition)  # Validate syntax; keep raw_value for URL and lineage.
        literal_params[name] = raw_value
        if definition.get("in") == "path":
            rendered_path = rendered_path.replace("{" + name + "}", urllib.parse.quote(raw_value, safe=""))
        elif definition.get("in") == "query":
            query_params.append((name, str(raw_value)))
        else:
            raise ValueError(f"unsupported parameter location for {name}: {definition.get('in')!r}")
    url = "https://api.hpc.tools" + rendered_path
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params)
    return url, literal_params


def _records(payload: Any) -> tuple[list[Any], str]:
    """Interpret the received JSON as a collection without semantic inference.

    A dict containing top-level "data" uses that value; otherwise use the root.
    Null yields no records, a list yields its elements, and every other value
    (including a dict with a differently named array) counts as one record.
    """
    if isinstance(payload, dict) and "data" in payload:
        data = payload["data"]
        envelope = "data"
    else:
        data = payload
        envelope = "root"
    if data is None:
        return [], envelope
    if isinstance(data, list):
        return data, envelope
    return [data], envelope


def _native_ids(records: list[Any]) -> list[Any]:
    """Collect non-null top-level "id" values from selected dictionary records.

    There is no deduplication or placeholder for records without an ID, so this
    list need not align positionally with the selected or printed records.
    """
    return [record["id"] for record in records if isinstance(record, dict) and record.get("id") is not None]


def _explicit_relationships(records: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Collect syntactic ID-like fields from the selected record window only.

    Traverse dictionaries and lists depth-first, matching non-null dictionary
    fields whose names end in exactly "Id" or "Ids" (case-sensitive). Values are
    kept whole, not checked as real references. The 100-entry ceiling is global
    across selected records; earlier matches may exhaust it. The returned flag
    means 100 were reached, NOT that omitted relationships were established.
    """
    found: list[dict[str, Any]] = []
    stack: list[tuple[Any, str, str | None]] = [
        (record, f"records[{index}]", None) for index, record in reversed(list(enumerate(records)))
    ]
    while stack and len(found) < MAX_RELATIONSHIPS:
        value, path, field = stack.pop()
        if field is not None and (field.endswith("Id") or field.endswith("Ids")) and value is not None:
            found.append({"path": path, "field": field, "value": value})
            if len(found) >= MAX_RELATIONSHIPS:
                break
        if isinstance(value, dict):
            stack.extend((child, f"{path}.{key}", key) for key, child in reversed(list(value.items())))
        elif isinstance(value, list):
            stack.extend((child, f"{path}[{index}]", None) for index, child in reversed(list(enumerate(value))))
    return found, len(found) >= MAX_RELATIONSHIPS


def _write_json(path: pathlib.Path, payload: Any, force: bool = False) -> str:
    """Publish the full parsed payload as reserialized, sorted, indented JSON.

    This is not the original HTTP byte stream. Write and sync a temporary file
    beside the destination before replacing it. Existing paths are ordinarily
    refused unless --force is set; the exists check and replace are not an
    atomic no-clobber guarantee against concurrent writers.
    """
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
    offset: int,
    max_records: int,
    stdout_records: int,
    output: pathlib.Path | None,
    timeout: float,
    force: bool = False,
) -> dict[str, Any]:
    """Query one public GET route, then expose distinct local result scopes.

    Fetch fresh Swagger, build a request, and receive one complete JSON response
    within the response-byte ceiling; no additional server pages are followed.
    Interpret the received envelope and select offset/max_records locally. The
    CLI (not this helper) caps max_records at 10,000. Native IDs and ID-like
    relationships describe these selected records, even those not printed.

    If requested, publish the complete parsed response as an artifact, regardless
    of the local selection. Stdout records are a further stdout_records slice;
    a separate byte limit is applied later by the CLI. source_record_count and
    has_more/next_offset describe only the received, interpreted collection,
    never a source-global total or remote pagination. Pagination descriptions
    are locally declared. Retrieval lineage proves neither completeness nor
    humanitarian meaning.
    """
    if output is not None and output.exists() and not force:
        raise FileExistsError(f"output already exists: {output}; use --force to replace it")
    spec = _swagger(timeout)
    operations = _public_operations(spec)
    url, literal_params = _build_request(endpoint, raw_params, operations)
    retrieved_at = _timestamp()
    payload = _request_json(url, timeout)
    all_records, envelope = _records(payload)
    selected = all_records[offset:offset + max_records]
    source_count = len(all_records)  # Count in this received envelope, not a source-wide total.
    has_more = offset + len(selected) < source_count

    relationships, relationships_truncated = _explicit_relationships(selected)
    artifact_path = _write_json(output, payload, force) if output is not None else None

    lineage = {
        "source": SOURCE,
        "base_endpoint": BASE_URL,
        "endpoint": endpoint,
        "request_parameters": literal_params,
        "retrieved_at_utc": retrieved_at,
        "native_ids": _native_ids(selected),
        "explicit_relationships": relationships,
        "explicit_relationships_truncated": relationships_truncated,
        "artifact_path": artifact_path,
    }
    stdout_slice = selected[:stdout_records]
    return {
        "schema_version": "hda-hpc-result/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "request_scope": {
            "endpoint": endpoint,
            "parameters": literal_params,
            "offset": offset,
            "max_records": max_records,
        },
        "lineage": lineage,
        "pagination": {
            "mechanism": "client-side over one unpaginated API response",
            "server_pagination_advertised": False,
            "response_envelope": envelope,
            "source_record_count": source_count,
            "offset": offset,
            "records_selected": len(selected),
            "has_more": has_more,
            "next_offset": offset + len(selected) if has_more else None,
        },
        "result": {
            "returned_count": len(selected),  # Selected locally; not necessarily printed.
            "returned_to_stdout": len(stdout_slice),
            "truncated_for_stdout": len(stdout_slice) < len(selected),
            "complete_response_artifact": artifact_path,
            "records": stdout_slice,
        },
    }


def _positive(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def _stdout_bytes_limit(value: str) -> int:
    result = _positive(value)
    if result < DEFAULT_STDOUT_BYTES_LIMIT:
        raise argparse.ArgumentTypeError(f"must be at least {DEFAULT_STDOUT_BYTES_LIMIT}")
    return result


def _list_stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    """Bound serialized discovery stdout by bytes, with no record-count limit.

    If oversized, omit the entire endpoint list and return an explanatory
    envelope. If that envelope also exceeds the ceiling, raise an error.
    """
    normal = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(normal) <= limit:
        return normal
    envelope = {
        "schema_version": "hda-output-limit-envelope/v1",
        "source_schema_version": result["schema_version"],
        **{key: result[key] for key in (
            "source_id", "interface_id", "source", "base_endpoint", "swagger_url",
            "api_version", "discovered_at_utc", "authentication", "pagination",
            "response_formats", "endpoint_count",
        )},
        "stdout_bytes_limit": limit,
        "normal_serialized_bytes": len(normal),
        "output_limit_exceeded": True,
        "data_omitted": True,
        "reason": (
            "Discovery output exceeds the stdout byte limit; endpoints are omitted. "
            "Deliberately increase --stdout-bytes-limit to retrieve the full list."
        ),
    }
    prepared = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(prepared) > limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit; increase --stdout-bytes-limit")
    return prepared


def _query_stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    """Apply a whole-document byte limit after query record-count windowing.

    If oversized, omit records, native IDs and relationships together, retaining
    counts and retrieval metadata. Retained stdout counters describe the planned
    record slice, not records emitted after this byte-driven omission; consult
    data_omitted/output_limit_exceeded instead. If even this envelope is too
    large, raise an error; any requested artifact was already published.
    """
    def serialize(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    normal = serialize(result)
    if len(normal) <= limit:
        return normal
    lineage = result["lineage"]
    envelope = {
        "schema_version": "hda-output-limit-envelope/v1",
        "source_schema_version": result["schema_version"],
        "source_id": result["source_id"],
        "interface_id": result["interface_id"],
        "request_scope": result["request_scope"],
        "pagination": result["pagination"],
        "lineage": {key: lineage[key] for key in (
            "source", "base_endpoint", "endpoint", "request_parameters",
            "retrieved_at_utc", "artifact_path", "explicit_relationships_truncated",
        )},
        "native_id_count": len(lineage["native_ids"]),
        "explicit_relationship_count": len(lineage["explicit_relationships"]),
        "result": {key: value for key, value in result["result"].items() if key != "records"},
        "stdout_bytes_limit": limit,
        "normal_serialized_bytes": len(normal),
        "output_limit_exceeded": True,
        "data_omitted": True,
        "reason": (
            "Query output exceeds the stdout byte limit; records, native IDs, and explicit "
            "relationships are omitted. Use the artifact if available, narrow the query, "
            "or deliberately increase --stdout-bytes-limit."
        ),
    }
    prepared = serialize(envelope)
    if len(prepared) > limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit; narrow the query or increase --stdout-bytes-limit")
    return prepared


def _nonnegative(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Expose list (byte bound) and query (record and byte bounds) as CLI commands.

    The stdout-byte ceiling defaults to 256 KiB and can only be increased.
    Query's max-records limit is enforced here, not inside query().
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="discover public endpoints and parameters from live Swagger")
    list_parser.add_argument("--stdout-bytes-limit", type=_stdout_bytes_limit, default=DEFAULT_STDOUT_BYTES_LIMIT)
    query_parser = subparsers.add_parser("query", help="query any discovered public endpoint")
    query_parser.add_argument("endpoint")
    query_parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    query_parser.add_argument("--offset", type=_nonnegative, default=0)
    query_parser.add_argument("--max-records", type=_positive, default=DEFAULT_MAX_RECORDS)
    query_parser.add_argument("--stdout-records", type=_positive, default=DEFAULT_STDOUT_RECORDS)
    query_parser.add_argument("--stdout-bytes-limit", type=_stdout_bytes_limit, default=DEFAULT_STDOUT_BYTES_LIMIT)
    query_parser.add_argument("--output", type=pathlib.Path)
    query_parser.add_argument("--force", action="store_true", help="deliberately replace an existing output artifact")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        if args.command == "list":
            result = discover(args.timeout)
            sys.stdout.buffer.write(_list_stdout_bytes(result, args.stdout_bytes_limit))
        else:
            if args.max_records > MAX_RECORDS:
                raise ValueError(f"max records cannot exceed {MAX_RECORDS}")
            result = query(
                args.endpoint.strip("/"), _parse_params(args.param), args.offset,
                args.max_records, args.stdout_records, args.output, args.timeout, args.force,
            )
            sys.stdout.buffer.write(_query_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA HPC operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
