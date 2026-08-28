#!/usr/bin/env python3
"""Bounded access to UNFPA PDP maternal mortality through public ArcGIS REST."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from hda_http import open_http, read_limited, remote_json_loads, require_http_url


SOURCE_ID = "unfpa_population_data_portal"
INTERFACE_ID = "unfpa_pdp_arcgis_public"
HOST = "https://server.pdp.unfpa.org"
SERVICES_ROOT = f"{HOST}/arcgis/rest/services"
SERVICE_PATH = "pdp2_hv/hv_i52_gl2"
FEATURE_SERVER = f"{SERVICES_ROOT}/{SERVICE_PATH}/FeatureServer"
LAYER_ID = 0
LAYER_URL = f"{FEATURE_SERVER}/{LAYER_ID}"
QUERY_URL = f"{LAYER_URL}/query"
SERVICE_ITEM_ID = "638d24c696ef497bbac52c3217391414"
INDICATOR = "Maternal mortality ratio"
METADATA_LIMIT = 1024 * 1024
DATA_LIMIT = 1024 * 1024
MAX_QUERY_RECORDS = 100
MAX_STDOUT_RECORDS = 10
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
USER_AGENT = "hda-unfpa-arcgis/1.0"
PROOF_FIELDS = (
    "objectid", "geography_name", "country", "m49_code", "region_name",
    "region_view_code", "year", "source_code", "source_description", "value",
)
PROOF_WHERE = "country IS NOT NULL AND value IS NOT NULL"
PROOF_ORDER = "year DESC,objectid ASC"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(url: str, parameters: Mapping[str, str], limit: int, operation: str,
                  timeout: float) -> tuple[dict[str, Any], dict[str, Any], str]:
    source_url = url + "?" + urllib.parse.urlencode(parameters)
    request = urllib.request.Request(
        require_http_url(source_url),
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, limit, operation, response.headers)
        network = {
            "http_status": response.status,
            "content_type": response.headers.get("Content-Type"),
            "response_bytes": len(body),
            "response_limit_bytes": limit,
            "final_response_url": response.geturl(),
        }
    try:
        payload = remote_json_loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{operation} returned invalid JSON ({network})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{operation} did not return a JSON object")
    if payload.get("error"):
        raise ValueError(f"{operation} returned an ArcGIS error: {payload['error']}")
    return payload, network, source_url


def _visible_text(value: str) -> str:
    separated = re.sub(r"(?i)<br\s*/?>|</p>|</strong>|</u>", "\n", value)
    return "\n".join(
        line.strip() for line in html.unescape(re.sub(r"<[^>]+>", "", separated)).splitlines()
        if line.strip()
    )


def _section(text: str, heading: str) -> str | None:
    pattern = rf"(?:^|\n){re.escape(heading)}\n(.+?)(?=\n(?:Short Name|Full Name|Domain|Sub-domain|Tags|Definition|Method of Calculation|Expected Frequency of Data Dissemination|Geospatial Dimension Availability|Time Dimension Availability|Disaggregation Dimension Availability|References)\n|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else None


def _metadata(timeout: float) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    root, root_network, root_url = _request_json(
        SERVICES_ROOT, {"f": "json"}, METADATA_LIMIT, "UNFPA ArcGIS services-root metadata", timeout
    )
    service, service_network, service_url = _request_json(
        FEATURE_SERVER, {"f": "json"}, METADATA_LIMIT, "UNFPA FeatureServer metadata", timeout
    )
    layer, layer_network, layer_url = _request_json(
        LAYER_URL, {"f": "json"}, METADATA_LIMIT, "UNFPA layer metadata", timeout
    )
    network = {"services_root": root_network, "feature_server": service_network, "layer": layer_network}
    urls = {"services_root": root_url, "feature_server": service_url, "layer": layer_url}
    return {"root": root, "service": service, "layer": layer}, network, urls


def _inspect(metadata: Mapping[str, Any]) -> dict[str, Any]:
    root, service, layer = metadata["root"], metadata["service"], metadata["layer"]
    fields = layer.get("fields")
    advanced = layer.get("advancedQueryCapabilities")
    description = _visible_text(service.get("serviceDescription", ""))
    required = set(PROOF_FIELDS) | {"ld_ranking"}
    if not isinstance(fields, list):
        raise ValueError("live layer metadata lacks required native maternal mortality fields")
    if not all(isinstance(field, dict) for field in fields):
        raise ValueError("live layer metadata fields must contain only JSON objects")
    names = {item.get("name") for item in fields}
    if root.get("currentVersion") is None:
        raise ValueError("live ArcGIS services root lacks currentVersion")
    if service.get("serviceItemId") != SERVICE_ITEM_ID:
        raise ValueError("live FeatureServer service item does not match the pinned UNFPA service")
    if layer.get("id") != LAYER_ID or layer.get("name") != "hv_i52_gl2":
        raise ValueError("live layer identity does not match pinned layer 0 hv_i52_gl2")
    if _section(description, "Short Name") != INDICATOR:
        raise ValueError("live service metadata no longer identifies the maternal mortality ratio")
    if not required.issubset(names):
        raise ValueError("live layer metadata lacks required native maternal mortality fields")
    if "Query" not in str(layer.get("capabilities", "")).split(","):
        raise ValueError("live layer does not advertise Query capability")
    if not isinstance(advanced, dict):
        raise ValueError("live layer lacks advanced query capability metadata")
    return {
        "arcgis_version": root["currentVersion"],
        "service_item_id": service["serviceItemId"],
        "layer": {"id": layer["id"], "name": layer["name"]},
        "indicator": {
            "name": _section(description, "Short Name"),
            "domain": _section(description, "Domain"),
            "sub_domain": _section(description, "Sub-domain"),
        },
        "capabilities": layer.get("capabilities"),
        "max_record_count": layer.get("maxRecordCount"),
        "supported_query_formats": layer.get("supportedQueryFormats"),
        "advanced_query_capabilities": {
            key: advanced.get(key) for key in
            ("supportsPagination", "supportsOrderBy", "supportsStatistics")
        },
        "fields": [
            {key: field.get(key) for key in ("name", "alias", "type", "nullable")}
            for field in fields
        ],
        "time": {
            "service_description_availability": _section(description, "Time Dimension Availability"),
            "layer_time_info": layer.get("timeInfo"),
        },
    }


def discover(timeout: float) -> dict[str, Any]:
    metadata, network, urls = _metadata(timeout)
    return {
        "schema_version": "hda-unfpa-arcgis-discovery/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "source": "UNFPA Population Data Portal",
        "authentication": {"required": False, "class": "public anonymous"},
        "service": _inspect(metadata),
        "lineage": {"host": HOST, "service_path": SERVICE_PATH, "layer_id": LAYER_ID,
                    "metadata_urls": urls, "retrieved_at_utc": _timestamp()},
        "network": network,
    }


def query(result_offset: int, result_record_count: int, stdout_records: int,
          timeout: float) -> dict[str, Any]:
    if result_offset < 0:
        raise ValueError("result offset must be non-negative")
    if result_record_count < 1 or result_record_count > MAX_QUERY_RECORDS:
        raise ValueError(f"result record count must be between 1 and {MAX_QUERY_RECORDS}")
    if stdout_records < 1 or stdout_records > MAX_STDOUT_RECORDS:
        raise ValueError(f"stdout records must be between 1 and {MAX_STDOUT_RECORDS}")
    metadata, metadata_network, metadata_urls = _metadata(timeout)
    inspection = _inspect(metadata)
    advanced = inspection["advanced_query_capabilities"]
    if not advanced["supportsPagination"] or not advanced["supportsOrderBy"]:
        raise ValueError("live layer no longer supports the pinned pagination and ordering mechanics")
    parameters = {
        "where": PROOF_WHERE,
        "outFields": ",".join(PROOF_FIELDS),
        "resultOffset": str(result_offset),
        "resultRecordCount": str(result_record_count),
        "orderByFields": PROOF_ORDER,
        "returnGeometry": "false",
        "f": "json",
    }
    payload, data_network, source_url = _request_json(
        QUERY_URL, parameters, DATA_LIMIT, "UNFPA maternal mortality layer query", timeout
    )
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("UNFPA layer query returned no native features for the exact scope")
    records = []
    for feature in features:
        attributes = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attributes, dict):
            raise ValueError("UNFPA layer query returned a feature without native attributes")
        if attributes.get("value") is None:
            raise ValueError("UNFPA layer query returned a null observation value")
        records.append(attributes)
    shown = min(len(records), stdout_records)
    return {
        "schema_version": "hda-unfpa-arcgis-result/v1",
        "source_id": SOURCE_ID,
        "interface_id": INTERFACE_ID,
        "request_scope": {"host": HOST, "service_path": SERVICE_PATH, "layer_id": LAYER_ID,
                          "query_parameters": parameters},
        "lineage": {"source": "UNFPA Population Data Portal", "feature_server": FEATURE_SERVER,
                    "layer_url": LAYER_URL, "query_url": source_url,
                    "metadata_urls": metadata_urls, "retrieved_at_utc": _timestamp()},
        "network": {"anonymous": True, "metadata": metadata_network, "data": data_network,
                    "publisher_max_record_count": inspection["max_record_count"],
                    "response_feature_count": len(records),
                    "exceeded_transfer_limit": payload.get("exceededTransferLimit")},
        "result": {"returned_to_stdout": shown, "truncated_for_stdout": len(records) > shown,
                   "records": records[:shown]},
        "semantic_boundary": [
            "UNFPA-native country, M49, source, year, and value fields are preserved mechanically.",
            "HDA does not infer COD-AB or other geography equivalence, join geometry, interpret estimate-versus-observation semantics, or establish cross-source comparability.",
        ],
    }


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    normal = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(normal) <= limit: return normal
    envelope = {"schema_version": "hda-output-limit-envelope/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "request_scope": result.get("request_scope", {"operation": "discover", "service_path": SERVICE_PATH, "layer_id": LAYER_ID}), "provenance": {"lineage": result["lineage"], "network": result["network"]}, "counts": {"response_feature_count": result["network"].get("response_feature_count")}, "stdout_bytes_limit": limit, "normal_serialized_bytes": len(normal), "output_limit_exceeded": True, "data_omitted": True, "reason": "Publisher records/data were omitted because the serialized model-facing stdout limit was exceeded."}
    emitted = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(emitted) > limit: raise ValueError("output-limit envelope exceeds stdout byte limit")
    return emitted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="inspect the pinned UNFPA FeatureServer and layer metadata")
    q = sub.add_parser("query", help="run the bounded maternal mortality JSON query")
    q.add_argument("--result-offset", type=_nonnegative, default=0)
    q.add_argument("--result-record-count", type=_positive, default=5)
    q.add_argument("--stdout-records", type=_positive, default=MAX_STDOUT_RECORDS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=_positive, default=DEFAULT_STDOUT_BYTES_LIMIT, help=f"serialized UTF-8 stdout ceiling including newline; deliberate increase only (default: {DEFAULT_STDOUT_BYTES_LIMIT} bytes)")
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("timeout must be positive")
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT:
            raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        result = discover(args.timeout) if args.command == "discover" else query(
            args.result_offset, args.result_record_count, args.stdout_records, args.timeout
        )
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA UNFPA ArcGIS operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
