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

from hda_http import open_http, read_limited, require_http_url


# Flow: discover retrieves live IMMUNISATION structure; three query commands
# request observations for explicit native codes in locally configured flows.
# Queries build ordered dot-separated keys, match required returned components,
# and expose native periods, values and attributes within bounded output.
# These are source-access mechanics, not interpretation of indicator meaning.

SOURCE_ID = "unicef_data"
INTERFACE_ID = "unicef_sdmx_public"
BASE_URL = "https://sdmx.data.unicef.org/ws/public/sdmxapi/rest"
# Flow IDs, version, key sequences and endpoint patterns are locally configured,
# not discovered for every query. Only discover() inspects live IMMUNISATION
# structure; it does not inspect NUTRITION or CME structures.
FLOW_AGENCY = "UNICEF"
FLOW_ID = "IMMUNISATION"
FLOW_VERSION = "1.0"
STRUCTURE_URL = f"{BASE_URL}/dataflow/{FLOW_AGENCY}/{FLOW_ID}/{FLOW_VERSION}?references=all"
DOCS_URL = "https://data.unicef.org/sdmx-api-documentation/"
STRUCTURE_LIMIT = 4 * 1024 * 1024
DATA_LIMIT = 1024 * 1024
MAX_STDOUT_OBSERVATIONS = 20
REQUIRED_SERIES_KEY_COMPONENTS = ("REF_AREA", "INDICATOR", "VACCINE", "AGE")
NUTRITION_FLOW_ID = "NUTRITION"
NUTRITION_SERIES_KEY_COMPONENTS = ("REF_AREA", "INDICATOR", "SEX", "AGE", "WEALTH_QUINTILE", "RESIDENCE", "MATERNAL_EDU_LVL", "HEAD_OF_HOUSE")
CME_FLOW_ID = "CME"
CME_SERIES_KEY_COMPONENTS = ("REF_AREA", "INDICATOR", "SEX", "WEALTH_QUINTILE")
DEFAULT_STDOUT_BYTES_LIMIT = 256 * 1024
USER_AGENT = "hda-unicef-sdmx/1.0"
STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml;version=2.1"
DATA_ACCEPT = "application/vnd.sdmx.genericdata+xml;version=2.1"
DOCTYPE_MARKERS = tuple("<!DOCTYPE".encode(encoding) for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Acquire one XML body under the caller's byte ceiling: 4 MiB for structure,
# 1 MiB for data. Reject configured DOCTYPE byte patterns before ElementTree
# parsing; this does not amount to comprehensive XML or schema validation.
# Retain HTTP status, content type, received bytes, limit and final URL as
# response diagnostics, including for XML parsing or DOCTYPE failures.
def _request_xml(url: str, accept: str, limit: int, operation: str, timeout: float) -> tuple[ET.Element, dict[str, Any]]:
    request = urllib.request.Request(require_http_url(url), headers={"Accept": accept, "User-Agent": USER_AGENT})
    with open_http(request, timeout=timeout) as response:
        body = read_limited(response, limit, operation, response.headers)
        facts = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "response_bytes": len(body), "response_limit_bytes": limit, "final_response_url": response.geturl()}
    try:
        if any(marker in body for marker in DOCTYPE_MARKERS):
            raise ValueError(f"{operation} contained a prohibited DOCTYPE declaration ({facts})")
        return ET.fromstring(body), facts
    except ET.ParseError as exc:
        raise ValueError(f"{operation} returned invalid XML ({facts})") from exc


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _english_name(element: ET.Element) -> str | None:
    names = [item for item in element if _local(item) == "Name"]
    chosen = next((item for item in names if item.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"), names[0] if names else None)
    return chosen.text if chosen is not None else None


# From the live response, locate IMMUNISATION Dataflow and DataStructure by
# the locally expected ID. Extract reported flow metadata and positioned
# dimensions/codelist references, then check the exact dimension ID order.
# This does not validate full DSD references, codelist values or version/agency
# alignment, nor does it validate NUTRITION or CME structure.
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


# For each returned Series, require the named components to equal the caller's
# native codes. Extra returned components are allowed; full-key equality and
# returned key order are not checked. Preserve the full returned key, period,
# value, and series/observation attributes without assigning humanitarian meaning.
# Missing/empty periods or values fail; returned periods are not independently
# checked against the requested start/end interval.
def _observations(root: ET.Element, requested_native_codes: dict[str, str], required_components: tuple[str, ...] = REQUIRED_SERIES_KEY_COMPONENTS) -> list[dict[str, Any]]:
    rows = []
    for series in (item for item in root.iter() if _local(item) == "Series"):
        key_element = next((item for item in series if _local(item) == "SeriesKey"), None)
        series_attributes = next((item for item in series if _local(item) == "Attributes"), None)
        key = _values(key_element) if key_element is not None else {}
        attributes = _values(series_attributes) if series_attributes is not None else {}
        for component in required_components:
            if component not in key:
                raise ValueError(f"UNICEF SDMX returned SeriesKey lacks required component {component}")
            if key[component] != requested_native_codes[component]:
                raise ValueError(f"UNICEF SDMX returned SeriesKey {component} does not exactly match requested native value")
        for obs in (item for item in series if _local(item) == "Obs"):
            period = next((item.get("value") for item in obs if _local(item) == "ObsDimension"), None)
            value = next((item.get("value") for item in obs if _local(item) == "ObsValue"), None)
            if not period:
                raise ValueError("UNICEF SDMX returned observation with missing or empty time")
            if not value:
                raise ValueError("UNICEF SDMX returned observation with missing or empty value")
            obs_attributes = next((item for item in obs if _local(item) == "Attributes"), None)
            rows.append({"series_key": key, "time_period": period, "obs_value": value, "series_attributes": attributes, "observation_attributes": _values(obs_attributes) if obs_attributes is not None else {}})
    return rows


# The three queries join caller-supplied native codes in the indicated order
# using ".", with TIME_PERIOD represented by observations and startPeriod/
# endPeriod supplied as URL parameters. They share bounded XML retrieval and
# extraction, returning at most 20 observations; observation_count counts this
# received response, not all observations held by UNICEF.
# IMMUNISATION uses REF_AREA.INDICATOR.VACCINE.AGE and /data/IMMUNISATION/<key>.
def query(ref_area: str, indicator: str, vaccine: str, age: str, start_period: str, end_period: str, timeout: float) -> dict[str, Any]:
    path_components = [ref_area, indicator, vaccine, age]
    if any(not item or any(mark in item for mark in ".+/ ?&#%") for item in path_components):
        raise ValueError("native path codes must be non-empty single SDMX key components")
    periods = [start_period, end_period]
    if any(not item or any(mark in item for mark in ".+/ ?&#") for item in periods):
        raise ValueError("native codes and periods must be non-empty single SDMX key components")
    key = ".".join(path_components)
    endpoint = f"{BASE_URL}/data/{FLOW_ID}/{key}"
    source_url = endpoint + "?" + urllib.parse.urlencode({"startPeriod": start_period, "endPeriod": end_period})
    root, network = _request_xml(source_url, DATA_ACCEPT, DATA_LIMIT, "UNICEF SDMX data response", timeout)
    requested_native_codes = dict(zip(REQUIRED_SERIES_KEY_COMPONENTS, path_components))
    observations = _observations(root, requested_native_codes)
    if not observations:
        raise ValueError("UNICEF SDMX response contained no observations for the exact query scope")
    truncated = len(observations) > MAX_STDOUT_OBSERVATIONS
    return {"schema_version": "hda-unicef-sdmx-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "dataflow": {"agency": FLOW_AGENCY, "id": FLOW_ID, "version": FLOW_VERSION}, "request_scope": {"dimension_order": ["REF_AREA", "INDICATOR", "VACCINE", "AGE", "TIME_PERIOD"], "native_codes": {"REF_AREA": ref_area, "INDICATOR": indicator, "VACCINE": vaccine, "AGE": age}, "start_period": start_period, "end_period": end_period}, "lineage": {"source": "UNICEF Data Warehouse", "official_docs": DOCS_URL, "endpoint_url": endpoint, "source_url": source_url, "retrieved_at_utc": _timestamp()}, "network": {"anonymous": True, **network, "observation_count": len(observations)}, "result": {"returned_to_stdout": min(len(observations), MAX_STDOUT_OBSERVATIONS), "truncated_for_stdout": truncated, "observations": observations[:MAX_STDOUT_OBSERVATIONS]}, "semantic_boundary": ["Values and native qualifiers are exposed mechanically; HDA does not interpret reporting periods, publication or collection dates, denominators, geography equivalence, or indicator comparability."]}


# NUTRITION uses REF_AREA.INDICATOR.SEX.AGE.WEALTH_QUINTILE.RESIDENCE.
# MATERNAL_EDU_LVL.HEAD_OF_HOUSE via /data/UNICEF,NUTRITION,1.0/<key>.
def query_nutrition(ref_area: str, indicator: str, sex: str, age: str, wealth_quintile: str, residence: str, maternal_edu_lvl: str, head_of_house: str, start_period: str, end_period: str, timeout: float) -> dict[str, Any]:
    path_components = [ref_area, indicator, sex, age, wealth_quintile, residence, maternal_edu_lvl, head_of_house]
    if any(not item or any(mark in item for mark in ".+/ ?&#%") for item in path_components):
        raise ValueError("native path codes must be non-empty single SDMX key components")
    periods = [start_period, end_period]
    if any(not item or any(mark in item for mark in ".+/ ?&#") for item in periods):
        raise ValueError("native codes and periods must be non-empty single SDMX key components")
    key = ".".join(path_components)
    endpoint = f"{BASE_URL}/data/{FLOW_AGENCY},{NUTRITION_FLOW_ID},{FLOW_VERSION}/{key}"
    source_url = endpoint + "?" + urllib.parse.urlencode({"startPeriod": start_period, "endPeriod": end_period})
    root, network = _request_xml(source_url, DATA_ACCEPT, DATA_LIMIT, "UNICEF SDMX data response", timeout)
    requested_native_codes = dict(zip(NUTRITION_SERIES_KEY_COMPONENTS, path_components))
    observations = _observations(root, requested_native_codes, NUTRITION_SERIES_KEY_COMPONENTS)
    if not observations:
        raise ValueError("UNICEF SDMX response contained no observations for the exact query scope")
    truncated = len(observations) > MAX_STDOUT_OBSERVATIONS
    return {"schema_version": "hda-unicef-sdmx-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "dataflow": {"agency": FLOW_AGENCY, "id": NUTRITION_FLOW_ID, "version": FLOW_VERSION}, "request_scope": {"dimension_order": [*NUTRITION_SERIES_KEY_COMPONENTS, "TIME_PERIOD"], "native_codes": requested_native_codes, "start_period": start_period, "end_period": end_period}, "lineage": {"source": "UNICEF Data Warehouse", "official_docs": DOCS_URL, "endpoint_url": endpoint, "source_url": source_url, "retrieved_at_utc": _timestamp()}, "network": {"anonymous": True, **network, "observation_count": len(observations)}, "result": {"returned_to_stdout": min(len(observations), MAX_STDOUT_OBSERVATIONS), "truncated_for_stdout": truncated, "observations": observations[:MAX_STDOUT_OBSERVATIONS]}, "semantic_boundary": ["Values and native qualifiers are exposed mechanically; HDA does not interpret reporting periods, publication or collection dates, denominators, geography equivalence, or indicator comparability."]}


# CME uses REF_AREA.INDICATOR.SEX.WEALTH_QUINTILE via
# /data/UNICEF,CME,1.0/<key>.
def query_cme(ref_area: str, indicator: str, sex: str, wealth_quintile: str, start_period: str, end_period: str, timeout: float) -> dict[str, Any]:
    path_components = [ref_area, indicator, sex, wealth_quintile]
    if any(not item or any(mark in item for mark in ".+/ ?&#%") for item in path_components):
        raise ValueError("native path codes must be non-empty single SDMX key components")
    periods = [start_period, end_period]
    if any(not item or any(mark in item for mark in ".+/ ?&#") for item in periods):
        raise ValueError("native codes and periods must be non-empty single SDMX key components")
    key = ".".join(path_components)
    endpoint = f"{BASE_URL}/data/{FLOW_AGENCY},{CME_FLOW_ID},{FLOW_VERSION}/{key}"
    source_url = endpoint + "?" + urllib.parse.urlencode({"startPeriod": start_period, "endPeriod": end_period})
    root, network = _request_xml(source_url, DATA_ACCEPT, DATA_LIMIT, "UNICEF SDMX data response", timeout)
    requested_native_codes = dict(zip(CME_SERIES_KEY_COMPONENTS, path_components))
    observations = _observations(root, requested_native_codes, CME_SERIES_KEY_COMPONENTS)
    if not observations:
        raise ValueError("UNICEF SDMX response contained no observations for the exact query scope")
    truncated = len(observations) > MAX_STDOUT_OBSERVATIONS
    return {"schema_version": "hda-unicef-sdmx-result/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "dataflow": {"agency": FLOW_AGENCY, "id": CME_FLOW_ID, "version": FLOW_VERSION}, "request_scope": {"dimension_order": [*CME_SERIES_KEY_COMPONENTS, "TIME_PERIOD"], "native_codes": requested_native_codes, "start_period": start_period, "end_period": end_period}, "lineage": {"source": "UNICEF Data Warehouse", "official_docs": DOCS_URL, "endpoint_url": endpoint, "source_url": source_url, "retrieved_at_utc": _timestamp()}, "network": {"anonymous": True, **network, "observation_count": len(observations)}, "result": {"returned_to_stdout": min(len(observations), MAX_STDOUT_OBSERVATIONS), "truncated_for_stdout": truncated, "observations": observations[:MAX_STDOUT_OBSERVATIONS]}, "semantic_boundary": ["Values and native qualifiers are exposed mechanically; HDA does not interpret reporting periods, publication or collection dates, denominators, geography equivalence, or indicator comparability."]}


# Query results first window extracted observations to at most 20; then the
# serialized UTF-8 JSON (including newline) must fit the stdout ceiling,
# 256 KiB by default. An oversized normal result is replaced by metadata only:
# request scope, provenance, response-local count and omission diagnostics,
# not normal observations or discovered structure. Discovery has no observation
# count. If the fallback envelope also exceeds the ceiling, the operation fails.
def _stdout_bytes(result: dict[str, Any], limit: int) -> bytes:
    normal = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(normal) <= limit: return normal
    envelope = {"schema_version": "hda-output-limit-envelope/v1", "source_id": SOURCE_ID, "interface_id": INTERFACE_ID, "request_scope": result.get("request_scope", {"operation": "discover", "dataflow": f"{FLOW_AGENCY}:{FLOW_ID}({FLOW_VERSION})"}), "provenance": {"lineage": result["lineage"], "network": result["network"]}, "counts": {"observation_count": result["network"].get("observation_count")}, "stdout_bytes_limit": limit, "normal_serialized_bytes": len(normal), "output_limit_exceeded": True, "data_omitted": True, "reason": "Publisher records/data were omitted because the serialized model-facing stdout limit was exceeded."}
    emitted = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(emitted) > limit: raise ValueError("output-limit envelope exceeds stdout byte limit")
    return emitted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="inspect the selected live dataflow and DSD")
    q = sub.add_parser("query", help="run one exact bounded native-code observation query")
    q.add_argument("--ref-area", required=True); q.add_argument("--indicator", required=True); q.add_argument("--vaccine", required=True); q.add_argument("--age", required=True); q.add_argument("--start-period", required=True); q.add_argument("--end-period", required=True)
    nutrition = sub.add_parser("query-nutrition", help="run one exact bounded NUTRITION native-code query")
    for component in NUTRITION_SERIES_KEY_COMPONENTS:
        nutrition.add_argument("--" + component.lower().replace("_", "-"), required=True)
    nutrition.add_argument("--start-period", required=True)
    nutrition.add_argument("--end-period", required=True)
    cme = sub.add_parser("query-cme", help="run one exact bounded CME native-code query")
    for component in CME_SERIES_KEY_COMPONENTS:
        cme.add_argument("--" + component.lower().replace("_", "-"), required=True)
    cme.add_argument("--start-period", required=True)
    cme.add_argument("--end-period", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--stdout-bytes-limit", type=int, default=DEFAULT_STDOUT_BYTES_LIMIT, help=f"serialized UTF-8 stdout ceiling including newline; deliberate increase only (default: {DEFAULT_STDOUT_BYTES_LIMIT} bytes)")
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0: raise ValueError("timeout must be positive")
        if args.stdout_bytes_limit < DEFAULT_STDOUT_BYTES_LIMIT: raise ValueError(f"--stdout-bytes-limit must be at least the default {DEFAULT_STDOUT_BYTES_LIMIT}")
        if args.command == "discover":
            result = discover(args.timeout)
        elif args.command == "query-cme":
            result = query_cme(args.ref_area, args.indicator, args.sex, args.wealth_quintile, args.start_period, args.end_period, args.timeout)
        elif args.command == "query-nutrition":
            result = query_nutrition(args.ref_area, args.indicator, args.sex, args.age, args.wealth_quintile, args.residence, args.maternal_edu_lvl, args.head_of_house, args.start_period, args.end_period, args.timeout)
        else:
            result = query(args.ref_area, args.indicator, args.vaccine, args.age, args.start_period, args.end_period, args.timeout)
        sys.stdout.buffer.write(_stdout_bytes(result, args.stdout_bytes_limit)); return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        parser.exit(1, f"HDA UNICEF SDMX operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
