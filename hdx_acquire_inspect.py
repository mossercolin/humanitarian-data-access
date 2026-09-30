#!/usr/bin/env python3
"""Inspect or retain bounded HDX resource bytes with explicit local lineage.

This module has three deliberately different modes in one resource-handling
lifecycle:

* ``inspect-url`` reads only a caller-bounded prefix of a remote CSV and keeps no
  local copy. It is for quick mechanical inspection, not durable acquisition.
* ``inspect-file`` applies the same CSV inspection rules to an already-local
  artifact. The returned ``durable`` flag means the inspected bytes already live
  in a retained local file; it is not a crash- or power-loss guarantee.
* ``acquire`` downloads a complete response body only while it stays inside the
  caller's byte ceiling, hashes the exact body chunks HDA writes, prepares an
  acquisition sidecar, and then publishes the data file and sidecar as a logical
  pair.

CSV is the only inspection format in this module by design. Text is interpreted
as UTF-8 with an optional BOM (``utf-8-sig``); HDA reports invalid encoding
rather than guessing another one. A bounded remote prefix can end inside a valid
multi-byte UTF-8 character or CSV record. The decoder therefore treats only an
incomplete trailing character caused by a partial retrieval as unfinished input,
while genuinely invalid bytes still fail. The final parsed CSV record of a
partial prefix may likewise be only a fragment.

The limits protect different resources and are intentionally separate:

* inspection byte limit: remote bytes held for disposable inspection;
* local inspection byte limit: unexpectedly large local materialization;
* acquisition byte limit: disk/network growth during a retained download;
* CSV field-size limit: unusually wide parser fields;
* sample-row limit: model-facing example rows retained in the result;
* stdout byte limit: serialized inspection output exposed to a caller/model.

``retrieval_complete`` means only that the inspector observed end-of-file within
its byte ceiling. It does not establish publisher, catalog, humanitarian, or
dataset completeness. The first parsed CSV record is mechanically treated as
headers, samples are the following parsed records, and a row count is claimed
only when the inspected body was complete.

Acquisition prepares both new candidate files in the destination directory and
fsyncs each candidate's file contents before publication. Default publication
refuses a pre-existing data file or sidecar; ``--force`` first moves whichever
old members exist to temporary backups. New data is published before the new
sidecar. If a handled publication failure occurs, HDA attempts to restore the
pre-call membership state. A failed restore is reported as incomplete rollback,
its unrestored backup is retained, and the final pair state is described as
uncertain. Once both new members are published, failure to delete superseded old
backups does not undo success; their paths are returned only in
``superseded_backups_retained`` and are not written into the historical sidecar.

These mechanics are not pair-atomic, crash-atomic, power-loss transactional, or
cross-process serialized. Parent directory entries are not fsynced and no lock
manager is used, so crashes or concurrent writers can still leave intermediate
states. Normal CLI argument-policy errors use argparse's error path; operational
failures use ``HDX operation failed`` on stderr. Inspection stdout is bounded by
an omission envelope when necessary. Acquisition stdout has no separate byte
ceiling: its complete result is serialized first, UTF-8 encoded, then written to
``stdout.buffer``.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import math
import os
import pathlib
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, BinaryIO, Mapping

from hda_http import (
    ResponseTooLarge,
    open_http,
    read_http_chunk,
    require_complete_length,
    require_http_url,
)


CATALOG_NAME = "Humanitarian Data Exchange (HDX)"
CATALOG_API = "https://data.humdata.org/api/3/action/package_search"

# Remote disposable inspection holds at most this many source bytes for parsing;
# the extra byte used internally exists only to answer whether the body exceeded
# the configured prefix.
DEFAULT_INSPECTION_LIMIT = 1_000_000

# Samples remain deliberately small even when a complete CSV contains many rows.
DEFAULT_SAMPLE_ROWS = 3

# Inspection results can contain huge headers/fields, so model-facing stdout has
# an independent serialization ceiling and falls back to an omission envelope.
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024

# Retained downloads stop before unexpectedly consuming unbounded disk/network.
DEFAULT_ACQUISITION_LIMIT = 64 * 1024 * 1024

# Local inspection is also bounded because it materializes bytes for CSV parsing.
DEFAULT_LOCAL_INSPECTION_LIMIT = 64 * 1024 * 1024

# Python's CSV parser uses a process-global field ceiling; HDA installs this only
# for one parse and restores the caller's previous value afterward.
DEFAULT_CSV_FIELD_SIZE_LIMIT = 1024 * 1024

# Acquisition hashes and writes incrementally rather than holding the whole body.
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def _timestamp() -> str:
    """Return HDA's local UTC acquisition timestamp, not publisher metadata."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_positive(name: str, value: int) -> None:
    """Reject a non-positive bound before it can create unbounded/nonsensical work."""
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _require_network_timeout(timeout: float) -> None:
    """Require a finite positive per-operation network timeout."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("network timeout must be finite and positive")


def _validate_inspection_request(
    declared_format: str,
    byte_limit: int,
    sample_rows: int,
    csv_field_size_limit: int,
) -> None:
    """Enforce the direct-call inspection contract before bytes are consumed."""
    if declared_format.upper() != "CSV":
        raise ValueError("only CSV is supported")
    _require_positive("inspection byte limit", byte_limit)
    _require_positive("CSV field-size limit", csv_field_size_limit)
    if sample_rows < 0:
        raise ValueError("sample rows must not be negative")


def _declared_content_length(headers: Mapping[str, str] | None) -> int | None:
    """Interpret Content-Length like  usable non-negative integer or unknown.

    The declaration is only an advisory early/completion signal. Actual counted
    response bytes remain the authoritative size boundary.
    """
    if headers is None:
        return None
    value = headers.get("Content-Length")
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _path_entry_exists(path: pathlib.Path) -> bool:
    """Treat any directory entry, including a dangling symlink, as pre-existing."""
    return os.path.lexists(os.fspath(path))


def _absolute_path_without_following(path: pathlib.Path) -> str:
    """Record the publication pathname itself without resolving an old symlink."""
    return os.path.abspath(os.fspath(path))


def _decode_csv(
    data: bytes,
    sample_rows: int,
    complete: bool,
    csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
) -> dict[str, Any]:
    """Mechanically parse a bounded UTF-8-SIG CSV view.

    WHAT: decode the inspected bytes, treat the first parsed record as headers,
    keep only the requested sample rows, and count remaining records when the
    inspected body is complete.
    WHY: HDA needs a small mechanical preview without claiming schema validity or
    interpreting humanitarian meaning.
    HOW: an incremental decoder receives ``final=False`` for partial retrievals,
    so only a trailing incomplete UTF-8 character created by HDA's byte boundary
    is buffered instead of falsely labeled corruption. Invalid bytes earlier in
    the prefix still raise. The process-global CSV field limit is restored after
    every parse attempt.

    ``observed_format == "CSV"`` means only that the configured CSV reader
    produced a first record. It is not proof that arbitrary source content is a
    semantically valid table. For bounded retrievals, the terminal parsed record
    can be a fragment and no total row count is claimed.
    """
    encoding = "utf-8-sig"
    diagnostics: list[str] = []
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        text = decoder.decode(data, final=complete)
    except UnicodeDecodeError as exc:
        return {
            "declared_format": "CSV",
            "observed_format": None,
            "encoding": None,
            "headers_exactly_as_found": None,
            "parse_diagnostics": [f"UTF-8 decode failed at byte {exc.start}: {exc.reason}"],
            "sample": {"identified_as": "sample", "method": "none", "size_rows": 0, "rows": []},
            "row_count_excluding_header": None,
            "row_count_diagnostic": "not available after UTF-8 decode failure",
        }

    stream = io.StringIO(text, newline="")
    previous_field_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(csv_field_size_limit)
        try:
            reader = csv.reader(stream)
            headers = next(reader, None)
            sample: list[list[str]] = []
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
    finally:
        csv.field_size_limit(previous_field_limit)

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
        result["row_count_diagnostic"] = (
            "not reported because retrieval was bounded"
            if not complete
            else "not available after parse failure"
        )
    return result


def inspect_stream(
    stream: BinaryIO,
    declared_format: str,
    byte_limit: int,
    sample_rows: int,
    csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
) -> dict[str, Any]:
    """Inspect no more than ``byte_limit`` bytes plus one detection byte.

    The extra byte answers only whether the stream crossed the caller's ceiling.
    ``retrieval_complete`` is true when this bounded read observed EOF; it says
    nothing about catalog/source completeness. Direct callers may request zero
    sample rows, but every byte/field limit remains positive and bounded.
    """
    _validate_inspection_request(declared_format, byte_limit, sample_rows, csv_field_size_limit)
    data = stream.read(byte_limit + 1)
    complete = len(data) <= byte_limit
    data = data[:byte_limit]
    result = _decode_csv(data, sample_rows, complete, csv_field_size_limit)
    result["bytes_examined"] = len(data)
    result["retrieval_complete"] = complete
    return result


def disposable_inspect(
    url: str,
    declared_format: str,
    byte_limit: int,
    sample_rows: int,
    timeout: float,
    csv_field_size_limit: int = DEFAULT_CSV_FIELD_SIZE_LIMIT,
) -> dict[str, Any]:
    """Inspect one bounded remote CSV prefix without retaining a local artifact.

    Validation happens before HTTP I/O. The response body is read only through
    the requested prefix plus one detection byte. When EOF is observed inside
    that boundary, the implementation's completion check also refuses a body shorter than a
    usable Content-Length declaration.
    """
    _validate_inspection_request(declared_format, byte_limit, sample_rows, csv_field_size_limit)
    _require_network_timeout(timeout)
    validated_url = require_http_url(url)
    request = urllib.request.Request(
        validated_url,
        headers={"User-Agent": "humanitarian-data-access/inspect"},
    )
    with open_http(request, timeout=timeout) as response:
        received = read_http_chunk(response, byte_limit + 1, "disposable inspection")
        result = inspect_stream(
            io.BytesIO(received),
            declared_format,
            byte_limit,
            sample_rows,
            csv_field_size_limit,
        )
        declared_size = _declared_content_length(response.headers)
        if result["retrieval_complete"]:
            require_complete_length(result["bytes_examined"], response.headers, "disposable inspection")
        final_response_url = response.geturl()
    result.update(
        {
            "mode": "disposable_inspection",
            "durable": False,
            "source_url": url,
            "final_response_url": final_response_url,
            "declared_size_bytes": declared_size,
            "inspection_byte_limit": byte_limit,
            "local_path": None,
        }
    )
    return result


def _inspect_local_file(
    path: pathlib.Path,
    declared_format: str,
    local_byte_limit: int,
    sample_rows: int,
    csv_field_size_limit: int,
) -> dict[str, Any]:
    """Inspect bytes from the opened local file, not a stale pathname stat.

    WHAT: open the file, inspect the opened descriptor, and report bytes actually
    examined.
    WHY: a pathname can shrink or grow between a separate stat and read; using
    the old size as the parser limit produced false "beyond limit" errors and
    stale size metadata.
    HOW: fstat() provides a cheap early refusal for an obviously oversized opened
    file, while the configured local ceiling remains authoritative during the
    read. This is not a snapshot: another actor can still mutate/replace storage
    concurrently.
    """
    _require_positive("local inspection byte limit", local_byte_limit)
    with path.open("rb") as stream:
        opened_size = os.fstat(stream.fileno()).st_size
        if opened_size > local_byte_limit:
            raise ValueError(
                f"local inspection input size {opened_size} exceeds the {local_byte_limit}-byte limit; "
                "use --local-byte-limit with an expected larger size"
            )
        inspection = inspect_stream(
            stream,
            declared_format,
            local_byte_limit,
            sample_rows,
            csv_field_size_limit,
        )
    if not inspection["retrieval_complete"]:
        raise ValueError(
            f"local inspection input grew beyond the {local_byte_limit}-byte limit while being read; "
            "use --local-byte-limit with an expected larger size"
        )
    return {
        "mode": "mechanical_inspection",
        "durable": True,
        "local_path": _absolute_path_without_following(path),
        "size_bytes": inspection["bytes_examined"],
        **inspection,
    }


def _download_to_temporary(
    url: str,
    destination: pathlib.Path,
    timeout: float,
    byte_limit: int,
) -> tuple[str, int, str, str]:
    """Stream one bounded response into a complete fsynced data candidate.

    WHAT: download into a same-directory temporary file while counting and
    hashing every chunk.
    WHY: acquisition must refuse unexpected growth without holding the whole body
    in memory, and publication must never expose an incomplete candidate.
    HOW: a usable Content-Length can refuse an obviously oversized response
    early, but live limit+1 accounting remains authoritative. SHA-256 is updated
    from the exact chunks written. EOF is checked against declared length, then
    the candidate file contents are flushed and fsynced before this helper
    returns. A failure before return removes its temporary candidate.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "humanitarian-data-access/acquire"},
    )
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as output, open_http(request, timeout=timeout) as response:
            declared_size = _declared_content_length(response.headers)
            if declared_size is not None and declared_size > byte_limit:
                raise ResponseTooLarge(
                    f"durable acquisition exceeds the {byte_limit}-byte limit "
                    f"(Content-Length: {declared_size}); use --byte-limit with an expected larger size"
                )
            while chunk := read_http_chunk(
                response,
                min(DOWNLOAD_CHUNK_SIZE, byte_limit - size + 1),
                "durable acquisition",
            ):
                if size + len(chunk) > byte_limit:
                    raise ResponseTooLarge(
                        f"durable acquisition exceeds the {byte_limit}-byte limit; "
                        "use --byte-limit with an expected larger size"
                    )
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            require_complete_length(size, response.headers, "durable acquisition")
            final_response_url = response.geturl()
            output.flush()
            os.fsync(output.fileno())
        return temporary_name, size, digest.hexdigest(), final_response_url
    except BaseException as exc:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            raise RuntimeError(
                f"durable acquisition failed and temporary data cleanup failed: {temporary_name}: {cleanup_exc}"
            ) from exc
        raise


def _publish_pair_with_rollback(
    data_temporary_name: str,
    metadata_temporary_name: str,
    destination: pathlib.Path,
    metadata_path: pathlib.Path,
    force: bool,
) -> list[str]:
    """Publish prepared data+sidecar candidates and recover handled failures.

    WHAT: own the complete filesystem state transition for the logical pair.
    WHY: backup, publication, rollback, and cleanup must share one audit-visible
    state model; scattering them across ``acquire`` makes it easy to lose track
    of which old/new member exists.
    HOW: without ``force`` a second no-clobber check occurs immediately before
    publication. With ``force``, whichever old pair members exist at that moment
    are moved to same-directory backups. New data is published first, then the
    sidecar. On a handled failure, each member's independent recovery is attempted.
    A backup whose restore fails is never deleted; an incomplete rollback raises
    an explicit uncertain-state error naming retained backup paths.

    Once both new members are present, old backup deletion is post-publication
    cleanup only. Every deletion is attempted; failures return retained backup
    paths instead of turning the valid new pair into an acquisition failure.

    This is best-effort handled-failure recovery, not a filesystem transaction:
    crashes, power loss, or concurrent writers can still expose intermediate
    states, and parent directory entries are not fsynced.
    """
    data_temp = data_temporary_name
    metadata_temp = metadata_temporary_name
    backups: dict[pathlib.Path, str] = {}
    backup_names: list[str] = []
    published: set[pathlib.Path] = set()

    try:
        # Both candidate contents are already complete and fsynced. Refusing a
        # newly appeared member here narrows the no-clobber race but does not
        # serialize us against another writer after this check.
        if not force:
            existing = [
                str(path)
                for path in (destination, metadata_path)
                if _path_entry_exists(path)
            ]
            if existing:
                raise FileExistsError(
                    f"output appeared before publication: {', '.join(existing)}; "
                    "use --force to replace the output pair"
                )

        # --force preserves the exact old membership that exists immediately
        # before publication. Data-only and sidecar-only old states are valid
        # rollback inputs; HDA does not assume an old complete pair.
        if force:
            for path in (destination, metadata_path):
                if _path_entry_exists(path):
                    backup_fd, backup_name = tempfile.mkstemp(
                        prefix=path.name + ".backup.",
                        dir=destination.parent,
                    )
                    os.close(backup_fd)
                    backup_names.append(backup_name)
                    os.replace(path, backup_name)
                    backups[path] = backup_name

        # Publication order is deliberate: data first, sidecar second. A failure
        # after the first replace is handled below using the old backup state.
        os.replace(data_temp, destination)
        published.add(destination)
        data_temp = ""
        os.replace(metadata_temp, metadata_path)
        published.add(metadata_path)
        metadata_temp = ""

    except BaseException as publication_error:
        rollback_errors: list[str] = []
        cleanup_errors: list[str] = []
        retained_backups: list[str] = []

        # Reverse logical publication order. A restored backup may overwrite a
        # newly published member. When no old member existed, removing the new
        # member restores the previous absence instead.
        for path in (metadata_path, destination):
            backup_name = backups.get(path)
            if backup_name is not None:
                try:
                    os.replace(backup_name, path)
                except BaseException as exc:
                    rollback_errors.append(f"restore {path} from {backup_name}: {exc}")
                    if os.path.lexists(backup_name):
                        retained_backups.append(_absolute_path_without_following(pathlib.Path(backup_name)))
            elif path in published:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except BaseException as exc:
                    rollback_errors.append(f"remove newly published {path}: {exc}")

        # Candidate files and empty backup placeholders are debris, but an old
        # backup whose restoration failed is the only preserved old copy and is
        # therefore protected from generic cleanup.
        protected = set(retained_backups)
        for temporary in (data_temp, metadata_temp, *backup_names):
            if not temporary:
                continue
            absolute = _absolute_path_without_following(pathlib.Path(temporary))
            if absolute in protected:
                continue
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except BaseException as exc:
                cleanup_errors.append(f"remove temporary {temporary}: {exc}")

        if rollback_errors:
            retained_text = ", ".join(retained_backups) if retained_backups else "none"
            details = "; ".join(rollback_errors)
            if cleanup_errors:
                details += "; cleanup errors: " + "; ".join(cleanup_errors)
            raise RuntimeError(
                "publication failed; rollback incomplete; final pair state may be uncertain; "
                f"retained backup paths: {retained_text}; {details}"
            ) from publication_error
        if cleanup_errors:
            raise RuntimeError(
                "publication failed; previous pair recovery completed, but temporary cleanup was incomplete: "
                + "; ".join(cleanup_errors)
            ) from publication_error
        raise

    # Both new members now exist. Old backup cleanup is deliberately outside the
    # failure/rollback state transition: inability to delete superseded copies
    # cannot convert an already-valid new pair into acquisition failure.
    retained_superseded: list[str] = []
    for backup_name in backup_names:
        try:
            os.unlink(backup_name)
        except FileNotFoundError:
            pass
        except OSError:
            if os.path.lexists(backup_name):
                retained_superseded.append(
                    _absolute_path_without_following(pathlib.Path(backup_name))
                )
    return retained_superseded


def acquire(
    url: str,
    dataset_id: str,
    resource_id: str,
    destination: pathlib.Path,
    timeout: float,
    byte_limit: int = DEFAULT_ACQUISITION_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """Acquire a complete bounded HTTP body and publish data plus lineage sidecar.

    The caller supplies the source URL and HDX dataset/resource identifiers. HDA
    validates the network address and records those IDs but does not independently
    prove that the downloaded response belongs to them. The catalog reference is
    mechanically constructed from the supplied dataset ID.

    The data candidate is streamed, hashed, and fsynced first. The sidecar is then
    written and fsynced as a second candidate. Only after both are complete does
    pair publication begin. ``superseded_backups_retained`` describes cleanup of
    old backups after successful publication and intentionally does not enter the
    acquisition sidecar itself.
    """
    validated_url = require_http_url(url)
    _require_positive("durable acquisition byte limit", byte_limit)
    _require_network_timeout(timeout)
    if not dataset_id:
        raise ValueError("dataset ID must not be empty")
    if not resource_id:
        raise ValueError("resource ID must not be empty")

    metadata_path = destination.with_name(destination.name + ".acquisition.json")
    existing = [
        str(path)
        for path in (destination, metadata_path)
        if _path_entry_exists(path)
    ]
    if existing and not force:
        raise FileExistsError(
            f"output already exists: {', '.join(existing)}; use --force to replace the output pair"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    data_temporary_name, size, sha256, final_response_url = _download_to_temporary(
        validated_url,
        destination,
        timeout,
        byte_limit,
    )

    # The sidecar separates caller claims, HTTP observations, and HDA-local facts.
    # The historical field name raw_preservation is retained for compatibility,
    # but its value deliberately claims only response-body bytes delivered by the
    # HTTP client, not wire framing, headers, publisher identity, or authority.
    metadata = {
        "schema_version": "hdx-acquisition/v1",
        "catalog": {"name": CATALOG_NAME, "api_endpoint": CATALOG_API},
        "native_dataset_id": dataset_id,
        "native_resource_id": resource_id,
        "source_url": url,
        "final_response_url": final_response_url,
        "retrieved_at_utc": _timestamp(),
        "size_bytes": size,
        "sha256": sha256,
        "local_path": _absolute_path_without_following(destination),
        "catalog_record": {
            "dataset_id": dataset_id,
            "reference": (
                "https://data.humdata.org/dataset/"
                + urllib.parse.quote(dataset_id, safe="")
            ),
        },
        "raw_preservation": (
            "HTTP response-body bytes delivered by the HTTP client, "
            "written without HDA decoding or normalization"
        ),
    }

    metadata_temporary_name: str | None = None
    try:
        metadata_fd, metadata_temporary_name = tempfile.mkstemp(
            prefix=metadata_path.name + ".",
            dir=destination.parent,
        )
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as exc:
        cleanup_errors: list[str] = []
        for temporary in (data_temporary_name, metadata_temporary_name):
            if not temporary:
                continue
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{temporary}: {cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(
                "acquisition sidecar preparation failed and candidate cleanup was incomplete: "
                + "; ".join(cleanup_errors)
            ) from exc
        raise

    retained_superseded = _publish_pair_with_rollback(
        data_temporary_name,
        metadata_temporary_name,
        destination,
        metadata_path,
        force,
    )
    return {
        "mode": "durable_acquisition",
        "durable": True,
        "metadata_path": _absolute_path_without_following(metadata_path),
        "superseded_backups_retained": retained_superseded,
        **metadata,
    }


def _inspection_stdout_payload(result: dict[str, Any], stdout_bytes_limit: int) -> bytes:
    """Serialize inspection output, omitting bulky rows/headers only when needed.

    The source-byte limits protect input. This independent ceiling protects the
    serialized inspection result exposed on stdout. If normal JSON is too large,
    HDA emits metadata plus a clear omission envelope rather than truncating JSON.
    """
    payload = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) <= stdout_bytes_limit:
        return payload

    envelope = {
        key: value
        for key, value in result.items()
        if key not in ("headers_exactly_as_found", "sample")
    }
    if "sample" in result:
        envelope["sample"] = {
            key: value
            for key, value in result["sample"].items()
            if key in ("identified_as", "method", "size_rows")
        }
    envelope.update(
        {
            "schema_version": "hda-output-limit-envelope/v1",
            "stdout_bytes_limit": stdout_bytes_limit,
            "normal_serialized_bytes": len(payload),
            "output_limit_exceeded": True,
            "data_omitted": True,
            "reason": (
                "Serialized inspection exceeds stdout byte limit; headers and sample rows omitted. "
                "Increase --stdout-bytes-limit deliberately for full output."
            ),
        }
    )
    payload = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > stdout_bytes_limit:
        raise ValueError("inspection output-limit envelope exceeds --stdout-bytes-limit")
    return payload


def main() -> int:
    """Parse CLI policy, dispatch one mode, then emit deterministic JSON output."""
    parser = argparse.ArgumentParser(description="Inspect or acquire bounded HDX resource bytes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    disposable = subparsers.add_parser("inspect-url")
    disposable.add_argument("url")
    disposable.add_argument("--format", required=True, help="inspection format (currently CSV only)")
    disposable.add_argument("--byte-limit", type=int, default=DEFAULT_INSPECTION_LIMIT)

    durable = subparsers.add_parser("acquire")
    durable.add_argument("url")
    durable.add_argument("--dataset-id", required=True)
    durable.add_argument("--resource-id", required=True)
    durable.add_argument("--output", type=pathlib.Path, required=True)
    durable.add_argument(
        "--force",
        action="store_true",
        help="deliberately replace an existing output and acquisition sidecar",
    )
    durable.add_argument(
        "--byte-limit",
        type=int,
        default=DEFAULT_ACQUISITION_LIMIT,
        help=(
            f"maximum downloaded bytes before refusal (default: {DEFAULT_ACQUISITION_LIMIT}); "
            "increase deliberately for an expected large resource"
        ),
    )

    local = subparsers.add_parser("inspect-file")
    local.add_argument("path", type=pathlib.Path)
    local.add_argument("--format", required=True, help="inspection format (currently CSV only)")
    local.add_argument(
        "--local-byte-limit",
        type=int,
        default=DEFAULT_LOCAL_INSPECTION_LIMIT,
        help=(
            f"maximum local input bytes (default: {DEFAULT_LOCAL_INSPECTION_LIMIT}); "
            "increase deliberately for an expected large file"
        ),
    )

    for item in (disposable, local):
        item.add_argument(
            "--stdout-bytes-limit",
            type=int,
            default=DEFAULT_STDOUT_BYTES_LIMIT,
            help=(
                f"maximum serialized inspection stdout bytes "
                f"(minimum/default: {DEFAULT_STDOUT_BYTES_LIMIT}); "
                "increase deliberately for larger output"
            ),
        )
        item.add_argument("--sample-rows", type=int, default=DEFAULT_SAMPLE_ROWS)
        item.add_argument(
            "--csv-field-size-limit",
            type=int,
            default=DEFAULT_CSV_FIELD_SIZE_LIMIT,
            help=(
                f"maximum parsed CSV field characters (default: {DEFAULT_CSV_FIELD_SIZE_LIMIT}); "
                "increase deliberately for expected wide fields"
            ),
        )

    # Kept as the existing top-level option for CLI compatibility. This timeout
    # bounds individual network operations; it is not a wall-clock deadline.
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    # CLI policy remains slightly stricter than direct-call policy: interactive
    # inspection must request at least one sample row, while library callers may
    # use sample_rows=0 for count/metadata-only parsing.
    if (
        getattr(args, "sample_rows", 1) < 1
        or getattr(args, "byte_limit", 1) < 1
        or getattr(args, "local_byte_limit", 1) < 1
        or getattr(args, "csv_field_size_limit", 1) < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        parser.error("limits, sample rows, and timeout must be finite/positive as applicable")
    if getattr(args, "stdout_bytes_limit", DEFAULT_STDOUT_BYTES_LIMIT) < DEFAULT_STDOUT_BYTES_LIMIT:
        parser.error(f"--stdout-bytes-limit must be at least {DEFAULT_STDOUT_BYTES_LIMIT}")

    try:
        if args.command == "inspect-url":
            result = disposable_inspect(
                args.url,
                args.format,
                args.byte_limit,
                args.sample_rows,
                args.timeout,
                args.csv_field_size_limit,
            )
        elif args.command == "acquire":
            result = acquire(
                args.url,
                args.dataset_id,
                args.resource_id,
                args.output,
                args.timeout,
                args.byte_limit,
                args.force,
            )
        else:
            result = _inspect_local_file(
                args.path,
                args.format,
                args.local_byte_limit,
                args.sample_rows,
                args.csv_field_size_limit,
            )

        if args.command != "acquire":
            payload = _inspection_stdout_payload(result, args.stdout_bytes_limit)
        else:
            # Acquisition has no separate stdout byte ceiling. Serialize the
            # complete final result before writing so locale-dependent text
            # encoding cannot produce a partial json.dump stream.
            payload = (
                json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
    except Exception as exc:
        parser.exit(1, f"HDX operation failed: {exc}\n")

    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
