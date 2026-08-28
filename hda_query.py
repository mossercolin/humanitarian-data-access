#!/usr/bin/env python3
"""Run a bounded, field-level query over an acquired CSV artifact."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any


OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "contains"}
AGGREGATES = {"count", "sum", "min", "max", "avg"}
MAX_RETRIEVAL_LIMIT = 100_000
DEFAULT_LOCAL_INPUT_LIMIT = 64 * 1024 * 1024
DEFAULT_CSV_FIELD_SIZE_LIMIT = 1024 * 1024


class _BoundedBinaryReader(io.RawIOBase):
    def __init__(self, stream: Any, byte_limit: int) -> None:
        self._stream = stream
        self._remaining = byte_limit
        self._byte_limit = byte_limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._remaining == 0:
            if self._stream.read(1):
                raise ValueError(f"local CSV input exceeds the {self._byte_limit}-byte limit")
            return 0
        chunk = self._stream.read(min(len(buffer), self._remaining))
        size = len(chunk)
        buffer[:size] = chunk
        self._remaining -= size
        return size


def _number(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"expected a numeric value, got {value!r}") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    return value


def _validate_fields(fields: list[str], available: list[str], label: str) -> None:
    unknown = sorted(set(fields) - set(available))
    if unknown:
        raise ValueError(f"unknown {label} field(s): {unknown}; available fields: {available}")


def _matches(row: dict[str, str], condition: dict[str, Any]) -> bool:
    field = condition["field"]
    op = condition["op"]
    expected = str(condition["value"])
    actual = row[field]
    if op not in OPS:
        raise ValueError(f"unsupported filter operation: {op!r}")
    if condition.get("numeric", False):
        left, right = _number(actual), _number(expected)
    else:
        left, right = actual, expected
    return {
        "eq": left == right,
        "ne": left != right,
        "lt": left < right,
        "lte": left <= right,
        "gt": left > right,
        "gte": left >= right,
        "contains": expected in actual,
    }[op]


def _aggregate(rows: list[dict[str, str]], spec: dict[str, Any]) -> Any:
    op = spec["op"]
    if op not in AGGREGATES:
        raise ValueError(f"unsupported aggregate operation: {op!r}")
    if op == "count":
        return len(rows)
    values = [_number(row[spec["field"]]) for row in rows]
    if not values:
        return None
    if op == "sum":
        return sum(values, Decimal(0))
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    return sum(values, Decimal(0)) / len(values)


def _load_csv(path: pathlib.Path, skip_data_rows: int,
              csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
              local_input_byte_limit: int = DEFAULT_LOCAL_INPUT_LIMIT) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("rb") as binary_stream, io.TextIOWrapper(
            io.BufferedReader(_BoundedBinaryReader(binary_stream, local_input_byte_limit)),
            encoding="utf-8-sig", newline="",
        ) as stream:
        csv.field_size_limit(csv_field_size_limit)
        reader = csv.reader(stream)
        raw_headers = next(reader, None)
        if raw_headers is None:
            raise ValueError("CSV has no header row")
        headers = [item.strip() for item in raw_headers]
        if any(not item for item in headers) or len(headers) != len(set(headers)):
            raise ValueError("CSV headers must be non-empty and unique after surrounding whitespace is removed")
        for _ in range(skip_data_rows):
            next(reader, None)
        rows = []
        for line_number, values in enumerate(reader, start=2 + skip_data_rows):
            if len(values) != len(headers):
                raise ValueError(f"CSV row {line_number} has {len(values)} fields; expected {len(headers)}")
            rows.append(dict(zip(headers, (value.strip() for value in values), strict=True)))
    return headers, rows


def run_query(csv_path: pathlib.Path, query: dict[str, Any],
              csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
              local_input_byte_limit: int = DEFAULT_LOCAL_INPUT_LIMIT) -> tuple[list[str], list[dict[str, Any]]]:
    allowed = {"select", "filters", "group_by", "aggregates", "sort", "limit", "skip_data_rows"}
    extra = sorted(set(query) - allowed)
    if extra:
        raise ValueError(f"unknown query keys: {extra}")
    limit = query.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RETRIEVAL_LIMIT:
        raise ValueError(f"limit is required and must be an integer from 1 to {MAX_RETRIEVAL_LIMIT}")
    skip_data_rows = query.get("skip_data_rows", 0)
    if not isinstance(skip_data_rows, int) or isinstance(skip_data_rows, bool) or skip_data_rows < 0:
        raise ValueError("skip_data_rows must be a non-negative integer")
    headers, rows = _load_csv(csv_path, skip_data_rows, csv_field_size_limit, local_input_byte_limit)
    filters = query.get("filters", [])
    _validate_fields([item["field"] for item in filters], headers, "filter")
    rows = [row for row in rows if all(_matches(row, item) for item in filters)]

    group_by = query.get("group_by", [])
    aggregates = query.get("aggregates", [])
    _validate_fields(group_by, headers, "group")
    _validate_fields([item["field"] for item in aggregates if item["op"] != "count"], headers, "aggregate")
    if group_by or aggregates:
        groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
        if group_by:
            for row in rows:
                groups[tuple(row[field] for field in group_by)].append(row)
        else:
            groups[()] = rows
        result = []
        for key, group_rows in groups.items():
            item: dict[str, Any] = dict(zip(group_by, key, strict=True))
            for spec in aggregates:
                alias = spec.get("as") or ("count" if spec["op"] == "count" else f"{spec['op']}_{spec['field']}")
                item[alias] = _json_value(_aggregate(group_rows, spec))
            result.append(item)
        output_fields = group_by + [spec.get("as") or ("count" if spec["op"] == "count" else f"{spec['op']}_{spec['field']}") for spec in aggregates]
    else:
        output_fields = query.get("select") or headers
        _validate_fields(output_fields, headers, "select")
        result = [{field: row[field] for field in output_fields} for row in rows]

    for sort_spec in reversed(query.get("sort", [])):
        field = sort_spec["field"]
        _validate_fields([field], output_fields, "sort")
        numeric = sort_spec.get("numeric", False)
        result.sort(key=lambda row: _number(str(row[field])) if numeric else str(row[field]), reverse=sort_spec.get("direction", "asc") == "desc")
    return output_fields, result[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=pathlib.Path)
    parser.add_argument("--query", required=True, help="JSON query object")
    parser.add_argument("--stdout-rows", type=int, default=10)
    parser.add_argument("--output-dir", type=pathlib.Path)
    parser.add_argument("--force", action="store_true", help="deliberately replace an existing derived query artifact")
    parser.add_argument(
        "--local-input-byte-limit", type=int, default=DEFAULT_LOCAL_INPUT_LIMIT,
        help=f"maximum local CSV input bytes (default: {DEFAULT_LOCAL_INPUT_LIMIT}); increase deliberately for an expected large file",
    )
    parser.add_argument(
        "--csv-field-size-limit", type=int, default=DEFAULT_CSV_FIELD_SIZE_LIMIT,
        help=f"maximum parsed CSV field characters (default: {DEFAULT_CSV_FIELD_SIZE_LIMIT}); increase deliberately for expected wide fields",
    )
    args = parser.parse_args()
    try:
        if args.stdout_rows < 1:
            raise ValueError("stdout-rows must be positive")
        if args.local_input_byte_limit < 1 or args.csv_field_size_limit < 1:
            raise ValueError("local input and CSV field size limits must be positive")
        query = json.loads(args.query)
        if not isinstance(query, dict):
            raise ValueError("query must be a JSON object")
        csv_path = args.csv.resolve(strict=True)
        input_size = csv_path.stat().st_size
        if input_size > args.local_input_byte_limit:
            raise ValueError(
                f"local CSV input size {input_size} exceeds the {args.local_input_byte_limit}-byte limit; "
                "use --local-input-byte-limit with an expected larger size"
            )
        sidecar_path = csv_path.with_name(csv_path.name + ".acquisition.json")
        lineage = json.loads(sidecar_path.read_text(encoding="utf-8"))
        fields, rows = run_query(
            csv_path, query, args.csv_field_size_limit, args.local_input_byte_limit,
        )
        artifact_path = None
        if len(rows) > args.stdout_rows and args.output_dir is not None:
            digest = hashlib.sha256((str(csv_path) + "\n" + json.dumps(query, sort_keys=True)).encode()).hexdigest()[:16]
            args.output_dir.mkdir(parents=True, exist_ok=True)
            target = (args.output_dir / f"query-{digest}.json").resolve()
            if target.exists() and not args.force:
                raise FileExistsError(f"output already exists: {target}; use --force to replace it")
            fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"lineage": lineage, "fields": fields, "rows": rows}, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            artifact_path = str(target)
        response = {
            "schema_version": "hda-query/v1",
            "lineage": lineage,
            "query": query,
            "result": {
                "fields": fields,
                "retrieved_row_count": len(rows),
                "returned_row_count": min(len(rows), args.stdout_rows),
                "truncated_for_stdout": len(rows) > args.stdout_rows,
                "full_result_artifact": artifact_path,
                "rows": rows[:args.stdout_rows],
            },
        }
        json.dump(response, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        print()
        return 0
    except (OSError, ValueError, KeyError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        parser.exit(1, f"HDA query failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
