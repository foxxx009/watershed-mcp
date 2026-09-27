# watershed-mcp

**Live demo:** https://foxxx009.github.io/watershed-mcp/ (browser-based, hits the same public APIs the server tools call; plus a real MCP stdio session transcript.)

**MCP server giving AI agents live access to bioregional river and watershed
data** — stream flow, water quality and basin health from public government
APIs (USGS NWIS + EPA Water Quality Portal).

Built for owockibot bounty **#477 ("Watershed MCP Server")** — the live, open
bounty. (An earlier posting, #471, with the same title was cancelled by the
poster; this repo is the deliverable for #477.) Sensor readings conform to the
Ecological Sensor Data JSON Schema Standard from bounty #450.

## Why

Bioregional knowledge commons needs machine-readable watershed data. The
data already exists in public APIs (USGS NWIS real-time gauges, EPA WQP
discrete water-quality results) but there was no MCP server exposing it to
AI agents in a schema-stable way. This repo is that missing piece.

## Tools

| Tool | What it does | Data source |
|---|---|---|
| `get_stream_flow` | Live discharge (ft3/s), gage height, and live water-quality parameters for USGS site numbers | USGS NWIS instantaneous values |
| `get_water_quality` | Recent discrete lab/field results (pH, dissolved oxygen, turbidity, temperature, ...) | EPA Water Quality Portal |
| `assess_basin_health` | Stations in a HUC + latest live readings + WQP results + **EPA compliance score (0-100)** | both |
| `find_stations` | Expanded site metadata (name, coordinates, drainage area, HUC) | USGS NWIS site file |
| `get_sensor_schema` | The bounty-#450 sensor JSON Schema used for all readings | this repo |

## Quick start

```bash
python watershed_mcp.py stdio          # MCP JSON-RPC 2.0 over stdio
python watershed_mcp.py http 8080      # HTTP gateway + dashboard (demo)

# Claude Desktop / any MCP client config:
# {"mcpServers": {"watershed": {"command": "python",
#                               "args": ["/path/to/watershed_mcp.py"]}}}
```

Zero third-party dependencies — Python 3.9+ standard library only.

## Example session (real output, 2026-09-26)

```json
{"tool": "get_stream_flow", "sites": "09380000",
 "result": {"count": 320, "readings": [
   {"sensor_id": "USGS-09380000", "source": "USGS NWIS instantaneous values",
    "parameter": "streamflow", "value": 10000.0, "unit": "ft3/s",
    "timestamp": "2026-09-24T21:15:00.000-07:00",
    "location": {"lat": 36.86433333, "lng": -111.58787222},
    "site_name": "COLORADO RIVER AT LEES FERRY, AZ"}]}}

{"tool": "assess_basin_health", "siteids": ["09380000"],
 "result": {"live_readings": {"count": 384},
            "wqp_water_quality": {"count": 144},
            "epa_compliance": {
              "checked": 8, "violations": 0, "compliance_score": 100.0,
              "per_parameter": {
                "dissolved_oxygen": {"n": 4, "bad": 0},
                "ph": {"n": 4, "bad": 0}}}}}
```

## EPA compliance score

Screening benchmarks (national defaults, documented per-parameter in
`watershed_mcp.py::BENCHMARKS`):

- **pH** must be 6.5–8.5 std units (EPA secondary MCL / ambient criteria)
- **Dissolved oxygen** ≥ 5.0 mg/L (EPA warm-water aquatic-life minimum)
- **Turbidity** ≤ 50 FNU (ambient-stream screening threshold)

`compliance_score = 100 × (checked − violations) / checked`, `null` when no
parameter was checkable. This is a screening rank, not a regulatory
determination.

## Sensor schema (#450 alignment)

Every reading carries the six required fields: `timestamp`, `location`
(lat/lng), `unit`, `value`, `sensor_id`, `source`. Full schema:
[`sensor_schema.json`](sensor_schema.json). Example files in `examples/`
are validated by `validate_examples.py` (run: `python validate_examples.py`).

## WQP integration notes (hard-won)

The WQP retired JSON responses (`mimeType=json` → HTTP 406). The supported
programmatic route is **TSV inside a zip**: `mimeType=tsv&zip=yes`. This
server handles unzip/parse transparently. USGS endpoints retry with backoff
on 429/503.

## HTTP demo endpoints

- `/` — dashboard with live Lees Ferry flow + latest WQP results
- `/healthz` — liveness
- `/tools/get_stream_flow?sites=09380000`
- `/tools/get_water_quality?siteids=USGS-09380000`
- `/tools/assess_basin_health?huc_code=140700061105`
- `/tools/get_sensor_schema`

## License

MIT. Data itself: USGS/EPA public domain. Not affiliated with USGS or EPA.
