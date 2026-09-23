# Delhi Live Bus Traffic

Live DTC bus GPS → road-snapped breadcrumbs → speed-coloured traffic map.

```
uvicorn backend.main:app --reload      # run from the project root
# then open http://127.0.0.1:8000
```

## Layout

```
backend/
  main.py        FastAPI app + tracker loop (parallel polling, jitter filter, OSRM snapping)
  routes.json    <-- the only file you edit to add/remove routes
frontend/
  index.html     Leaflet dashboard (canvas renderer, incremental updates, animated buses)
tools/
  discover_routes.py     resolve a bus number -> live route UID and append to routes.json
  download_delhi_osm.py  download the Delhi road network from Overpass into data/
data/, cache/    OSM road network + stop data from the original step1-4 scripts
step2..4_*.py    original one-shot scripts (OSM download, snapshot fetch, folium map)
backup_*/        previous versions of main.py / index.html
```

## Deploy on Render

The repo has a `render.yaml` blueprint, so no manual service config is needed.

1. **Push to GitHub** (see below).
2. Render dashboard → **New → Blueprint** → connect
   `github.com/tiwarymadhav-bot/Traffic_Rt` → Apply.
   It reads `render.yaml`: Python runtime, free plan, Singapore region,
   `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`, health check on
   `/api/health`.
3. First deploy takes 2–3 min. The dashboard is then at
   `https://<service>.onrender.com/`.

### Free-plan notes

- Render free sleeps after ~15 min with no traffic, and a sleeping service
  stops tracking — every painted trail is lost on wake. The app pings its own
  `/api/health` every 10 min (`KEEP_ALIVE_SEC`, using `RENDER_EXTERNAL_URL`
  which Render injects) to stay awake. For a second line of defence, point
  cron-job.org or UptimeRobot at `https://<service>.onrender.com/api/health`
  every 10 minutes.
- 512 MB RAM, so the blueprint caps `MAX_SEGMENTS=6000` and `SEGMENT_TTL=10800`
  (3 h) instead of the local 15000 / 6 h. Raise them on a paid plan.
- The OSRM demo server rate-limits by IP; from a shared cloud IP expect more
  fallbacks to the straight chord than you see locally. For production,
  self-host OSRM and point `OSRM_MATCH_URL` / `OSRM_ROUTE_URL` at it.
- `data/` and `cache/` are gitignored, so the "OSM roads" overlay is empty on
  Render until `tools/download_delhi_osm.py` is run there (or a disk is
  attached). Everything else works without it.

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PORT` | 8000 | set by Render |
| `POLL_INTERVAL` | 15 | seconds between DTC sweeps (overrides routes.json) |
| `MAX_SEGMENTS` | 15000 | painted-road cap |
| `SEGMENT_TTL` | 21600 | seconds a trail stays on the map |
| `DTC_CONCURRENCY` | 8 | parallel DTC requests |
| `OSRM_CONCURRENCY` | 6 | parallel OSRM requests |
| `KEEP_ALIVE_URL` | `$RENDER_EXTERNAL_URL/api/health` | self-ping target; empty disables |
| `KEEP_ALIVE_SEC` | 600 | self-ping interval |

## Push to GitHub

The repo is already initialised and committed locally. From the project folder:

```bash
git remote add origin https://github.com/tiwarymadhav-bot/Traffic_Rt.git
git branch -M main
git push -u origin main
```

If the remote already has commits, use `git push -u origin main --force` only
if you are sure you want to replace them.

## Tracked routes

47 feeds = 24 routes × up/down:

`73 · 85 · 233 · 306 · 307 · 307A · 307A STL · 307B STL · 309 · 391 · 429 · 463 · 469 ·
473 · 534 · 534A · 543 · 543A · 623 · OMS · 0OMS · OMS STL · D-4501 · D-5401`

### Adding a route

```bash
python tools/discover_routes.py 764 502A          # exact bus numbers
python tools/discover_routes.py IMS --contains    # every route whose name contains IMS
python tools/discover_routes.py 764 --dry-run     # look first
```

Then restart the backend, or `curl -X POST http://127.0.0.1:8000/api/reload_routes`.

## How route IDs work

The live API is keyed by an **internal UID**, not the public bus number
(route 85 = UID 2343 up / 2340 down). Resolution chain:

1. `GET /ajax/bus/search/?q=85`  → `25|85 (Anand Vihar ISBT → Punjabi Bagh Terminal)`
2. `GET /bus/search/?id=25`      → page contains `_liveBusRouteUID = '2343'`
3. `POST /api/live/buses/` with `{"route_id": "2343"}` (needs the `csrftoken` cookie
   + `X-CSRFToken` header) → live bus positions, route reported as `85UP`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/traffic_segments?after=<cursor>` | **incremental** — only segments newer than the cursor, plus all live bus positions |
| `GET /api/routes` | tracked routes with live bus counts per direction |
| `GET /api/health` | feed/cycle diagnostics |
| `POST /api/reload_routes` | re-read `routes.json` without a restart |

## Traffic colours

| Speed | Status | Colour |
|---|---|---|
| < 5 km/h | Heavy jam | red |
| 5–11 | Moderate | orange |
| > 11 | Fast | green |

Speed is an exponential moving average (α = 0.45), so colours do not flicker
between polls.

## Noise filtering

A hop is painted only if it survives all of these:

- **≥ 18 m** since the anchor (below that it is GPS shimmer; the anchor is held,
  so skipping leaves no gap).
- **Not wandering.** A standing bus drifts 20–40 m in random directions every
  poll, which clears any distance filter and used to paint a star of crossing
  lines. Over the last 5 raw fixes: net travel ≥ 100 m is always accepted; under
  35 m is always parked; in between, net ÷ path-length must be ≥ 0.6 — real
  travel is straight-ish, wander doubles back on itself.
- **No direction flip.** A hop under 60 m that reverses bearing by more than
  120° is noise, not a U-turn.
- Jumps over 5 km start a fresh track.

## How a trail is painted

**Primary: linear referencing along the route's own line.**

Asking a router "which road is this bus on?" cannot work where a flyover, its
service road and a metro viaduct sit 20–40 m apart while GPS error is 30–50 m —
every candidate inside that radius is a road, so no threshold can reject the
wrong one. That was the cause of every trail artefact in this project.

A bus is not free to be on any road: it runs one fixed published line, and
`data/route_corridors.json` has it. So each fix is **projected onto that line**
and the trail is the slice of the line between two projections:

- the trail is exactly the route **by construction** — a service road, a wrong
  carriageway, a cut corner or a line across a block are simply not on it;
- distance is measured **along the road**, so the speed is better than a
  straight-line estimate;
- a fix more than `MAX_OFFROUTE_M` (150 m) from the line paints nothing;
- if the chainage goes *backwards*, the bus is running the other way: the
  opposite direction's line is tried and the bus is re-labelled. This corrects
  the feed, which returns the same bus on both directions;
- the search is windowed around the previous position, so a ring route (OMS
  passes the same junction twice) cannot snap to the wrong lap;
- no router is involved at all — no rate limits, no latency, no demo-server
  flakiness.

Measured with ±45 m of simulated GPS wander: every painted point sat **0.00 m**
from the true road, speeds came out 12–14 km/h against a true 12, and
consecutive segments had zero gaps.

**Fallback: OSRM map matching**, used only for a route with no published line
(currently `307A STL|down`). That path keeps the older machinery — `/match` over
a rolling window, `/route` for long hops, the straight chord up to 350 m, the
corridor grid check, and the post-paint audit.

## Route corridors (the data behind both paths)

Every tracked route has one fixed path, and the DTC site publishes it: each
route page carries `_mapData.route_coords`, the full LineString of that
route/direction (~400 points for route 85). `tools/build_route_corridors.py`
collects all of them into `data/route_corridors.json`.

```bash
python tools/build_route_corridors.py             # all routes
python tools/build_route_corridors.py --only 463 473
python tools/build_route_corridors.py --refresh   # re-resolve cached sids
```

Then commit `data/route_corridors.json` (it is deliberately NOT gitignored) so
Render gets it, and redeploy. Startup logs confirm it:

```
[corridor] 47 corridors loaded (18420 points, 41003 cells)
```

The backend densifies each corridor to 20 m spacing, buckets it into a ~50 m
grid, and requires that **80 % of a painted path's points fall in that grid or
its 3x3 neighbourhood** (so roughly 50-100 m of tolerance). The path is sampled
every 25 m first — a straight chord has only two points, both on the route, so
without sampling any line cutting across a block between two on-route fixes
would pass. A matched path that
fails is retried as `/route`, then as the chord — and if the chord is off
corridor too, nothing is painted and `hops_dropped` counts it.

This is the only check that knows a route-463 bus does not belong on the
DND-KMP Expressway: it is not on route 463 at all. No bow or ratio threshold can
work that out, because a wrong road is still a road. Routes with no corridor in
the file are simply left unconstrained.

## Map matching

Road snapping uses OSRM's **/match** service (hidden-Markov map matching) over
a rolling window of each bus's last 5 fixes, with per-fix radiuses and
timestamps — not point-to-point routing, which is what used to send trails on
rectangular detours through side lanes. Guards before a matched path is
accepted: leg length ≤ 1.8× the straight chord (2.5× above 300 m), and maximum
cross-track deviation ≤ max(120 m, 40 % of the chord).

Fallback chain when matching fails or trips a guard:

1. hop > 200 m → plain A→B `/route` (reliable at that distance);
2. otherwise the straight chord, **but only up to 350 m** — a failed long hop is
   left unpainted rather than drawn as a diagonal across blocks and buildings.

Old paint stays intact until newer data arrives (6 h TTL, age fade off by
default).

## Delhi OSM road network

```bash
python tools/download_delhi_osm.py            # 4x4 Overpass tiles, cached
python tools/download_delhi_osm.py --grid 5   # finer tiles if queries time out
python tools/download_delhi_osm.py --major-only
```

Writes `data/delhi_roads_major.geojson` (overlay used by the dashboard's
"OSM roads" button, served at `/api/osm_roads`) and `data/delhi_roads.geojson`
(full drive network).

## Tunables (top of `backend/main.py`)

`POLL_INTERVAL`, `DTC_CONCURRENCY`, `OSRM_CONCURRENCY`, `SEGMENT_TTL`,
`MAX_SEGMENTS`, `MIN_MOVE_M`, `MAX_JUMP_KM`, `MAX_PLAUSIBLE_KMH`, `SPEED_ALPHA`,
`MATCH_WINDOW`, `MATCH_RADIUS_M`, `MAX_LEG_RATIO_SHORT/LONG`, `MAX_CROSS_TRACK_M`,
`CORRIDOR_CELL_M`, `CORRIDOR_STEP_M`, `CORRIDOR_MIN_INSIDE`, `JAM_BELOW_KMH`,
`MAX_OFFROUTE_M`, `FAST_ABOVE_KMH` (most of these are also env vars, so they can be changed on a
running deploy without a code push).

## Diagnostics

```
/api/debug/segments?s=28.40&w=76.83&n=28.91&e=77.36&limit=300&min_bow=80
```

Returns the painted segments in a bounding box with `snapped`, `chord_m`,
`len_m`, `ratio`, `bow_m`, plus the raw GPS fixes of the buses involved and the
bow percentiles of everything in the box. `min_bow` / `min_ratio` filter it
down to suspicious trails; an empty result means the box is clean. `ratio` near
1.0 with a large bow is a real curve; 1.4+ is a detour.
