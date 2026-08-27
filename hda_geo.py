#!/usr/bin/env python3
"""Bounded access to OCHA Common Operational Dataset administrative boundaries."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any, Sequence


CKAN_ACTION = "https://data.humdata.org/api/3/action"
USER_AGENT = "hda-geo/1.0"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(action: str, parameters: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = f"{CKAN_ACTION}/{action}?{urllib.parse.urlencode(parameters)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not payload.get("success") or not isinstance(payload.get("result"), (dict, list)):
        raise ValueError(f"HDX CKAN {action} did not return a successful result")
    return {"url": url, "result": payload["result"]}


def _country_match(dataset: dict[str, Any], query: str) -> bool:
    if not str(dataset.get("name", "")).startswith("cod-ab-"):
        return False
    needle = query.strip().casefold()
    values = [dataset.get("name", "").removeprefix("cod-ab-")]
    for group in dataset.get("groups") or []:
        values.extend([group.get("id", ""), group.get("name", ""), group.get("title", "")])
    return needle in {str(value).strip().casefold() for value in values}


def _dataset(query: str, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
    search = _request_json("package_search", {"q": query, "rows": 10}, timeout)
    candidates = [item for item in search["result"].get("results", []) if _country_match(item, query)]
    if len(candidates) != 1:
        names = [item.get("name") for item in candidates]
        raise ValueError(f"country must resolve to exactly one COD-AB dataset; matches={names}")
    shown = _request_json("package_show", {"id": candidates[0]["id"]}, timeout)
    return shown["result"], {"search_url": search["url"], "dataset_url": shown["url"]}


def _resource(dataset: dict[str, Any]) -> dict[str, Any]:
    resources = [
        item for item in dataset.get("resources") or []
        if str(item.get("format", "")).casefold() == "geojson"
        and str(item.get("url", "")).lower().endswith(".geojson.zip")
    ]
    if len(resources) != 1:
        raise ValueError(f"expected exactly one COD-AB GeoJSON archive; found {len(resources)}")
    return resources[0]


def _archive(url: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    return data, hashlib.sha256(data).hexdigest()


def _feature_collection(data: bytes, admin_level: int) -> tuple[str, dict[str, Any]]:
    pattern = re.compile(rf"(^|/)[a-z]{{3}}_admin{admin_level}\.geojson$", re.IGNORECASE)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [name for name in archive.namelist() if pattern.search(name)]
        if len(members) != 1:
            raise ValueError(f"expected one non-edge-matched admin{admin_level} GeoJSON member; found {members}")
        payload = json.load(archive.open(members[0]))
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ValueError("COD-AB member is not a GeoJSON FeatureCollection")
    if not payload["features"]:
        raise ValueError("COD-AB FeatureCollection is empty")
    return members[0], payload


def _identity(feature: dict[str, Any], admin_level: int) -> dict[str, Any]:
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


def access(country: str, admin_level: int, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset, request_lineage = _dataset(country, timeout)
    resource = _resource(dataset)
    archive, digest = _archive(resource["url"], timeout)
    member, geojson = _feature_collection(archive, admin_level)
    crs = geojson.get("crs")
    crs_claim = (
        "source CRS declaration preserved; no transformation or EPSG equivalence asserted"
        if crs is not None
        else "source GeoJSON contains no CRS declaration; no transformation or EPSG equivalence asserted"
    )
    identities = [_identity(feature, admin_level) for feature in geojson["features"]]
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
        "country_query": country,
        "admin_level": admin_level,
        "feature_count": len(geojson["features"]),
        "identities": identities,
        "crs": crs,
        "crs_claim": crs_claim,
        "lineage": lineage,
    }
    geojson["hda"] = {"schema_version": "hda-geographic-access/v1", "lineage": lineage, "crs_claim": result["crs_claim"]}
    return result, geojson


def _write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    resolve = subparsers.add_parser("resolve", help="resolve a country through its COD-AB admin-0 record")
    resolve.add_argument("country")
    resolve.add_argument("--timeout", type=float, default=60)
    boundary = subparsers.add_parser("boundary", help="obtain one COD-AB administrative level")
    boundary.add_argument("country")
    boundary.add_argument("--admin-level", type=int, default=0)
    boundary.add_argument("--output", type=pathlib.Path, required=True, help="GeoJSON FeatureCollection output")
    boundary.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        level = 0 if args.operation == "resolve" else args.admin_level
        if not 0 <= level <= 9:
            raise ValueError("admin level must be between 0 and 9")
        result, geojson = access(args.country, level, args.timeout)
        if args.operation == "resolve":
            result["identities"] = result["identities"][:1]
        else:
            _write_json(args.output, geojson)
            result["geojson_output"] = str(args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
