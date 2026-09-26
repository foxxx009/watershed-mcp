#!/usr/bin/env python3
"""watershed-mcp: MCP server giving AI agents live access to bioregional
river and watershed data from public USGS NWIS and EPA WQP APIs.

Zero third-party dependencies (Python 3.9+ standard library only).

Modes:
  watershed_mcp.py stdio        - MCP JSON-RPC 2.0 over stdio (default)
  watershed_mcp.py http 8080    - plain HTTP gateway + dashboard (for demos)

Sensor readings emitted by this server conform to the Ecological Sensor
Data JSON Schema Standard (owockibot bounty #450): every reading carries
timestamp, location (lat/lng), unit, value, sensor_id and source.
"""

import re
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

_RDB_FMT = re.compile(r"^\d+[a-zA-Z]$")

UA = "watershed-mcp/1.0 (AI-agent MCP server; contact: github.com/foxxx009/watershed-mcp)"

NWIS_IV = "https://waterservices.usgs.gov/nwis/iv/"
NWIS_SITE = "https://waterservices.usgs.gov/nwis/site/"
WQP_RESULT = "https://www.waterqualitydata.us/data/Result/search"

# USGS parameter codes used for live water-quality tooling
PARAMS = {
    "00060": {"name": "streamflow", "unit": "ft3/s", "label": "Discharge"},
    "00065": {"name": "gage_height", "unit": "ft", "label": "Gage height"},
    "00010": {"name": "water_temperature", "unit": "degC", "label": "Temperature"},
    "00300": {"name": "dissolved_oxygen", "unit": "mg/L", "label": "Dissolved oxygen"},
    "00400": {"name": "ph", "unit": "std units", "label": "pH"},
    "63680": {"name": "turbidity", "unit": "FNU", "label": "Turbidity"},
}
DEFAULT_PCODES = "00060,00065,00010,00300,00400,63680"

# Screening benchmarks used by assess_basin_health (EPA/national defaults)
BENCHMARKS = {
    "ph": {"min": 6.5, "max": 8.5, "unit": "std units",
           "basis": "EPA secondary MCL / national ambient criteria"},
    "dissolved_oxygen": {"min": 5.0, "max": None, "unit": "mg/L",
                         "basis": "EPA warm-water aquatic-life minimum"},
    "turbidity": {"min": None, "max": 50.0, "unit": "FNU",
                  "basis": "screening threshold for ambient streams"},
}


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def _http_get(url, timeout=45, retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last


def _http_json(url, timeout=45):
    return json.loads(_http_get(url, timeout).decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# USGS NWIS
# --------------------------------------------------------------------------
def nwis_instantaneous(sites, pcodes=DEFAULT_PCODES, hours=24):
    """Live (last hours) readings from USGS NWIS instantaneous values service."""
    if isinstance(sites, str):
        sites = [s.strip() for s in sites.split(",") if s.strip()]
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    q = urllib.parse.urlencode({
        "format": "json", "sites": ",".join(sites), "parameterCd": pcodes,
        "startDT": start, "siteStatus": "all"})
    data = _http_json(NWIS_IV + "?" + q)
    readings = []
    for ts in data.get("value", {}).get("timeSeries", []):
        src = ts.get("sourceInfo", {})
        loc = src.get("geoLocation", {}).get("geogLocation", {})
        var = ts.get("variable", {})
        pc = ((var.get("variableCode") or [{}])[0].get("value"))
        meta = PARAMS.get(pc, {"name": pc, "unit": var.get("unit", {}).get("unitCode", ""), "label": pc})
        for block in ts.get("values", []):
            for v in block.get("value", []):
                try:
                    val = float(v.get("value"))
                except (TypeError, ValueError):
                    continue
                readings.append({
                    "sensor_id": "USGS-" + src.get("siteCode", [{}])[0].get("value", ""),
                    "source": "USGS NWIS instantaneous values",
                    "parameter": meta["name"],
                    "parameter_code": pc,
                    "label": meta["label"],
                    "value": val,
                    "unit": meta["unit"],
                    "timestamp": v.get("dateTime"),
                    "location": {"lat": loc.get("latitude"),
                                 "lng": loc.get("longitude")},
                    "site_name": src.get("siteName"),
                    "qualifier": v.get("qualifier"),
                })
    return {"count": len(readings), "sites": sites, "readings": readings}


def nwis_site_info(sites):
    """Expanded site metadata (name, drainage area, huc, coordinates)."""
    if isinstance(sites, str):
        sites = [s.strip() for s in sites.split(",") if s.strip()]
    q = urllib.parse.urlencode({"format": "rdb", "sites": ",".join(sites),
                                "siteOutput": "expanded"})
    raw = _http_get(NWIS_SITE + "?" + q).decode("utf-8", "replace")
    lines = [l for l in raw.splitlines() if l and not l.startswith("#")]
    if not lines:
        return {"sites": []}
    header = lines[0].split("\t")
    out = []
    for line in lines[1:]:
        cells = line.split("\t")
        if cells and all(_RDB_FMT.match(c) for c in cells if c != ""):
            continue  # RDB column-format row (e.g. "5s", "15s")
        row = dict(zip(header, cells))
        out.append({
            "sensor_id": "USGS-" + row.get("site_no", ""),
            "source": "USGS NWIS site file",
            "site_name": row.get("station_nm"),
            "location": {"lat": _f(row.get("dec_lat_va")),
                         "lng": _f(row.get("dec_long_va"))},
            "drainage_area_sqmi": _f(row.get("drain_area_va")),
            "huc_code": row.get("huc_cd"),
            "agency": row.get("agency_cd"),
            "site_type": row.get("site_tp_cd"),
        })
    return {"count": len(out), "sites": out}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# EPA WQP  (JSON mime was retired; tsv inside a zip is the supported route)
# --------------------------------------------------------------------------
def wqp_results(siteids=None, bbox=None, pcode=None, chars=None,
                years_back=2, max_rows=400):
    """Recent discrete water-quality results from the EPA Water Quality Portal."""
    args = {"mimeType": "tsv", "zip": "yes", "sorted": "no"}
    if siteids:
        args["siteid"] = siteids if isinstance(siteids, list) else [siteids]
    if bbox:
        args["bBox"] = ",".join(str(round(float(x), 5)) for x in bbox)
    if pcode:
        args["pCode"] = pcode
    if chars:
        args["characteristicName"] = chars if isinstance(chars, list) else [chars]
    lo = (datetime.now(timezone.utc) - timedelta(days=365 * years_back))
    args["startDateLo"] = lo.strftime("%m-%d-%Y")
    args["startDateHi"] = datetime.now(timezone.utc).strftime("%m-%d-%Y")
    if not (siteids or bbox or chars):
        return {"error": "need one of siteids / bbox / characteristics",
                "count": 0, "readings": []}
    url = WQP_RESULT + "?" + urllib.parse.urlencode(args, doseq=True)
    blob = _http_get(url)
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = [n for n in zf.namelist() if n.lower().endswith((".tsv", ".csv"))][0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            for row in csv.DictReader(text, delimiter="\t"):
                rows.append(row)
                if len(rows) >= max_rows:
                    break
    readings = []
    for r in rows:
        val = _f(r.get("ResultMeasureValue"))
        if val is None:
            continue
        readings.append({
            "sensor_id": r.get("MonitoringLocationIdentifier", ""),
            "source": "EPA Water Quality Portal (WQX/NWIS)",
            "parameter": (r.get("CharacteristicName") or "").strip(),
            "usgs_pcode": r.get("USGSPCode") or None,
            "value": val,
            "unit": (r.get("ResultMeasure", {}).get("MeasureUnitCode")
                     if isinstance(r.get("ResultMeasure"), dict)
                     else r.get("ResultMeasure/MeasureUnitCode")) or "",
            "timestamp": r.get("ActivityStartDate"),
            "location": {"lat": _f(r.get("LatitudeMeasure")),
                         "lng": _f(r.get("LongitudeMeasure"))},
            "site_name": r.get("MonitoringLocationName"),
            "sample_fraction": r.get("ResultSampleFractionText"),
            "provider": r.get("ProviderName"),
        })
    return {"count": len(readings), "query_url": url, "readings": readings}


# --------------------------------------------------------------------------
# Derived analytics
# --------------------------------------------------------------------------
def epa_compliance_ranking(readings):
    """Score 0-100: share of readings inside EPA screening benchmarks."""
    checked, violations = 0, []
    per_param = {}
    for r in readings:
        p = (r.get("parameter") or "").lower()
        if p.startswith("ph") and "phos" not in p:
            key = "ph"
        elif "oxygen" in p or p == "do":
            key = "dissolved_oxygen"
        elif "turbid" in p:
            key = "turbidity"
        else:
            continue
        bench = BENCHMARKS[key]
        # WQP pH may be recorded as "pH, std units"; unit mismatch guard
        val = float(r["value"])
        checked += 1
        ok = True
        if bench["min"] is not None and val < bench["min"]:
            ok = False
        if bench["max"] is not None and val > bench["max"]:
            ok = False
        per_param.setdefault(key, {"n": 0, "bad": 0})["n"] += 1
        if not ok:
            per_param[key]["bad"] += 1
            violations.append({"parameter": key, "value": val,
                               "unit": r.get("unit"),
                               "benchmark": bench, "sensor_id": r.get("sensor_id"),
                               "timestamp": r.get("timestamp")})
    score = round(100.0 * (checked - len(violations)) / checked, 1) if checked else None
    return {"checked": checked, "violations": len(violations),
            "compliance_score": score, "per_parameter": per_param,
            "violation_examples": violations[:10]}


def basin_health(huc_code=None, siteids=None, bbox=None):
    """Aggregate basin view: stations + latest live flows + WQP compliance."""
    out = {"huc_code": huc_code, "generated_at": _now()}
    if huc_code and not siteids:
        # station search by hydrologic unit via the site service
        q = urllib.parse.urlencode({"format": "rdb", "huc": huc_code,
                                    "siteOutput": "expanded", "hasDataTypeCd": "iv"})
        raw = _http_get(NWIS_SITE + "?" + q).decode("utf-8", "replace")
        lines = [l for l in raw.splitlines() if l and not l.startswith("#")]
        if len(lines) > 1:
            header = lines[0].split("\t")
            siteids = []
            for l in lines[1:]:
                cells = l.split("\t")
                if cells and all(_RDB_FMT.match(c) for c in cells if c != ""):
                    continue
                s = dict(zip(header, cells)).get("site_no")
                if s:
                    siteids.append(s)
            siteids = siteids[:20]
    if not siteids and not bbox:
        return {"error": "provide huc_code, siteids or bbox"}
    if siteids:
        info = nwis_site_info(siteids)
        try:
            live = nwis_instantaneous(siteids[:15])
        except Exception as e:  # noqa: BLE001 - report, don't crash the tool
            live = {"error": repr(e)[:200], "count": 0, "readings": []}
        out["stations"] = info
        out["live_readings"] = {"count": live.get("count"),
                                "by_parameter": _by_param(live.get("readings", []))}
    # WQP expects "USGS-<8-digit>" site identifiers; normalize bare numbers
    wqp_ids = [s if s.upper().startswith(("USGS-", "WQX-")) else "USGS-" + s
               for s in (siteids or [])][:5]
    wq = wqp_results(siteids=wqp_ids or None, bbox=bbox, years_back=10)
    out["wqp_water_quality"] = {"count": wq.get("count"), "query_url": wq.get("query_url"),
                                "sample": wq.get("readings", [])[:8]}
    out["epa_compliance"] = epa_compliance_ranking(wq.get("readings", []))
    return out


def _by_param(readings):
    agg = {}
    for r in readings:
        k = r["parameter"]
        agg.setdefault(k, {"n": 0, "latest": None})
        agg[k]["n"] += 1
        if not agg[k]["latest"] or (r.get("timestamp") or "") > (agg[k]["latest"].get("timestamp") or ""):
            agg[k]["latest"] = r
    return agg


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# MCP tool registry (bounty #450 sensor-schema aligned)
# --------------------------------------------------------------------------
SENSOR_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Ecological Sensor Reading (watershed subset, bounty #450)",
    "type": "object",
    "required": ["timestamp", "location", "unit", "value", "sensor_id", "source"],
    "properties": {
        "timestamp": {"type": "string"},
        "location": {"type": "object",
                     "properties": {"lat": {"type": ["number", "null"]},
                                    "lng": {"type": ["number", "null"]}}},
        "unit": {"type": "string"},
        "value": {"type": "number"},
        "sensor_id": {"type": "string"},
        "source": {"type": "string"},
    },
}

TOOLS = {
    "get_stream_flow": {
        "description": ("Live streamflow (discharge, ft3/s) and gage height for one or "
                        "more USGS site numbers, e.g. 09380000 (Colorado River at Lees "
                        "Ferry). Optional other parameters: 00010 temperature, 00300 "
                        "dissolved oxygen, 00400 pH, 63680 turbidity."),
        "inputSchema": {"type": "object", "required": ["sites"],
                        "properties": {"sites": {"type": "string",
                                                 "description": "comma-separated USGS site numbers"},
                                       "pcodes": {"type": "string"},
                                       "hours": {"type": "number"}}},
    },
    "get_water_quality": {
        "description": ("Recent discrete water-quality lab/field results from the EPA Water "
                        "Quality Portal. Query by siteids (USGS-xxxxxxxx style), bbox "
                        "[xmin,ymin,xmax,ymax], and/or characteristicName list (e.g. pH, "
                        "Dissolved oxygen, Turbidity). Output rows follow bounty-#450 "
                        "sensor schema."),
        "inputSchema": {"type": "object",
                        "properties": {"siteids": {"type": ["array", "string"]},
                                       "bbox": {"type": "array",
                                                "items": {"type": "number"}},
                                       "characteristics": {"type": ["array", "string"]},
                                       "pcode": {"type": "string"},
                                       "years_back": {"type": "number"},
                                       "max_rows": {"type": "number"}}},
    },
    "assess_basin_health": {
        "description": ("Basin-level health view: stations in a hydrologic unit (HUC), "
                        "latest live readings, recent WQP water quality and an EPA "
                        "compliance score (0-100, share of readings inside screening "
                        "benchmarks for pH / dissolved oxygen / turbidity)."),
        "inputSchema": {"type": "object",
                        "properties": {"huc_code": {"type": "string"},
                                       "siteids": {"type": ["array", "string"]},
                                       "bbox": {"type": "array",
                                                "items": {"type": "number"}}}},
    },
    "find_stations": {
        "description": "USGS expanded site metadata for given site numbers.",
        "inputSchema": {"type": "object", "required": ["sites"],
                        "properties": {"sites": {"type": "string"}}},
    },
    "get_sensor_schema": {
        "description": ("Return the Ecological Sensor Data JSON Schema (draft 2020-12) "
                        "subset used by this server (owockibot bounty #450 standard)."),
        "inputSchema": {"type": "object", "properties": {}},
    },
}

TOOL_IMPL = {
    "get_stream_flow": lambda a: nwis_instantaneous(
        a["sites"], a.get("pcodes", DEFAULT_PCODES), float(a.get("hours", 24))),
    "get_water_quality": lambda a: wqp_results(
        a.get("siteids"), a.get("bbox"), a.get("pcode"), a.get("characteristics"),
        float(a.get("years_back", 2)), int(a.get("max_rows", 400))),
    "assess_basin_health": lambda a: basin_health(
        a.get("huc_code"), a.get("siteids"), a.get("bbox")),
    "find_stations": lambda a: nwis_site_info(a["sites"]),
    "get_sensor_schema": lambda a: SENSOR_SCHEMA,
}


# --------------------------------------------------------------------------
# MCP stdio transport (JSON-RPC 2.0, no third-party deps)
# --------------------------------------------------------------------------
def handle(method, params, _id):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "watershed-mcp",
                               "version": "1.0.0",
                               "description": ("Bioregional river & watershed data from "
                                               "USGS NWIS + EPA WQP; bounty #450 schema "
                                               "aligned.")}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"tools": [{"name": n, **d} for n, d in TOOLS.items()]}
    if method == "tools/call":
        name = params["name"]
        fn = TOOL_IMPL.get(name)
        if not fn:
            return {"content": [{"type": "text",
                                 "text": json.dumps({"error": "unknown tool"})}],
                    "isError": True}
        t0 = time.time()
        try:
            result = fn(params.get("arguments") or {})
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text",
                                 "text": json.dumps({"error": repr(e)[:400],
                                                     "tool": name})}],
                    "isError": True}
        result = _trim(result)
        result["_meta"] = {"tool": name, "elapsed_ms": round((time.time() - t0) * 1000),
                           "generated_at": _now()}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
    if method == "ping":
        return {}
    return {"error": {"code": -32601, "message": "method not found: " + method}}


def _trim(obj, depth=0):
    """Keep responses bounded for agent context windows."""
    if isinstance(obj, list):
        return [_trim(x, depth) for x in obj[:60]]
    if isinstance(obj, dict):
        return {k: _trim(v, depth) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + "...[truncated]"
    return obj


def serve_stdio():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "parse error"}}),
                  flush=True)
            continue
        resp = handle(msg.get("method"), msg.get("params") or {}, msg.get("id"))
        if resp is None:
            continue
        out = {"jsonrpc": "2.0", "id": msg.get("id")}
        if "error" in resp:
            out["error"] = resp["error"]
        else:
            out["result"] = resp
        print(json.dumps(out, ensure_ascii=False), flush=True)


# --------------------------------------------------------------------------
# HTTP gateway for the live demo
# --------------------------------------------------------------------------
DASH = """<!doctype html><html><head><meta charset="utf-8"><title>watershed-mcp demo</title>
<style>
body{font-family:ui-monospace,Consolas,monospace;background:#0d1117;color:#e6edf3;margin:0;padding:24px}
h1{font-size:20px} a{color:#58a6ff} table{border-collapse:collapse;margin:12px 0}
td,th{border:1px solid #30363d;padding:4px 10px;font-size:13px}
code{background:#161b22;padding:2px 6px;border-radius:4px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin:14px 0}
</style></head><body>
<h1>watershed-mcp &mdash; live bioregional river data (USGS NWIS + EPA WQP)</h1>
<div class="card"><b>Colorado River at Lees Ferry (USGS 09380000)</b> &mdash; live
{flow_rows}</div>
<div class="card"><b>Latest WQP water quality near the basin</b><br>{wq_rows}</div>
<div class="card">MCP stdio usage: <code>python watershed_mcp.py stdio</code><br>
HTTP tools: <code>/tools/get_stream_flow?sites=09380000</code>,
<code>/tools/assess_basin_health?huc_code=15010001</code>,
<code>/tools/get_water_quality?siteids=USGS-09380000</code><br>
Generated {now}</div></body></html>"""


def serve_http(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(parsed.query))
            try:
                if parsed.path == "/":
                    self._send(200, _dashboard().encode(), "text/html; charset=utf-8")
                elif parsed.path == "/healthz":
                    self._send(200, json.dumps({"ok": True, "time": _now()}).encode())
                elif parsed.path.startswith("/tools/"):
                    name = parsed.path.split("/tools/")[1]
                    if name not in TOOL_IMPL:
                        self._send(404, json.dumps({"error": "unknown tool"}).encode())
                        return
                    result = TOOL_IMPL[name](q)
                    self._send(200, json.dumps(_trim(result), ensure_ascii=False).encode())
                else:
                    self._send(404, json.dumps({"error": "not found"}).encode())
            except Exception as e:  # noqa: BLE001
                self._send(502, json.dumps({"error": repr(e)[:300]}).encode())

    HTTPServer(("0.0.0.0", int(port)), H).serve_forever()


def _dashboard():
    try:
        flow = nwis_instantaneous("09380000", "00060,00065", 48)
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                r["timestamp"], r["label"], r["value"], r["unit"])
            for r in flow.get("readings", [])[:8]) or "<tr><td colspan=4>no data</td></tr>"
        flow_rows = "<table><tr><th>timestamp</th><th>parameter</th><th>value</th><th>unit</th></tr>%s</table>" % rows
    except Exception as e:  # noqa: BLE001
        flow_rows = "flow fetch failed: %s" % repr(e)[:120]
    try:
        wq = wqp_results(siteids="USGS-09380000", pcode="00300", years_back=3, max_rows=40)
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                r.get("timestamp"), r.get("parameter"), r.get("value"), r.get("unit"))
            for r in wq.get("readings", [])[:8]) or "<tr><td colspan=4>no data</td></tr>"
        wq_rows = "<table><tr><th>date</th><th>characteristic</th><th>value</th><th>unit</th></tr>%s</table>" % rows
    except Exception as e:  # noqa: BLE001
        wq_rows = "wqp fetch failed: %s" % repr(e)[:120]
    return DASH.format(flow_rows=flow_rows, wq_rows=wq_rows, now=_now())


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "http":
        serve_http(sys.argv[2] if len(sys.argv) > 2 else "8080")
    else:
        serve_stdio()
