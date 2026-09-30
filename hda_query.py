#!/usr/bin/env python3
"""Run a bounded, field-level query over an acquired CSV artifact.

This module turns a local acquired CSV into a smaller structured query result without
adding a database or query framework.  A query can select fields, filter rows, group
and aggregate them, sort the derived rows, and finally cap the result with ``limit``.
Filtering happens before grouping/selection, sorting happens after those transforms,
and the query limit is applied last.  ``select`` is therefore ignored when grouping or
aggregation is requested, matching the existing contract.

The query is deliberately small and explicit.  Filters support eq/ne/lt/lte/gt/gte
and contains; aggregates support count/sum/min/max/avg.  Text comparison is the
default.  With ``numeric: true`` values are first required to parse as finite Decimal
numbers; ``numeric: true`` with ``contains`` still performs the historical raw-string
substring test after that numeric validation.  Sorts may request numeric ordering and
are applied in reverse key order because Python's sort is stable: sorting the least
significant key first preserves the later, more significant keys.

Four different bounds protect different surfaces and must not be confused:

* ``limit`` caps the derived query result at at most 100,000 rows.  It is applied after
  filtering/grouping/selection/sorting, so it does not bound the work or memory used by
  those earlier phases.
* the local-input byte limit caps how many CSV bytes may be consumed.  ``main()`` first
  checks the filesystem size for an early refusal, while ``_BoundedBinaryReader`` also
  enforces the limit during the live read because a file can change after that check.
  The byte limit does not equal a Python-memory limit: decoded strings and per-row
  dictionaries can occupy more memory than the source file.
* the CSV field-size limit caps one parsed CSV field.  Python exposes this as process-
  global parser state, so the loader temporarily changes it and restores the previous
  value before returning.
* stdout has a row window and a separate serialized-byte ceiling.  The row window
  decides how many result rows normally enter stdout.  If that normal JSON would still
  exceed the byte ceiling, an output-limit envelope is emitted with ``result.rows``
  omitted.  The envelope keeps the normal result metadata; therefore
  ``returned_row_count`` describes the normal row window, not rows physically present
  in the envelope.

Numeric work uses Decimal so comparisons and aggregate arithmetic are not forced
through binary floats.  JSON output preserves the existing representation: integral
Decimals become JSON integers and non-integral Decimals become JSON numbers through a
finite float.  Before Decimal -> int conversion, values needing more than 4,300 decimal
integer digits are refused; this prevents compact scientific notation from expanding
into pathological integers during conversion/serialization.  Normal finite numbers
are not converted to strings.  JSON emission also refuses NaN/Infinity as a final
standards-compliance backstop.

Acquisition lineage comes from the required ``<csv>.acquisition.json`` sidecar and is
embedded in output as supplied; this module does not independently verify that lineage
against the CSV contents.  ``retrieved_row_count`` is the number of rows left after the
query ``limit`` has already been applied.  It is not a total-match count, and equality
to ``limit`` does not establish whether additional matching rows existed.

When the bounded query result has more rows than the stdout row window and an output
directory is supplied, the complete bounded query result (after ``limit``) is also
written as a local artifact.  Publication writes a temporary file in the destination
directory, flushes and fsyncs that file, then replaces the target so ordinary readers
do not observe a partially written destination.  The pre-existing-target check is not
an atomic no-clobber primitive, and the directory itself is not fsynced, so this is not
a claim of race-proof no-clobber or full crash durability.  Byte-limit omission alone
does not create an artifact; that existing behavior is intentional for this contract.

Expected query/CSV/serialization failures are converted by the CLI into one concise
``HDA query failed`` error.  The module stays standard-library-only and intentionally
materializes a bounded CSV in memory rather than introducing a streaming query engine.
"""

from __future__ import annotations

# CLI and structured-text parsing define the external query/response contract.
import argparse
import csv
import json

# Stream, filesystem, and publication primitives keep local input/output bounded and
# allow result artifacts to be published without exposing a partially written target.
import io
import os
import pathlib
import sys
import tempfile

# Hashing gives derived artifacts deterministic names; grouping and Decimal support the
# query transformations without adding a database or third-party numeric dependency.
import hashlib
import math
from collections import defaultdict
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any


OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "contains"}
AGGREGATES = {"count", "sum", "min", "max", "avg"}
MAX_RETRIEVAL_LIMIT = 100_000
MAX_JSON_INTEGER_DIGITS = 4_300
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
DEFAULT_LOCAL_INPUT_LIMIT = 64 * 1024 * 1024
DEFAULT_CSV_FIELD_SIZE_LIMIT = 1024 * 1024


class _BoundedBinaryReader(io.RawIOBase):
    """Expose at most one configured number of source bytes to the CSV decoder.

    The caller also performs an inexpensive filesystem-size check, but that snapshot is
    not sufficient by itself: a file can grow between the check and the read.  This
    reader therefore counts bytes while they are actually consumed.  Once the budget is
    exhausted it probes one byte beyond the boundary; finding data there turns the read
    into a clean size-limit failure instead of silently treating an oversized CSV as if
    it ended exactly at the limit.
    """

    def __init__(self, stream: Any, byte_limit: int) -> None:
        self._stream = stream
        self._remaining = byte_limit
        self._byte_limit = byte_limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._remaining == 0:
            # The extra-byte probe distinguishes an exact-boundary file from a larger
            # file without allowing the larger payload to flow into the CSV parser.
            if self._stream.read(1):
                raise ValueError(f"local CSV input exceeds the {self._byte_limit}-byte limit")
            return 0
        chunk = self._stream.read(min(len(buffer), self._remaining))
        size = len(chunk)
        buffer[:size] = chunk
        self._remaining -= size
        return size


def _number(value: str) -> Decimal:
    """Parse one query/data value as a finite Decimal for numeric operations.

    Decimal keeps text-to-number comparison and aggregation independent of binary-float
    rounding.  NaN and infinities are rejected because they make ordering, aggregation,
    and standards-compliant JSON output ambiguous or unsafe.
    """

    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"expected a numeric value, got {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"expected a finite numeric value, got {value!r}")
    return number


def _json_value(value: Any) -> Any:
    """Convert internal Decimal results to the existing JSON number representation.

    Integral Decimals remain JSON integers, but conversion is refused before ``int``
    when the magnitude would require more than 4,300 decimal integer digits.  That
    boundary prevents compact exponent notation from expanding into a pathological
    integer during conversion or JSON serialization.  Fractional Decimals retain the
    historical JSON-number shape through ``float``; the float must remain finite.
    """

    if not isinstance(value, Decimal):
        return value
    if not value.is_finite():
        raise ValueError(f"cannot serialize non-finite Decimal value: {value}")
    if value == value.to_integral():
        integer_digits = 1 if value.is_zero() else value.copy_abs().adjusted() + 1
        if integer_digits > MAX_JSON_INTEGER_DIGITS:
            raise ValueError(
                f"integer result requires {integer_digits} decimal digits; "
                f"maximum is {MAX_JSON_INTEGER_DIGITS}"
            )
        return int(value)
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("numeric result is outside the finite JSON float range")
    return converted


def _validate_query_contract(query: dict[str, Any]) -> None:
    """Validate query structure and data-independent semantics before CSV execution.

    This phase rejects misspelled nested keys, missing required keys, unsupported
    operations, invalid sort directions, and non-boolean ``numeric`` flags regardless
    of whether the CSV later contains zero rows or filters short-circuit.  Field-name
    existence is intentionally deferred until the real CSV header is available.

    Current compatibility semantics are preserved: ``select`` may coexist with
    grouping/aggregation (and is then ignored), and ``numeric: true`` may coexist with
    ``contains`` (numeric parsing is required before the raw-string substring test).
    """

    if not isinstance(query, dict):
        raise ValueError("query must be a JSON object")

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

    for name in ("select", "group_by"):
        value = query.get(name, [])
        if not isinstance(value, list) or any(not isinstance(field, str) or not field for field in value):
            raise ValueError(f"{name} must be a list of non-empty field names")

    filters = query.get("filters", [])
    if not isinstance(filters, list):
        raise ValueError("filters must be a list")
    for index, condition in enumerate(filters):
        if not isinstance(condition, dict):
            raise ValueError(f"filter {index} must be an object")
        allowed_filter = {"field", "op", "value", "numeric"}
        extra_filter = sorted(set(condition) - allowed_filter)
        if extra_filter:
            raise ValueError(f"filter {index} has unknown keys: {extra_filter}")
        missing = [key for key in ("field", "op", "value") if key not in condition]
        if missing:
            raise ValueError(f"filter {index} is missing required keys: {missing}")
        if not isinstance(condition["field"], str) or not condition["field"]:
            raise ValueError(f"filter {index} field must be a non-empty string")
        if not isinstance(condition["op"], str) or condition["op"] not in OPS:
            raise ValueError(f"unsupported filter operation: {condition['op']!r}")
        if "numeric" in condition and not isinstance(condition["numeric"], bool):
            raise ValueError(f"filter {index} numeric must be a JSON boolean")
        if condition.get("numeric", False):
            # Validate the query-side numeric literal now so an empty dataset cannot
            # accidentally make a malformed numeric condition appear valid.
            _number(str(condition["value"]))

    aggregates = query.get("aggregates", [])
    if not isinstance(aggregates, list):
        raise ValueError("aggregates must be a list")
    for index, spec in enumerate(aggregates):
        if not isinstance(spec, dict):
            raise ValueError(f"aggregate {index} must be an object")
        allowed_aggregate = {"field", "op", "as"}
        extra_aggregate = sorted(set(spec) - allowed_aggregate)
        if extra_aggregate:
            raise ValueError(f"aggregate {index} has unknown keys: {extra_aggregate}")
        if "op" not in spec:
            raise ValueError(f"aggregate {index} is missing required key: 'op'")
        if not isinstance(spec["op"], str) or spec["op"] not in AGGREGATES:
            raise ValueError(f"unsupported aggregate operation: {spec['op']!r}")
        if spec["op"] != "count" and "field" not in spec:
            raise ValueError(f"aggregate {index} operation {spec['op']!r} requires 'field'")
        if "field" in spec and (not isinstance(spec["field"], str) or not spec["field"]):
            raise ValueError(f"aggregate {index} field must be a non-empty string")
        if "as" in spec and not isinstance(spec["as"], str):
            raise ValueError(f"aggregate {index} as must be a string when provided")

    sort_specs = query.get("sort", [])
    if not isinstance(sort_specs, list):
        raise ValueError("sort must be a list")
    for index, spec in enumerate(sort_specs):
        if not isinstance(spec, dict):
            raise ValueError(f"sort {index} must be an object")
        allowed_sort = {"field", "direction", "numeric"}
        extra_sort = sorted(set(spec) - allowed_sort)
        if extra_sort:
            raise ValueError(f"sort {index} has unknown keys: {extra_sort}")
        if "field" not in spec:
            raise ValueError(f"sort {index} is missing required key: 'field'")
        if not isinstance(spec["field"], str) or not spec["field"]:
            raise ValueError(f"sort {index} field must be a non-empty string")
        direction = spec.get("direction", "asc")
        if not isinstance(direction, str) or direction not in {"asc", "desc"}:
            raise ValueError(f"sort {index} direction must be 'asc' or 'desc'")
        if "numeric" in spec and not isinstance(spec["numeric"], bool):
            raise ValueError(f"sort {index} numeric must be a JSON boolean")


def _validate_fields(fields: list[str], available: list[str], label: str) -> None:
    """Refuse query field names that do not exist in the relevant real schema."""

    unknown = sorted(set(fields) - set(available))
    if unknown:
        raise ValueError(f"unknown {label} field(s): {unknown}; available fields: {available}")


def _matches(row: dict[str, str], condition: dict[str, Any]) -> bool:
    """Evaluate exactly one validated filter operation against one normalized row.

    Numeric mode first parses both sides as finite Decimal values.  ``contains`` keeps
    its historical behavior: even in numeric mode, the actual membership test uses the
    original strings after numeric validity has been established.
    """

    field = condition["field"]
    op = condition["op"]
    expected = str(condition["value"])
    actual = row[field]
    if condition.get("numeric", False):
        left, right = _number(actual), _number(expected)
    else:
        left, right = actual, expected

    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    return expected in actual


def _aggregate(rows: list[dict[str, str]], spec: dict[str, Any]) -> Any:
    """Reduce one group to count/sum/min/max/avg using finite Decimal arithmetic.

    ``count`` needs no source field.  Other aggregates parse every source cell as a
    finite Decimal.  An empty group yields ``None`` for non-count operations.  Decimal
    arithmetic failures are translated into the module's ordinary value-error boundary
    so callers do not receive raw Decimal tracebacks.
    """

    op = spec["op"]
    if op == "count":
        return len(rows)
    try:
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
    except DecimalException as exc:
        raise ValueError(f"numeric aggregate {op!r} failed: {exc}") from exc


def _load_csv(path: pathlib.Path, skip_data_rows: int,
              csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
              local_input_byte_limit: int = DEFAULT_LOCAL_INPUT_LIMIT) -> tuple[list[str], list[dict[str, str]]]:
    """Load one bounded UTF-8 CSV as normalized ``{header: text}`` row dictionaries.

    UTF-8 with an optional BOM is accepted.  Header names and cell values have
    surrounding whitespace removed; normalized headers must remain non-empty and
    unique, and every retained row must have exactly the header field count.  Blank CSV
    records therefore fail the same strict row-shape check rather than being silently
    discarded.

    ``skip_data_rows`` discards data records immediately after the header and stops as
    soon as EOF is reached, so a very large skip request cannot spin after the file has
    ended.  The CSV parser's process-global per-field limit is restored in ``finally``
    even when decoding or validation fails.
    """

    previous_field_size_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(csv_field_size_limit)
        with path.open("rb") as binary_stream, io.TextIOWrapper(
                io.BufferedReader(_BoundedBinaryReader(binary_stream, local_input_byte_limit)),
                encoding="utf-8-sig", newline="",
            ) as stream:
            reader = csv.reader(stream)
            raw_headers = next(reader, None)
            if raw_headers is None:
                raise ValueError("CSV has no header row")
            headers = [item.strip() for item in raw_headers]
            if any(not item for item in headers) or len(headers) != len(set(headers)):
                raise ValueError("CSV headers must be non-empty and unique after surrounding whitespace is removed")

            skipped = 0
            for _ in range(skip_data_rows):
                if next(reader, None) is None:
                    break
                skipped += 1

            rows = []
            for line_number, values in enumerate(reader, start=2 + skipped):
                if len(values) != len(headers):
                    raise ValueError(f"CSV row {line_number} has {len(values)} fields; expected {len(headers)}")
                rows.append(dict(zip(headers, (value.strip() for value in values))))
        return headers, rows
    finally:
        csv.field_size_limit(previous_field_size_limit)


def run_query(csv_path: pathlib.Path, query: dict[str, Any],
              csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
              local_input_byte_limit: int = DEFAULT_LOCAL_INPUT_LIMIT) -> tuple[list[str], list[dict[str, Any]]]:
    """Execute the validated query pipeline and return fields plus the bounded result.

    The phases are deliberately linear: validate data-independent query syntax and
    direct-call limits; load and normalize the bounded CSV; validate field names against
    the real header; filter rows; group/aggregate or project selected fields; validate
    the derived output schema; apply stable multi-key sorting; then apply ``limit``.

    The returned rows are the complete *bounded query result after limit*, not the total
    number of source matches.  Local artifact and stdout-window decisions happen later
    in ``main()`` so this function remains focused on query semantics.
    """

    if not isinstance(local_input_byte_limit, int) or isinstance(local_input_byte_limit, bool) or local_input_byte_limit < 1:
        raise ValueError("local_input_byte_limit must be a positive integer")
    if (
        not isinstance(csv_field_size_limit, int)
        or isinstance(csv_field_size_limit, bool)
        or not 1 <= csv_field_size_limit <= sys.maxsize
    ):
        raise ValueError(f"csv_field_size_limit must be an integer from 1 to {sys.maxsize}")

    # Phase 1: reject malformed query structure before any CSV-dependent execution.
    _validate_query_contract(query)
    limit = query["limit"]
    skip_data_rows = query.get("skip_data_rows", 0)

    # Phase 2: materialize the bounded CSV and only then validate real field names.
    headers, rows = _load_csv(csv_path, skip_data_rows, csv_field_size_limit, local_input_byte_limit)
    filters = query.get("filters", [])
    _validate_fields([item["field"] for item in filters], headers, "filter")

    # Phase 3: filtering reduces the row set used by every later transformation.
    rows = [row for row in rows if all(_matches(row, item) for item in filters)]

    # Phase 4: grouping/aggregation and ordinary projection are mutually exclusive
    # result shapes.  When grouping is requested, a simultaneous select is ignored.
    group_by = query.get("group_by", [])
    aggregates = query.get("aggregates", [])
    _validate_fields(group_by, headers, "group")
    _validate_fields([item["field"] for item in aggregates if item["op"] != "count"], headers, "aggregate")

    if group_by or aggregates:
        aggregate_names = [
            spec.get("as") or ("count" if spec["op"] == "count" else f"{spec['op']}_{spec['field']}")
            for spec in aggregates
        ]
        output_fields = list(group_by) + aggregate_names
        if len(output_fields) != len(set(output_fields)):
            raise ValueError(f"output field names must be unique: {output_fields}")

        groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
        if group_by:
            for row in rows:
                groups[tuple(row[field] for field in group_by)].append(row)
        else:
            # Aggregate-only queries intentionally produce one group even when rows is
            # empty, allowing count=0 and null for other empty aggregates.
            groups[()] = rows

        result = []
        for key, group_rows in groups.items():
            item: dict[str, Any] = dict(zip(group_by, key))
            for spec, alias in zip(aggregates, aggregate_names):
                item[alias] = _json_value(_aggregate(group_rows, spec))
            result.append(item)
    else:
        # An explicitly empty select preserves the historical meaning "all headers".
        output_fields = query.get("select") or headers
        _validate_fields(output_fields, headers, "select")
        if len(output_fields) != len(set(output_fields)):
            raise ValueError(f"output field names must be unique: {output_fields}")
        result = [{field: row[field] for field in output_fields} for row in rows]

    # Phase 5: Python sorting is stable, so apply the least-significant key first by
    # walking the user's sort specification in reverse order.
    for sort_spec in reversed(query.get("sort", [])):
        field = sort_spec["field"]
        _validate_fields([field], output_fields, "sort")
        numeric = sort_spec.get("numeric", False)
        result.sort(
            key=lambda row: _number(str(row[field])) if numeric else str(row[field]),
            reverse=sort_spec.get("direction", "asc") == "desc",
        )

    # Phase 6: query limit is last.  The caller cannot infer total matches from a result
    # whose length equals this limit because extra matches may have been discarded here.
    return output_fields, result[:limit]


def _write_result_artifact(csv_path: pathlib.Path, query: dict[str, Any],
                           output_dir: pathlib.Path, force: bool,
                           lineage: Any, fields: list[str],
                           rows: list[dict[str, Any]]) -> str:
    """Publish the complete bounded query result as one deterministic local JSON file.

    The filename is derived from the resolved CSV path plus the query, preserving the
    existing naming contract.  Existing targets are refused unless ``force`` is true.
    Content is written to a same-directory temporary file, flushed, and fsynced before
    ``os.replace`` publishes it; this avoids exposing a partially written destination
    during ordinary publication.  The no-clobber existence check is not race-proof and
    the directory is not fsynced, so callers must not interpret this as full crash
    durability or an atomic compare-and-swap no-clobber guarantee.
    """

    query_json = json.dumps(query, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256((str(csv_path) + "\n" + query_json).encode()).hexdigest()[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    target = (output_dir / f"query-{digest}.json").resolve()
    if target.exists() and not force:
        raise FileExistsError(f"output already exists: {target}; use --force to replace it")

    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"lineage": lineage, "fields": fields, "rows": rows},
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
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
    return str(target)


def main() -> int:
    """Parse the CLI, run one query, and emit bounded JSON or a clean failure.

    CLI orchestration owns concerns outside query semantics: early filesystem-size
    refusal, acquisition-lineage loading, optional row-overflow artifact creation,
    stdout row-window construction, the separate serialized-byte ceiling, and the final
    user-facing error boundary.  Byte-limit fallback removes only ``result.rows`` and
    keeps the metadata describing the normal row-window response.
    """

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=pathlib.Path)
    parser.add_argument("--query", required=True, help="JSON query object")
    parser.add_argument("--stdout-rows", type=int, default=10)
    parser.add_argument(
        "--stdout-bytes-limit", type=int, default=DEFAULT_STDOUT_BYTES_LIMIT,
        help=f"maximum serialized stdout bytes (minimum/default: {DEFAULT_STDOUT_BYTES_LIMIT}); increase deliberately for larger output",
    )
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
        # CLI limits protect distinct output/input surfaces; validate them before query
        # parsing or filesystem work so invalid invocations fail deterministically.
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"stdout-bytes-limit must be at least {DEFAULT_STDOUT_BYTES_LIMIT}")
        if args.stdout_rows < 1:
            raise ValueError("stdout-rows must be positive")
        if args.local_input_byte_limit < 1 or args.csv_field_size_limit < 1:
            raise ValueError("local input and CSV field size limits must be positive")

        query = json.loads(args.query)
        if not isinstance(query, dict):
            raise ValueError("query must be a JSON object")

        # The stat check is a cheap early refusal.  _BoundedBinaryReader later enforces
        # the same byte ceiling during the actual read in case the file has changed.
        csv_path = args.csv.resolve(strict=True)
        input_size = csv_path.stat().st_size
        if input_size > args.local_input_byte_limit:
            raise ValueError(
                f"local CSV input size {input_size} exceeds the {args.local_input_byte_limit}-byte limit; "
                "use --local-input-byte-limit with an expected larger size"
            )

        # Lineage is required acquisition metadata carried forward verbatim.  This tool
        # does not independently prove that the sidecar describes the current CSV.
        sidecar_path = csv_path.with_name(csv_path.name + ".acquisition.json")
        lineage = json.loads(sidecar_path.read_text(encoding="utf-8"))

        fields, rows = run_query(
            csv_path, query, args.csv_field_size_limit, args.local_input_byte_limit,
        )

        # Artifacts preserve the entire post-limit query result only when the stdout row
        # window truncates it and the caller explicitly supplied an output directory.
        # Byte-limit omission by itself does not trigger artifact creation.
        artifact_path = None
        if len(rows) > args.stdout_rows and args.output_dir is not None:
            artifact_path = _write_result_artifact(
                csv_path, query, args.output_dir, args.force, lineage, fields, rows,
            )

        # First apply the ordinary row window.  Counts describe the post-query-limit
        # result and that normal window; they are not a total-match count.
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
        serialized = (
            json.dumps(response, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")

        # The serialized-byte ceiling is a second, independent stdout bound.  If it
        # fires, keep the normal result metadata but omit rows in a compact envelope.
        if len(serialized) > args.stdout_bytes_limit:
            envelope = {
                "schema_version": "hda-output-limit-envelope/v1",
                "source_schema_version": response["schema_version"],
                "lineage": lineage,
                "query": query,
                "result": {key: value for key, value in response["result"].items() if key != "rows"},
                "stdout_bytes_limit": args.stdout_bytes_limit,
                "normal_serialized_bytes": len(serialized),
                "output_limit_exceeded": True,
                "data_omitted": True,
                "reason": "Serialized response exceeds stdout byte limit; result.rows omitted.",
            }
            serialized = (
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
            ).encode("utf-8")
            if len(serialized) > args.stdout_bytes_limit:
                raise ValueError("output-limit envelope exceeds stdout byte limit")

        sys.stdout.buffer.write(serialized)
        return 0
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, csv.Error, json.JSONDecodeError) as exc:
        parser.exit(1, f"HDA query failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
