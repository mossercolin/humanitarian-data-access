#!/usr/bin/env python3
"""Bounded, deterministic discovery of public HDX CKAN datasets."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url


CATALOG_API = "https://data.humdata.org/api/3/action/package_search"
MAX_CANDIDATES = 100
MAX_RESOURCES = 100
DESCRIPTION_LIMIT = 500
API_RESPONSE_LIMIT = 16 * 1024 * 1024


def _bounded_text(value: Any, limit: int = DESCRIPTION_LIMIT) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _resource(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": resource.get("id"),
        "name": resource.get("name"),
        "format": resource.get("format"),
        "url": resource.get("url"),
    }


def _candidate(package: dict[str, Any], resource_limit: int) -> dict[str, Any]:
    resources = package.get("resources")
    if not isinstance(resources, list):
        resources = []
    literal_metadata = {
        key: package[key]
        for key in (
            "dataset_date",
            "data_update_frequency",
            "metadata_created",
            "metadata_modified",
            "groups",
        )
        if package.get(key) not in (None, "", [])
    }
    return {
        "dataset_id": package.get("id"),
        "name": package.get("name"),
        "title": package.get("title"),
        "description": _bounded_text(package.get("notes")),
        "resources": {
            "total": len(resources),
            "returned": min(len(resources), resource_limit),
            "truncated": len(resources) > resource_limit,
            "items": [_resource(item) for item in resources[:resource_limit]],
        },
        "catalog_metadata_literal": literal_metadata,
    }


def discover(
    terms: str,
    limit: int,
    resource_limit: int,
    filters: Sequence[str],
    timeout: float,
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    params: list[tuple[str, str | int]] = [("q", terms), ("rows", limit), ("start", 0)]
    params.extend(("fq", value) for value in filters)
    request_url = CATALOG_API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        require_http_url(request_url),
        headers={"Accept": "application/json", "User-Agent": "humanitarian-data-access/hdx-discover"},
    )
    with open_http(request, timeout=timeout) as response:
        payload = remote_json_loads(read_limited(response, API_RESPONSE_LIMIT, "HDX catalog API response", response.headers))

    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError("HDX CKAN returned an unsuccessful or malformed response")
    result = payload["result"]
    packages = result.get("results")
    if not isinstance(packages, list):
        raise ValueError("HDX CKAN response has no result list")
    candidates = [_candidate(item, resource_limit) for item in packages[:limit]]
    catalog_count = result.get("count")
    if not isinstance(catalog_count, int):
        raise ValueError("HDX CKAN response has no integer result count")

    output: dict[str, Any] = {
        "schema_version": "hdx-discovery/v1",
        "catalog": {
            "name": "Humanitarian Data Exchange (HDX)",
            "owner": "United Nations Office for the Coordination of Humanitarian Affairs",
            "interface": "CKAN package_search",
            "api_endpoint": CATALOG_API,
        },
        "search": {
            "terms": terms,
            "filters": list(filters),
            "searched_at_utc": timestamp,
            "scope": {
                "catalog": "public HDX CKAN catalog",
                "start": 0,
                "candidate_limit": limit,
                "resource_limit_per_candidate": resource_limit,
            },
        },
        "counts": {
            "catalog_matches": catalog_count,
            "returned_candidates": len(candidates),
        },
        "bounded": {
            "candidates_truncated": catalog_count > len(candidates),
            "resources_may_be_truncated": any(
                candidate["resources"]["truncated"] for candidate in candidates
            ),
        },
        "candidates": candidates,
    }
    if not candidates:
        output["scoped_negative"] = (
            "No suitable structured candidate found in HDX for this search scope."
        )
    return output


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("terms", help="exact humanitarian catalog search terms")
    parser.add_argument("--limit", type=_positive_int, default=10, help="candidate limit (default: 10)")
    parser.add_argument(
        "--resource-limit",
        type=_positive_int,
        default=20,
        help="resource limit per candidate (default: 20)",
    )
    parser.add_argument(
        "--filter",
        dest="filters",
        action="append",
        default=[],
        help="exact CKAN fq filter; repeat to apply multiple filters",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    args = parser.parse_args(argv)
    if args.limit > MAX_CANDIDATES:
        parser.error(f"--limit cannot exceed {MAX_CANDIDATES}")
    if args.resource_limit > MAX_RESOURCES:
        parser.error(f"--resource-limit cannot exceed {MAX_RESOURCES}")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        output = discover(args.terms, args.limit, args.resource_limit, args.filters, args.timeout)
    except Exception as exc:
        print(f"HDX discovery failed: {exc}", file=sys.stderr)
        return 1
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
