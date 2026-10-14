#!/usr/bin/env python3
"""Bounded access to OCHA Common Operational Dataset administrative boundaries."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url


# Flow: search HDX for one COD-AB dataset, select its GeoJSON ZIP and
# requested administrative-level member, then preserve native identities
# and source lineage. Only stdout is byte-bounded; boundary also writes GeoJSON.
SOURCE_ID = "ocha_cod_ab"
INTERFACE_ID = "ocha_cod_ab_hdx_geojson"
CKAN_ACTION = "https://data.humdata.org/api/3/action"
USER_AGENT = "hda-geo/1.0"
API_RESPONSE_LIMIT = 16 * 1024 * 1024
DEFAULT_ARCHIVE_LIMIT = 256 * 1024 * 1024
DEFAULT_GEOJSON_MEMBER_LIMIT = 512 * 1024 * 1024
DECOMPRESSION_CHUNK_SIZE = 1024 * 1024
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(action: str, parameters: dict[str, Any], timeout: float) -> dict[str, Any]:
    # Each catalog action response has its own fixed 16 MiB byte limit.
    url = f"{CKAN_ACTION}/{action}?{urllib.parse.urlencode(parameters)}"
    request = urllib.request.Request(require_http_url(url), headers={"User-Agent": USER_AGENT})
    with open_http(request, timeout=timeout) as response:
        payload = remote_json_loads(read_limited(response, API_RESPONSE_LIMIT, "HDX COD catalog API response", response.headers))
    if not payload.get("success") or not isinstance(payload.get("result"), (dict, list)):
        raise ValueError(f"HDX CKAN {action} did not return a successful result")
    return {"url": url, "result": payload["result"]}


def _country_match(dataset: dict[str, Any], query: str) -> bool:
    # Require the cod-ab- prefix and exact case-folded equality with the
    # name suffix or a catalog group ID/name/title; no fuzzy matching.
    if not str(dataset.get("name", "")).startswith("cod-ab-"):
        return False
    needle = query.strip().casefold()
    values = [dataset.get("name", "").removeprefix("cod-ab-")]
    for group in dataset.get("groups") or []:
        values.extend([group.get("id", ""), group.get("name", ""), group.get("title", "")])
    return needle in {str(value).strip().casefold() for value in values}


def _dataset(query: str, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
    # General HDX text search examines at most the first 10 returned results.
    # One COD-AB match is required within that window, not across the full
    # catalog; a name match does not establish geographic equivalence.
    search = _request_json("package_search", {"q": query, "rows": 10}, timeout)
    candidates = [item for item in search["result"].get("results", []) if _country_match(item, query)]
    if len(candidates) != 1:
        names = [item.get("name") for item in candidates]
        raise ValueError(f"country must resolve to exactly one COD-AB dataset; matches={names}")
    shown = _request_json("package_show", {"id": candidates[0]["id"]}, timeout)
    return shown["result"], {"search_url": search["url"], "dataset_url": shown["url"]}


def _resource(dataset: dict[str, Any]) -> dict[str, Any]:
    # Select exactly one resource declared GeoJSON with a .geojson.zip URL,
    # rather than guessing between multiple catalog resources.
    resources = [
        item for item in dataset.get("resources") or []
        if str(item.get("format", "")).casefold() == "geojson"
        and str(item.get("url", "")).lower().endswith(".geojson.zip")
    ]
    if len(resources) != 1:
        raise ValueError(f"expected exactly one COD-AB GeoJSON archive; found {len(resources)}")
    return resources[0]


def _archive(url: str, timeout: float, byte_limit: int = DEFAULT_ARCHIVE_LIMIT) -> tuple[bytes, str]:
    # Bound downloaded compressed ZIP bytes (256 MiB default). SHA-256
    # identifies those bytes, not publisher authenticity or data freshness.
    request = urllib.request.Request(require_http_url(url), headers={"User-Agent": USER_AGENT})
    with open_http(request, timeout=timeout) as response:
        data = read_limited(response, byte_limit, "COD-AB remote archive", response.headers)
    return data, hashlib.sha256(data).hexdigest()


def _read_member_limited(stream: Any, byte_limit: int) -> bytes:
    # Enforce the actual decompressed selected-member byte limit while
    # reading, independently of the size declared in ZIP metadata.
    chunks = []
    size = 0
    while chunk := stream.read(min(DECOMPRESSION_CHUNK_SIZE, byte_limit - size + 1)):
        size += len(chunk)
        if size > byte_limit:
            raise ValueError(
                f"decompressed GeoJSON member exceeds the {byte_limit}-byte limit; "
                "use --geojson-member-byte-limit with an expected larger size"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _feature_collection(data: bytes, admin_level: int,
                        member_byte_limit: int = DEFAULT_GEOJSON_MEMBER_LIMIT) -> tuple[str, dict[str, Any]]:
    # Select one xxx_adminN.geojson member, possibly inside a ZIP folder.
    # This non-edge-matched filename convention does not validate geometry
    # or establish whether boundaries are actually edge-matched.
    pattern = re.compile(rf"(^|/)[a-z]{{3}}_admin{admin_level}\.geojson$", re.IGNORECASE)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [item for item in archive.infolist() if pattern.search(item.filename)]
        if len(members) != 1:
            raise ValueError(f"expected one non-edge-matched admin{admin_level} GeoJSON member; found {[item.filename for item in members]}")
        member = members[0]
        # Declared uncompressed size allows early rejection; the subsequent
        # bounded read checks actual bytes (512 MiB default, configurable).
        if member.file_size > member_byte_limit:
            raise ValueError(
                f"declared uncompressed GeoJSON member size {member.file_size} exceeds the "
                f"{member_byte_limit}-byte limit; use --geojson-member-byte-limit with an expected larger size"
            )
        with archive.open(member) as stream:
            document = _read_member_limited(stream, member_byte_limit)
        payload = remote_json_loads(document)
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ValueError("COD-AB member is not a GeoJSON FeatureCollection")
    if not payload["features"]:
        raise ValueError("COD-AB FeatureCollection is empty")
    return member.filename, payload


def _identity(feature: dict[str, Any], admin_level: int) -> dict[str, Any]:
    # Retain the level name/P-code and available lower-level parent fields;
    # missing parents are not reconstructed. ISO codes, publisher-supplied
    # center coordinates, validity dates and version are not validated.
    properties = feature.get("properties") or {}
    prefix = f"adm{admin_level}"
    parents = []
    for level in range(admin_level):
        name, code = properties.get(f"adm{level}_name"), properties.get(f"adm{level}_pcode")
        if name is not None or code is not None:
            parents.append({"admin_level": level, "name": name, "native_id": code})
    return {
        "name": properties.get(f"{prefix}_name"),
        "native_id": properties.get(f"{prefix}_pcode"),
        "admin_level": admin_level,
        "iso2": properties.get("iso2"),
        "iso3": properties.get("iso3"),
        "representative_coordinates": {
            "longitude": properties.get("center_lon"),
            "latitude": properties.get("center_lat"),
            "source_field_semantics": "publisher-supplied center fields",
        },
        "parents": parents,
        "valid_on": properties.get("valid_on"),
        "valid_to": properties.get("valid_to"),
        "version": properties.get("version"),
    }


def access(country: str, admin_level: int, timeout: float,
           archive_byte_limit: int = DEFAULT_ARCHIVE_LIMIT,
           geojson_member_byte_limit: int = DEFAULT_GEOJSON_MEMBER_LIMIT) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset, request_lineage = _dataset(country, timeout)
    resource = _resource(dataset)
    archive, digest = _archive(resource["url"], timeout, archive_byte_limit)
    member, geojson = _feature_collection(archive, admin_level, geojson_member_byte_limit)
    # Preserve any source CRS declaration, without transforming coordinates
    # or establishing equivalence to an EPSG reference system.
    crs = geojson.get("crs")
    crs_claim = (
        "source CRS declaration preserved; no transformation or EPSG equivalence asserted"
        if crs is not None
        else "source GeoJSON contains no CRS declaration; no transformation or EPSG equivalence asserted"
    )
    identities = [_identity(feature, admin_level) for feature in geojson["features"]]
    # Dataset/resource metadata comes from HDX; authority, product and
    # delivery_catalog are HDA labels. retrieved_at is stamped after parsing.
    lineage = {
        "authority": "United Nations OCHA",
        "product": "Common Operational Datasets — Administrative Boundaries (COD-AB)",
        "delivery_catalog": "OCHA Centre for Humanitarian Data, Humanitarian Data Exchange",
        "dataset_id": dataset.get("id"),
        "dataset_name": dataset.get("name"),
        "dataset_title": dataset.get("title"),
        "dataset_date": dataset.get("dataset_date"),
        "dataset_metadata_modified": dataset.get("metadata_modified"),
        "dataset_organization": (dataset.get("organization") or {}).get("title"),
        "resource_id": resource.get("id"),
        "resource_name": resource.get("name"),
        "resource_url": resource.get("url"),
        "archive_sha256": digest,
        "archive_member": member,
        "retrieved_at": _timestamp(),
        **request_lineage,
    }
    result = {
        "schema_version": "hda-geographic-access/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "country_query": country,
        "admin_level": admin_level,
        "feature_count": len(geojson["features"]),
        "identities": identities,
        "crs": crs,
        "crs_claim": crs_claim,
        "lineage": lineage,
    }
    # boundary writes reserialized GeoJSON with this top-level HDA
    # provenance field, not a byte-for-byte copy of the archived member.
    geojson["hda"] = {"schema_version": "hda-geographic-access/v1", "lineage": lineage, "crs_claim": result["crs_claim"]}
    return result, geojson


def _write_json(path: pathlib.Path, payload: Any, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"output already exists: {path}; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
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


def _stdout_bytes(result: dict[str, Any], byte_limit: int) -> bytes:
    # Stdout has a configurable 256 KiB default/minimum, not a file limit.
    # Oversized normal results become a successful output-limit envelope
    # with no identities and the original schema in source_schema_version.
    # If even that envelope exceeds the limit, output fails.
    normal = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(normal) <= byte_limit:
        return normal
    envelope = {
        key: result[key]
        for key in (
            "source_id", "interface_id", "country_query", "admin_level",
            "feature_count", "crs", "crs_claim", "lineage", "geojson_output",
        )
        if key in result
    }
    envelope.update({
        "schema_version": "hda-output-limit-envelope/v1",
        "source_schema_version": result.get("schema_version"),
        "stdout_bytes_limit": byte_limit,
        "normal_serialized_bytes": len(normal),
        "output_limit_exceeded": True,
        "data_omitted": True,
        "reason": "serialized geographic output exceeds stdout byte limit; identities omitted",
    })
    prepared = (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(prepared) > byte_limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit")
    return prepared


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    resolve = subparsers.add_parser("resolve", help="return the first COD-AB administrative-level-zero identity for the country query", description="Return the first COD-AB admin-0 identity; feature_count retains the full collection count.")
    resolve.add_argument("country")
    resolve.add_argument("--timeout", type=float, default=60)
    boundary = subparsers.add_parser("boundary", help="obtain one COD-AB administrative level")
    boundary.add_argument("country")
    boundary.add_argument("--admin-level", type=int, default=0)
    boundary.add_argument("--output", type=pathlib.Path, required=True, help="GeoJSON FeatureCollection output")
    boundary.add_argument("--force", action="store_true", help="deliberately replace an existing GeoJSON output")
    boundary.add_argument("--timeout", type=float, default=60)
    for command in (resolve, boundary):
        command.add_argument(
            "--stdout-bytes-limit",
            type=int,
            default=DEFAULT_STDOUT_BYTES_LIMIT,
            help=f"maximum serialized stdout bytes (minimum/default: {DEFAULT_STDOUT_BYTES_LIMIT}); increase deliberately for larger output",
        )
        command.add_argument(
            "--archive-byte-limit",
            type=int,
            default=DEFAULT_ARCHIVE_LIMIT,
            help=f"maximum COD-AB archive response bytes (default: {DEFAULT_ARCHIVE_LIMIT}); increase deliberately for an expected large archive",
        )
        command.add_argument(
            "--geojson-member-byte-limit",
            type=int,
            default=DEFAULT_GEOJSON_MEMBER_LIMIT,
            help=f"maximum decompressed selected GeoJSON member bytes (default: {DEFAULT_GEOJSON_MEMBER_LIMIT}); increase deliberately for an expected large boundary",
        )
    args = parser.parse_args(argv)
    try:
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"stdout byte limit must be at least {DEFAULT_STDOUT_BYTES_LIMIT}")
        level = 0 if args.operation == "resolve" else args.admin_level
        if not 0 <= level <= 9:
            raise ValueError("admin level must be between 0 and 9")
        if args.archive_byte_limit < 1:
            raise ValueError("archive byte limit must be positive")
        if args.geojson_member_byte_limit < 1:
            raise ValueError("GeoJSON member byte limit must be positive")
        if args.operation == "boundary" and args.output.exists() and not args.force:
            raise FileExistsError(f"output already exists: {args.output}; use --force to replace it")
        result, geojson = access(
            args.country, level, args.timeout, args.archive_byte_limit,
            args.geojson_member_byte_limit,
        )
        if args.operation == "resolve":
            # Only the first admin-0 identity (in member order) is shown;
            # feature_count still describes the full FeatureCollection.
            result["identities"] = result["identities"][:1]
        else:
            # File output precedes stdout. A later stdout failure can leave
            # the boundary file already written (or replaced with --force).
            _write_json(args.output, geojson, args.force)
            result["geojson_output"] = str(args.output)
        prepared = _stdout_bytes(result, args.stdout_bytes_limit)
        sys.stdout.buffer.write(prepared)
        return 0
    except Exception as exc:
        parser.exit(1, f"HDA GEO failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
