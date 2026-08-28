#!/usr/bin/env python3
"""Acquire an HDX resource unchanged, or inspect a CSV without retaining it."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import pathlib
import tempfile
import urllib.request
from datetime import datetime, timezone
from typing import Any, BinaryIO

from hda_http import ResponseTooLarge, require_http_url


CATALOG_NAME = "Humanitarian Data Exchange (HDX)"
CATALOG_API = "https://data.humdata.org/api/3/action/package_search"
DEFAULT_INSPECTION_LIMIT = 1_000_000
DEFAULT_SAMPLE_ROWS = 3
DEFAULT_ACQUISITION_LIMIT = 64 * 1024 * 1024
DEFAULT_LOCAL_INSPECTION_LIMIT = 64 * 1024 * 1024
DEFAULT_CSV_FIELD_SIZE_LIMIT = 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_csv(data: bytes, sample_rows: int, complete: bool,
                csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT) -> dict[str, Any]:
    encoding = "utf-8-sig"
    diagnostics: list[str] = []
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        return {
            "declared_format": "CSV",
            "observed_format": None,
            "encoding": None,
            "parse_diagnostics": [f"UTF-8 decode failed at byte {exc.start}: {exc.reason}"],
            "sample": {"identified_as": "sample", "method": "none", "size_rows": 0, "rows": []},
        }

    stream = io.StringIO(text, newline="")
    try:
        csv.field_size_limit(csv_field_size_limit)
        reader = csv.reader(stream)
        headers = next(reader, None)
        sample = []
        row_count = 0
        for row in reader:
            row_count += 1
            if len(sample) < sample_rows:
                sample.append(row)
    except csv.Error as exc:
        if "field larger than field limit" in str(exc):
            raise ValueError(
                f"CSV field exceeds the {csv_field_size_limit}-character parser limit; "
                "use --csv-field-size-limit with an expected larger field"
            ) from exc
        headers, sample, row_count = None, [], 0
        diagnostics.append(f"CSV parse failed: {exc}")
    result: dict[str, Any] = {
        "declared_format": "CSV",
        "observed_format": "CSV" if headers is not None else None,
        "encoding": encoding,
        "headers_exactly_as_found": headers,
        "parse_diagnostics": diagnostics,
        "sample": {
            "identified_as": "sample",
            "method": "first data rows after the first parsed record used as headers",
            "size_rows": len(sample),
            "rows": sample,
        },
    }
    if complete and not diagnostics:
        result["row_count_excluding_header"] = row_count
    else:
        result["row_count_excluding_header"] = None
        result["row_count_diagnostic"] = "not reported because retrieval was bounded" if not complete else "not available after parse failure"
    return result


def inspect_stream(stream: BinaryIO, declared_format: str, byte_limit: int | None, sample_rows: int,
                   csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT) -> dict[str, Any]:
    if declared_format.upper() != "CSV":
        raise ValueError("only CSV is supported")
    if byte_limit is None:
        data = stream.read()
        complete = True
    else:
        data = stream.read(byte_limit + 1)
        complete = len(data) <= byte_limit
        data = data[:byte_limit]
    result = _decode_csv(data, sample_rows, complete, csv_field_size_limit)
    result["bytes_examined"] = len(data)
    result["retrieval_complete"] = complete
    return result


def disposable_inspect(url: str, declared_format: str, byte_limit: int, sample_rows: int, timeout: float,
                       csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT) -> dict[str, Any]:
    request = urllib.request.Request(require_http_url(url), headers={"User-Agent": "humanitarian-data-access/inspect"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = inspect_stream(response, declared_format, byte_limit, sample_rows, csv_field_size_limit)
        content_length = response.headers.get("Content-Length")
    result.update({
        "mode": "disposable_inspection",
        "durable": False,
        "source_url": url,
        "declared_size_bytes": int(content_length) if content_length and content_length.isdigit() else None,
        "inspection_byte_limit": byte_limit,
        "local_path": None,
    })
    return result


def acquire(url: str, dataset_id: str, resource_id: str, destination: pathlib.Path, timeout: float,
            byte_limit: int = DEFAULT_ACQUISITION_LIMIT, force: bool = False) -> dict[str, Any]:
    require_http_url(url)
    if byte_limit < 1:
        raise ValueError("durable acquisition byte limit must be positive")
    metadata_path = destination.with_name(destination.name + ".acquisition.json")
    existing = [str(path) for path in (destination, metadata_path) if path.exists()]
    if existing and not force:
        raise FileExistsError(f"output already exists: {', '.join(existing)}; use --force to replace the output pair")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(url, headers={"User-Agent": "humanitarian-data-access/acquire"})
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    metadata_temporary_name: str | None = None
    published_new_destination = False
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > byte_limit:
                raise ResponseTooLarge(
                    f"durable acquisition exceeds the {byte_limit}-byte limit "
                    f"(Content-Length: {content_length}); use --byte-limit with an expected larger size"
                )
            while chunk := response.read(min(DOWNLOAD_CHUNK_SIZE, byte_limit - size + 1)):
                if size + len(chunk) > byte_limit:
                    raise ResponseTooLarge(
                        f"durable acquisition exceeds the {byte_limit}-byte limit; "
                        "use --byte-limit with an expected larger size"
                    )
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        metadata = {
        "schema_version": "hdx-acquisition/v1",
        "catalog": {"name": CATALOG_NAME, "api_endpoint": CATALOG_API},
        "native_dataset_id": dataset_id,
        "native_resource_id": resource_id,
        "source_url": url,
        "retrieved_at_utc": _timestamp(),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "local_path": str(destination.resolve()),
        "catalog_record": {
            "dataset_id": dataset_id,
            "reference": f"https://data.humdata.org/dataset/{dataset_id}",
        },
        "raw_preservation": "response bytes stored unchanged",
        }
        metadata_fd, metadata_temporary_name = tempfile.mkstemp(prefix=metadata_path.name + ".", dir=destination.parent)
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = ""
        published_new_destination = not existing
        os.replace(metadata_temporary_name, metadata_path)
        metadata_temporary_name = None
    except BaseException:
        if published_new_destination:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for temporary in (temporary_name, metadata_temporary_name):
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        raise
    return {"mode": "durable_acquisition", "durable": True, "metadata_path": str(metadata_path.resolve()), **metadata}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    disposable = subparsers.add_parser("inspect-url")
    disposable.add_argument("url")
    disposable.add_argument("--format", required=True)
    disposable.add_argument("--byte-limit", type=int, default=DEFAULT_INSPECTION_LIMIT)
    durable = subparsers.add_parser("acquire")
    durable.add_argument("url")
    durable.add_argument("--dataset-id", required=True)
    durable.add_argument("--resource-id", required=True)
    durable.add_argument("--output", type=pathlib.Path, required=True)
    durable.add_argument("--force", action="store_true", help="deliberately replace an existing output and acquisition sidecar")
    durable.add_argument(
        "--byte-limit",
        type=int,
        default=DEFAULT_ACQUISITION_LIMIT,
        help=f"maximum downloaded bytes before refusal (default: {DEFAULT_ACQUISITION_LIMIT}); increase deliberately for an expected large resource",
    )
    local = subparsers.add_parser("inspect-file")
    local.add_argument("path", type=pathlib.Path)
    local.add_argument("--format", required=True)
    local.add_argument(
        "--local-byte-limit", type=int, default=DEFAULT_LOCAL_INSPECTION_LIMIT,
        help=f"maximum local input bytes (default: {DEFAULT_LOCAL_INSPECTION_LIMIT}); increase deliberately for an expected large file",
    )
    for item in (disposable, local):
        item.add_argument("--sample-rows", type=int, default=DEFAULT_SAMPLE_ROWS)
        item.add_argument(
            "--csv-field-size-limit", type=int, default=DEFAULT_CSV_FIELD_SIZE_LIMIT,
            help=f"maximum parsed CSV field characters (default: {DEFAULT_CSV_FIELD_SIZE_LIMIT}); increase deliberately for expected wide fields",
        )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if (getattr(args, "sample_rows", 1) < 1 or getattr(args, "byte_limit", 1) < 1
            or getattr(args, "local_byte_limit", 1) < 1
            or getattr(args, "csv_field_size_limit", 1) < 1 or args.timeout <= 0):
        parser.error("limits, sample rows, and timeout must be positive")
    try:
        if args.command == "inspect-url":
            result = disposable_inspect(
                args.url, args.format, args.byte_limit, args.sample_rows, args.timeout,
                args.csv_field_size_limit,
            )
        elif args.command == "acquire":
            result = acquire(args.url, args.dataset_id, args.resource_id, args.output, args.timeout, args.byte_limit, args.force)
        else:
            size = args.path.stat().st_size
            if size > args.local_byte_limit:
                raise ValueError(
                    f"local inspection input size {size} exceeds the {args.local_byte_limit}-byte limit; "
                    "use --local-byte-limit with an expected larger size"
                )
            with args.path.open("rb") as stream:
                inspection = inspect_stream(stream, args.format, size, args.sample_rows, args.csv_field_size_limit)
            if not inspection["retrieval_complete"]:
                raise ValueError(
                    f"local inspection input grew beyond the {args.local_byte_limit}-byte limit while being read; "
                    "use --local-byte-limit with an expected larger size"
                )
            result = {"mode": "mechanical_inspection", "durable": True, "local_path": str(args.path.resolve()), "size_bytes": size, **inspection}
    except Exception as exc:
        parser.exit(1, f"HDX operation failed: {exc}\n")
    json.dump(result, __import__("sys").stdout, ensure_ascii=False, sort_keys=True, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
