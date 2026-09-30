#!/usr/bin/env python3
"""Bounded discovery of public HDX CKAN datasets.

HDX exposes its public catalog through CKAN's ``package_search`` operation.
This module turns one bounded search response into small HDA candidate records:
catalog packages that a caller can inspect before choosing data to acquire.

The CKAN request uses ``q`` for query text, ``rows`` for the requested number of
packages, ``start`` for the result offset, and repeatable ``fq`` values for CKAN
filters. ``q`` may be blank, so filters alone can define the search scope.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url


# These limits bound different stages of discovery. Candidate and resource
# limits cap how many publisher records are projected into the result, while
# DESCRIPTION_LIMIT caps retained descriptive text after whitespace cleanup.
# API_RESPONSE_LIMIT bounds bytes accepted from the remote CKAN service.
# DEFAULT_STDOUT_BYTES_LIMIT separately bounds the final serialized result: a
# compact remote response can still expand or combine into larger output.
CATALOG_API = "https://data.humdata.org/api/3/action/package_search"
MAX_CANDIDATES = 100
MAX_RESOURCES = 100
DESCRIPTION_LIMIT = 500
API_RESPONSE_LIMIT = 16 * 1024 * 1024
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024


def _bounded_text(value: Any, limit: int = DESCRIPTION_LIMIT) -> str | None:
    """Return readable bounded catalog prose without interpreting its meaning.

    CKAN ``notes`` may contain irregular runs of whitespace. Normalizing them
    makes descriptions readable, while truncation prevents one publisher
    description from consuming the bounded discovery result.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _resource(resource: dict[str, Any]) -> dict[str, Any]:
    """Project one HDX resource into fields needed for later acquisition.

    An HDX resource is a publisher-listed file, API, or other access endpoint
    attached to a catalog package; discovery reports it but does not acquire it.
    """
    return {
        "id": resource.get("id"),
        "name": resource.get("name"),
        "format": resource.get("format"),
        "url": resource.get("url"),
    }


def _candidate(package: dict[str, Any], resource_limit: int) -> dict[str, Any]:
    """Convert one HDX package into a bounded HDA discovery candidate.

    A candidate is a catalog lead, not a claim that the dataset is suitable for
    a particular use. ``catalog_metadata_literal`` means selected values are
    copied publisher metadata, not HDA semantic interpretation.

    Resource truncation is measured independently for every returned package.
    Only the bounded slice is projected and validated; resources beyond that
    slice are counted but otherwise left untouched.
    """
    resources = package.get("resources")
    if resources is None:
        resources = []
    elif not isinstance(resources, list):
        raise ValueError("HDX CKAN package resources is not a list")

    bounded_resources = resources[:resource_limit]
    if any(not isinstance(item, dict) for item in bounded_resources):
        raise ValueError("HDX CKAN package contains a non-object resource")
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
            "items": [_resource(item) for item in bounded_resources],
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
    """Search HDX and return a bounded, lineage-bearing discovery result.

    ``discover`` is a supported callable operation, so it enforces the same
    candidate, per-candidate resource, and finite positive timeout bounds as the
    CLI. Blank or whitespace query text remains valid for filter-only searches.

    ``searched_at_utc`` records request-start time rather than response time.
    ``rows=limit`` asks CKAN to limit its response; ``packages[:limit]`` remains
    as a defensive local bound in case a nonconforming service returns more.
    Each bounded package then becomes one HDA candidate record.

    ``catalog_matches`` is CKAN's count for the search, whereas
    ``returned_candidates`` is the number this call actually returns. Candidate
    truncation compares those values. ``resources_may_be_truncated`` summarizes
    only the returned candidates, not packages omitted by the candidate bound.

    Zero returned packages establishes only that this request scope returned no
    packages; it does not establish global absence or dataset suitability.
    """
    if limit < 1 or limit > MAX_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDIDATES}")
    if resource_limit < 1 or resource_limit > MAX_RESOURCES:
        raise ValueError(f"resource_limit must be between 1 and {MAX_RESOURCES}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    params: list[tuple[str, str | int]] = [("q", terms), ("rows", limit), ("start", 0)]
    params.extend(("fq", value) for value in filters)
    request_url = CATALOG_API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        require_http_url(request_url),
        headers={"Accept": "application/json", "User-Agent": "humanitarian-data-access/hdx-discover"},
    )
    # The remote body is read through a hard byte ceiling before any JSON is
    # decoded. The decoded value must then match CKAN's structural envelope: a
    # top-level object reporting success, with a result object that supplies a
    # result list and a non-negative integer count. Only after that envelope is
    # established are bounded package and resource objects shaped as candidates;
    # malformed publisher package/resource shapes fail explicitly instead of
    # being silently discarded or projected as if they were valid metadata.
    with open_http(request, timeout=timeout) as response:
        payload = remote_json_loads(read_limited(response, API_RESPONSE_LIMIT, "HDX catalog API response", response.headers))

    if not isinstance(payload, dict):
        raise ValueError("HDX CKAN response is not an object")
    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError("HDX CKAN returned an unsuccessful or malformed response")
    result = payload["result"]
    packages = result.get("results")
    if not isinstance(packages, list):
        raise ValueError("HDX CKAN response has no result list")
    catalog_count = result.get("count")
    if isinstance(catalog_count, bool) or not isinstance(catalog_count, int) or catalog_count < 0:
        raise ValueError("HDX CKAN response result count is not a non-negative integer")

    bounded_packages = packages[:limit]
    if any(not isinstance(item, dict) for item in bounded_packages):
        raise ValueError("HDX CKAN results contains a non-object package")
    candidates = [_candidate(item, resource_limit) for item in bounded_packages]

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
            "No HDX catalog packages were returned for this search scope."
        )
    return output


def _stdout_bytes(output: dict[str, Any], limit: int) -> bytes:
    """Serialize pretty JSON, or emit the existing all-or-nothing envelope.

    Normal stdout contains catalog identity, request scope, counts, bounds, and
    candidate/resource records. If that complete serialization exceeds the
    stdout byte ceiling, the envelope preserves catalog, search, count, and
    bounding context plus measured size, while omitting all candidate/resource
    payload data rather than partially packing records.
    """
    normal = (json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(normal) <= limit:
        return normal
    envelope = {
        "schema_version": "hda-output-limit-envelope/v1",
        "catalog": output["catalog"],
        "search": output["search"],
        "counts": output["counts"],
        "bounded": output["bounded"],
        "stdout_bytes_limit": limit,
        "normal_serialized_bytes": len(normal),
        "output_limit_exceeded": True,
        "data_omitted": True,
        "reason": "Candidate/resource payloads were omitted because the serialized stdout byte limit was exceeded.",
    }
    emitted = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(emitted) > limit:
        raise ValueError("output-limit envelope exceeds stdout byte limit")
    return emitted


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with separate usage, failure, and success channels.

    Argparse handles CLI usage and validation failures. Once arguments are
    accepted, runtime discovery or serialization failures are written to stderr
    and return status 1. A successful bounded JSON document is written to stdout
    and returns status 0.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("terms", help="CKAN catalog query text (may be blank)")
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
        help="CKAN fq filter; repeat to apply multiple filters",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--stdout-bytes-limit",
        type=_positive_int,
        default=DEFAULT_STDOUT_BYTES_LIMIT,
        help="serialized stdout byte ceiling (minimum/default: 262144; larger values override)",
    )
    args = parser.parse_args(argv)
    if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
        parser.error(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
    if args.limit > MAX_CANDIDATES:
        parser.error(f"--limit cannot exceed {MAX_CANDIDATES}")
    if args.resource_limit > MAX_RESOURCES:
        parser.error(f"--resource-limit cannot exceed {MAX_RESOURCES}")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    try:
        output = discover(args.terms, args.limit, args.resource_limit, args.filters, args.timeout)
        prepared = _stdout_bytes(output, args.stdout_bytes_limit)
    except Exception as exc:
        print(f"HDX discovery failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(prepared)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
