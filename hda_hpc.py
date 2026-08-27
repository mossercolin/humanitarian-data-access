#!/usr/bin/env python3
"""Discover and query OCHA HPC through one bounded, resource-agnostic interface."""

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


SOURCE = "OCHA HPC"
BASE_URL = "https://api.hpc.tools/v2/public"
SWAGGER_URL = "https://api.hpc.tools/api-docs"
DEFAULT_MAX_RECORDS = 10
DEFAULT_STDOUT_RECORDS = 5
MAX_RECORDS = 10_000
MAX_RELATIONSHIPS = 100
USER_AGENT = "hda-hpc/1.0"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"server returned invalid JSON (HTTP {response.status}, "
                f"content-type {response.headers.get('Content-Type')!r}, bytes {len(body)})"
            ) from exc


def _swagger(timeout: float) -> dict[str, Any]:
    payload = _request_json(SWAGGER_URL, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        raise ValueError("OCHA HPC Swagger document has no paths object")
    return payload


def _public_operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
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
        "source": SOURCE,
        "base_endpoint": BASE_URL,
        "swagger_url": SWAGGER_URL,
        "api_version": spec.get("info", {}).get("version"),
        "discovered_at_utc": _timestamp(),
        "authentication": {"required": False, "class": "public anonymous"},
        "pagination": {"server_side": False, "advertised_parameters": []},
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
        _coerce(raw_value, definition)
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
    return [record["id"] for record in records if isinstance(record, dict) and record.get("id") is not None]


def _explicit_relationships(records: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    found: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if len(found) >= MAX_RELATIONSHIPS:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if (key.endswith("Id") or key.endswith("Ids")) and child is not None:
                    found.append({"path": child_path, "field": key, "value": child})
                    if len(found) >= MAX_RELATIONSHIPS:
                        return
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    for index, record in enumerate(records):
        walk(record, f"records[{index}]")
    return found, len(found) >= MAX_RELATIONSHIPS


def _write_json(path: pathlib.Path, payload: Any) -> str:
    path = path.resolve()
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
) -> dict[str, Any]:
    spec = _swagger(timeout)
    operations = _public_operations(spec)
    url, literal_params = _build_request(endpoint, raw_params, operations)
    retrieved_at = _timestamp()
    payload = _request_json(url, timeout)
    all_records, envelope = _records(payload)
    selected = all_records[offset:offset + max_records]
    source_count = len(all_records)
    has_more = offset + len(selected) < source_count

    relationships, relationships_truncated = _explicit_relationships(selected)
    artifact_path = _write_json(output, payload) if output is not None else None

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
            "returned_count": len(selected),
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


def _nonnegative(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="discover public endpoints and parameters from live Swagger")
    query_parser = subparsers.add_parser("query", help="query any discovered public endpoint")
    query_parser.add_argument("endpoint")
    query_parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    query_parser.add_argument("--offset", type=_nonnegative, default=0)
    query_parser.add_argument("--max-records", type=_positive, default=DEFAULT_MAX_RECORDS)
    query_parser.add_argument("--stdout-records", type=_positive, default=DEFAULT_STDOUT_RECORDS)
    query_parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        if args.command == "list":
            result = discover(args.timeout)
        else:
            if args.max_records > MAX_RECORDS:
                raise ValueError(f"max records cannot exceed {MAX_RECORDS}")
            result = query(
                args.endpoint.strip("/"), _parse_params(args.param), args.offset,
                args.max_records, args.stdout_records, args.output, args.timeout,
            )
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        print()
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA HPC operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
