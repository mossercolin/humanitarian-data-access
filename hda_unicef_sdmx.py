#!/usr/bin/env python3
"""Bounded discovery and observation access for UNICEF's public SDMX flow."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Sequence

from hda_http import read_limited, require_http_url


SOURCE_ID = "unicef_data"
INTERFACE_ID = "unicef_sdmx_public"
BASE_URL = "https://sdmx.data.unicef.org/ws/public/sdmxapi/rest"
FLOW_AGENCY = "UNICEF"
FLOW_ID = "IMMUNISATION"
FLOW_VERSION = "1.0"
STRUCTURE_URL = f"{BASE_URL}/dataflow/{FLOW_AGENCY}/{FLOW_ID}/{FLOW_VERSION}?references=all"
DOCS_URL = "https://data.unicef.org/sdmx-api-documentation/"
STRUCTURE_LIMIT = 4 * 1024 * 1024
DATA_LIMIT = 1024 * 1024
MAX_STDOUT_OBSERVATIONS = 20
USER_AGENT = "hda-unicef-sdmx/1.0"
STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml;version=2.1"
DATA_ACCEPT = "application/vnd.sdmx.genericdata+xml;version=2.1"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_xml(url: str, accept: str, limit: int, operation: str, timeout: float) -> tuple[ET.Element, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": accept, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = read_limited(response, limit, operation, response.headers)
        facts = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": limit}
    try:
        return ET.fromstring(body), facts
    except ET.ParseError as exc:
        raise ValueError(f"{operation} returned invalid XML ({facts})") from exc


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _english_name(element: ET.Element) -> str | None:
    names = [item for item in element if _local(item) == "Name"]
    chosen = next((item for item in names if item.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"), names[0] if names else None)
    return chosen.text if chosen is not None else None


def _structure(root: ET.Element) -> dict[str, Any]:
    flow = next((item for item in root.iter() if _local(item) == "Dataflow" and item.get("id") == FLOW_ID), None)
    dsd = next((item for item in root.iter() if _local(item) == "DataStructure" and item.get("id") == FLOW_ID), None)
    if flow is None or dsd is None:
        raise ValueError(f"live structure response lacks {FLOW_ID} dataflow or data structure")
    dimensions = []
    for item in dsd.iter():
        if _local(item) not in {"Dimension", "TimeDimension"} or not item.get("position"):
            continue
        representation = next((ref for ref in item.iter() if _local(ref) == "Ref" and ref.get("class") == "Codelist"), None)
        dimensions.append({"id": item.get("id"), "position": int(item.get("position", "0")), "role": "time" if _local(item) == "TimeDimension" else "series", "codelist": representation.get("id") if representation is not None else None})
    dimensions.sort(key=lambda item: item["position"])
    if [item["id"] for item in dimensions] != ["REF_AREA", "INDICATOR", "VACCINE", "AGE", "TIME_PERIOD"]:
        raise ValueError(f"unexpected live {FLOW_ID} dimension order: {[item['id'] for item in dimensions]}")
    return {"agency": flow.get("agencyID"), "dataflow": flow.get("id"), "version": flow.get("version"), "name": _english_name(flow), "dimensions": dimensions}


def discover(timeout: float) -> dict[str, Any]:
    root, network = _request_xml(STRUCTURE_URL, STRUCTURE_ACCEPT, STRUCTURE_LIMIT, "UNICEF SDMX structure response", timeout)
    return {"schema_version": "hda-unicef-sdmx-discovery/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "source": "UNICEF Data Warehouse", "base_url": BASE_URL, "official_docs": DOCS_URL, "authentication": {"required": False, "class": "public anonymous"}, "structure": _structure(root), "lineage": {"structure_url": STRUCTURE_URL, "retrieved_at_utc": _timestamp()}, "network": network}


def _values(element: ET.Element) -> dict[str, str]:
    return {item.get("id", ""): item.get("value", "") for item in element if _local(item) == "Value"}


def _observations(root: ET.Element) -> list[dict[str, Any]]:
    rows = []
    for series in (item for item in root.iter() if _local(item) == "Series"):
        key_element = next((item for item in series if _local(item) == "SeriesKey"), None)
        series_attributes = next((item for item in series if _local(item) == "Attributes"), None)
        key = _values(key_element) if key_element is not None else {}
        attributes = _values(series_attributes) if series_attributes is not None else {}
        for obs in (item for item in series if _local(item) == "Obs"):
            period = next((item.get("value") for item in obs if _local(item) == "ObsDimension"), None)
            value = next((item.get("value") for item in obs if _local(item) == "ObsValue"), None)
            obs_attributes = next((item for item in obs if _local(item) == "Attributes"), None)
            rows.append({"series_key": key, "time_period": period, "obs_value": value, "series_attributes": attributes, "observation_attributes": _values(obs_attributes) if obs_attributes is not None else {}})
    return rows


def query(ref_area: str, indicator: str, vaccine: str, age: str, start_period: str, end_period: str, timeout: float) -> dict[str, Any]:
    native = [ref_area, indicator, vaccine, age, start_period, end_period]
    if any(not item or any(mark in item for mark in ".+/ ?&#") for item in native):
        raise ValueError("native codes and periods must be non-empty single SDMX key components")
    key = ".".join([ref_area, indicator, vaccine, age])
    endpoint = f"{BASE_URL}/data/{FLOW_ID}/{key}"
    source_url = endpoint + "?" + urllib.parse.urlencode({"startPeriod": start_period, "endPeriod": end_period})
    root, network = _request_xml(source_url, DATA_ACCEPT, DATA_LIMIT, "UNICEF SDMX data response", timeout)
    observations = _observations(root)
    if not observations:
        raise ValueError("UNICEF SDMX response contained no observations for the exact query scope")
    truncated = len(observations) > MAX_STDOUT_OBSERVATIONS
    return {"schema_version": "hda-unicef-sdmx-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "dataflow": {"agency": FLOW_AGENCY, "id": FLOW_ID, "version": FLOW_VERSION}, "request_scope": {"dimension_order": ["REF_AREA", "INDICATOR", "VACCINE", "AGE", "TIME_PERIOD"], "native_codes": {"REF_AREA": ref_area, "INDICATOR": indicator, "VACCINE": vaccine, "AGE": age}, "start_period": start_period, "end_period": end_period}, "lineage": {"source": "UNICEF Data Warehouse", "official_docs": DOCS_URL, "endpoint_url": endpoint, "source_url": source_url, "retrieved_at_utc": _timestamp()}, "network": {"anonymous": True, **network, "observation_count": len(observations)}, "result": {"returned_to_stdout": min(len(observations), MAX_STDOUT_OBSERVATIONS), "truncated_for_stdout": truncated, "observations": observations[:MAX_STDOUT_OBSERVATIONS]}, "semantic_boundary": ["Values and native qualifiers are exposed mechanically; HDA does not interpret reporting periods, publication or collection dates, denominators, geography equivalence, or indicator comparability."]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="inspect the selected live dataflow and DSD")
    q = sub.add_parser("query", help="run one exact bounded native-code observation query")
    q.add_argument("--ref-area", required=True); q.add_argument("--indicator", required=True); q.add_argument("--vaccine", required=True); q.add_argument("--age", required=True); q.add_argument("--start-period", required=True); q.add_argument("--end-period", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0: raise ValueError("timeout must be positive")
        result = discover(args.timeout) if args.command == "discover" else query(args.ref_area, args.indicator, args.vaccine, args.age, args.start_period, args.end_period, args.timeout)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2); print(); return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA UNICEF SDMX operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
