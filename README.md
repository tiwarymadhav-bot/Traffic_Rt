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

### Stop dwell is not a jam

A bus standing at a stop is doing its job, not sitting in traffic. Left alone,
that dwell lands in the next hop's duration and paints the stretch deep red, so
every bus stop grows a red blob.

`data/route_stops.json` (built by the same tool as the corridors) holds every
stop of every route. Each one is projected onto its route line once at startup,
so the backend knows the chainage of every stop, and which two are the
terminals. Then, when a bus makes no paintable move:

- **at an ordinary stop** (within `STOP_RADIUS_M`, 60 m) - only the first
  `DWELL_GRACE_SEC` (45 s) is *banked* as dwell, and the smoothed speed is left
  alone for that long, so the bus does not fade to red while passengers board.
  When it pulls away, the hop's speed is `distance / (elapsed - dwell)`: the
  speed it did **on the road**;
- **still standing at that stop after the grace period** - a bus does not need
  three minutes to load. The stop itself is jammed, so every second beyond the
  grace counts as congestion exactly like open road, and the stretch paints red.
  This is the case that matters: **a real jam at a stop is still a jam**, only
  the boarding part of it is excused;
- **at a terminal** (first or last stop, within `TERMINAL_RADIUS_M`) - a layover
  between trips, never traffic. The clock simply restarts, so the departure is
  not painted as a phantom jam stretching back to the arrival;
- **anywhere else** - congestion. The clock keeps running and the speed bleeds
  towards zero, which is what paints a real jam red.

One more guard: the moving time can never be shorter than one poll gap, so a bus
that stood for most of the window and pulled away at the end cannot report an
absurd speed.

Without the file nothing breaks - dwell simply counts as jam time, as it did
before.

| situation | painted as |
|---|---|
| boarding 30 s, then 250 m | fast |
| **jam at a stop, 3 min, then 60 m** | **heavy jam** |
| jam on open road, 3 min, then 60 m | heavy jam |
| 20 min terminal layover | nothing (clock restarts) |

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

The same run also writes `data/route_stops.json` - every stop of every route,
used to tell a bus stop apart from a jam (see above).

Then commit **both** files (they are deliberately NOT gitignored) so Render gets
them, and redeploy. Startup logs confirm it:

```
[corridor] 46 corridors loaded (20013 points, 28607 cells); 46 usable as route lines
[stops] 1180 stops placed on 46 route lines (0 too far from their line, ignored)
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

## Plan a trip (`/api/plan`)

Type or click a **From** and a **To**, press *Show traffic*, and the road route
appears coloured by the traffic we have actually measured on it.

The route comes from OSRM. Each ~120 m piece of it is then asked whether one of
our 58 bus corridors passes there and what speed was last seen on it. A piece
with an answer is drawn in its speed colour; a piece without is drawn as a **grey
dashed line** and counted as unknown. The panel states the covered share as a
plain number and a bar:

> **58%** of this route has live traffic from our buses (7.2 km).
> The other 5.2 km is drawn grey - we have no data there, so no colour.

That honesty is the point. These 58 routes cover a useful slice of Delhi's
arterial roads and nothing else, so most trips have stretches we know nothing
about. Colouring them by guesswork would make the map look better and be worth
less. For the same reason the travel time is reported twice - the router's own
estimate, and the time implied by the speeds actually measured - rather than
blended into one number that hides which is which.

The panel also lists the jams found on the measured part (touching pieces merged
into one jam) and every tracked bus sitting on that road right now.

Place names are resolved through OpenStreetMap's Nominatim via `/api/geocode`,
biased to the Delhi bounding box. If it is unreachable the panel says so; points
can always be set by clicking the map instead.

## When is the next bus? (`/api/eta`)

The dashboard's **My location** button asks the browser where you are, then the
panel lists the stops around you and the buses on their way to each.

The arrival time is not distance divided by an average. Every painted hop already
records where it happened along the route and how fast the bus was there, so the
road *ahead* of a bus is walked in 250 m pieces and each piece is timed at the
speed most recently measured near it, plus ~20 s for every stop in between. A jam
sitting between the bus and your stop therefore pushes the time out instead of
being averaged away. A bus that has already passed your stop is not listed.

Every arrival carries how much of that stretch was actually measured:

| badge | meaning |
|---|---|
| `live` | 60 %+ of the road ahead has a recent speed measurement |
| `partial` | 20–60 % |
| `estimate` | almost nothing measured - the bus's own speed, or a default |

The badge is shown as it comes. Nothing is presented as measured when it was
guessed.

```
GET /api/eta?lat=28.5721&lon=77.2601&radius=800
```

Stop **names** come from `data/route_stops.json`, which needs the third field in
each record (`[lon, lat, name]`). A file built before that existed still works -
the panel just shows "Unnamed stop" - so rebuild it once with:

```bash
python tools/build_route_corridors.py --stops-only
```

Tunables: `ETA_FALLBACK_KMH` (16), `ETA_STOP_DWELL_S` (20), `SPEED_TTL` (1800 s).

## Keeping the corridors honest

The trail is painted straight along the corridor, so a trail on a flyover means
the *corridor* is on the flyover - the matcher is doing its job with a wrong map.
Lines get there two ways: the published DTC line takes the flyover, or the gap
smoothing above routed a long jump through OSRM, whose car profile always prefers
the fast road (the flyover) over the service road the bus actually uses.

The stops settle it. A bus must serve its stops and **a flyover has none**, so a
stretch of line that has run away from its own stops is wrong by definition.

```bash
python tools/audit_corridors.py                 # report only, changes nothing
python tools/audit_corridors.py --json data/corridor_audit.json
python tools/repair_corridors.py --dry-run      # what would change
python tools/repair_corridors.py                # rebuild the bad stretches
```

`audit_corridors.py` projects every stop onto its own line and measures the
offset. One stop 30 m out is ordinary noise; several *consecutive* stops out, all
on the same side, is a line that left the road. A lone off stop is reported only
when its neighbours are drifting too - a wrong line drags the whole sequence off,
whereas `306|up` stop #14 sits 43 m out between neighbours at 0 m and 4 m, which
says the stop coordinate is wrong, not the corridor. Without that rule the audit
reported twice as many places and the repair asked a router for 18 km of detour
to replace 455 m of road. Findings are grouped by place,
because one bad junction shows up once per route through it, and each place comes
with an OpenStreetMap link to eyeball.

The audit leads with the only question that really decides whether a corridor is
right: **does the line pass every one of its stops, in order?** A bus is defined
by the stops it serves, so a line that reaches them all is correct whether it does
so on a flyover or under one. The report names each route that misses a stop
(further than `--serves`, 40 m) or reaches a later stop before an earlier one,
with the median and p90 offset beside it so a single odd stop is easy to tell
apart from a line that has genuinely wandered. Ring routes such as `OMS` show
harmless out-of-order counts, because a stop passed twice can project onto the
wrong lap.

The stop test has one blind spot, and it is the important one: **a flyover has
no stops at all**, so the nearest stops sit comfortably before and after it, each
close to the line, and nothing looks wrong. So there is a second test, on road
class. A DTC bus does not run on the DND Flyway, over the Ashram Flyover or along
the Delhi-Meerut Expressway, so a corridor point whose nearest road (within 20 m)
is `motorway` or `motorway_link` is wrong by class alone, whatever the stops say.
That test uses `data/delhi_roads_major.geojson`; without the file it is skipped.

`repair_corridors.py` rebuilds only the flagged stretches, and rebuilds them
**through the stops** - the stops go to OSRM as via-points with a 30 m snapping
radius, so the returned path has to come down to the road that serves them. Every
rebuild is then checked before it is kept: it must bring its worst stop within
20 m, must not be more than 1.6x the length it replaced, and must start and end
where the old piece did. Anything else is discarded and the original line stays.
A motorway stretch is rebuilt **without any router at all**. The public OSRM
server refuses `exclude=motorway` - it answers *"Exclude flag combination is not
supported"* - so it cannot be told to keep a bus off a flyover. But
`data/delhi_roads_major.geojson` is an osmnx edge export: every feature carries
`u`/`v` node ids, a length and a oneway flag, so it already **is** a graph.
`tools/offline_router.py` builds that graph with the motorway edges left out and
runs Dijkstra over what remains, so the exclusion is structural rather than a
request a server may decline. Only 140 of 7306 edges are motorway and the rest
stay one connected component, so nothing useful is lost.

Two details make it work on real Delhi roads. Main roads are one-way pairs, so
each end is snapped using the direction the bus is travelling there - the nearest
edge is often the opposite carriageway, from which the graph may not reach the
destination at all. And every edge within reach of an end is offered to Dijkstra
as a way in or out, priced by distance and direction, because the major-roads
extract has dangling one-way stubs where a road continues into a class that was
not exported; snap to one of those alone and the search goes nowhere.

Measured on the live corridors: eight stretches across `469`, `543`, `543A` and
`OMS` came off the Ashram Flyover with 0 m of motorway left and the length
within 1 % of what they replaced.

A repair can improve a corridor or do nothing - never make it worse.

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
`MAX_OFFROUTE_M`, `FAST_ABOVE_KMH`, `STOP_RADIUS_M`, `DWELL_GRACE_SEC`,
`TERMINAL_RADIUS_M`
(most of these are also env vars, so they can be changed on a
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
