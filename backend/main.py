"""
Delhi Live Bus Traffic - backend
=================================
Polls the DTC internal live-bus API for every route/direction listed in
`routes.json`, turns consecutive GPS fixes into road-snapped "breadcrumb"
segments (via OSRM), colours them by speed and serves them as GeoJSON.

Key differences from the first version
--------------------------------------
* Routes come from routes.json (route + direction + uid), not a hardcoded list.
* All route feeds are fetched CONCURRENTLY (semaphore-bounded) instead of one
  after another - with 42 feeds the sequential version could not finish a cycle
  inside the poll interval.
* Road snapping uses OSRM MAP MATCHING (/match, HMM over the last few fixes)
  instead of routing two noisy points in isolation - that is what used to send
  trails on rectangular detours through side lanes.
* A hop is never dropped: if matching fails or looks wrong it falls back to the
  straight chord, so the painted trail is continuous with no holes.
* GPS jitter filter + exponential moving average on speed => no colour flicker
  and no zig-zag confetti from a parked bus.
* /api/traffic_segments is INCREMENTAL: the client passes ?after=<cursor> and
  only gets segments it has never seen. Full redraws every 5s are gone.
* gzip on responses, coordinates rounded and polylines decimated.

Run:  uvicorn backend.main:app --reload   (from the project root)
"""

import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from typing import Dict, List, Optional, Tuple

import aiohttp
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
ROUTES_FILE = os.path.join(BASE_DIR, "routes.json")
CORRIDOR_FILE = os.path.join(ROOT_DIR, "data", "route_corridors.json")
FRONTEND_INDEX = os.path.join(ROOT_DIR, "frontend", "index.html")

DTC_HOME = "https://www.dtcbusroutes.in/"
DTC_LIVE_URL = "https://www.dtcbusroutes.in/api/live/buses/"
OSRM_MATCH_URL = "https://router.project-osrm.org/match/v1/driving/"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/"


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Tunables can be overridden by environment variables (Render, Docker...)."""
    try:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw else default
    except ValueError:
        return default


# ---- tunables -------------------------------------------------------------
POLL_INTERVAL = _env_int("POLL_INTERVAL", 15)       # seconds between full sweeps
DTC_CONCURRENCY = _env_int("DTC_CONCURRENCY", 8)    # parallel requests to the DTC API
OSRM_CONCURRENCY = _env_int("OSRM_CONCURRENCY", 6)  # parallel requests to OSRM
SEGMENT_TTL = _env_int("SEGMENT_TTL", 21600)        # keep painted road for 6 hours
MAX_SEGMENTS = _env_int("MAX_SEGMENTS", 15000)      # cap so memory / payload stay bounded
MIN_MOVE_M = 18.0           # below this a "move" is GPS noise, not travel
STATIONARY_NET_M = 35.0     # net travel over the raw-fix window below this = parked
MIN_STRAIGHTNESS = 0.6      # net travel / path length; low = wandering on the spot
CLEAR_TRAVEL_M = 100.0      # net travel above this is real movement, never wander
RECENT_FIXES = 5            # raw fixes used for the parked test
RECENT_SEC = 120
BEARING_FLIP_DEG = 120.0    # a short hop that reverses direction is noise
SHORT_NOISE_HOP_M = 60.0    # ...only treated as noise below this length
MAX_JUMP_KM = 5.0           # above this it is a teleport / feed glitch
MAX_PLAUSIBLE_KMH = 90.0    # speed is clamped here, the move is still drawn
SPEED_ALPHA = 0.45          # EMA weight for the new speed sample
BUS_STALE_SEC = 300         # a bus disappears from the map after this
MAX_PATH_POINTS = 80        # decimate long snapped polylines
COORD_DP = 5                # ~1.1 m precision, big payload saving

# ---- map matching --------------------------------------------------------
MATCH_WINDOW = 5            # how many recent fixes feed the HMM matcher
MATCH_WINDOW_SEC = 240      # ignore fixes older than this in the window
MATCH_RADIUS_M = 30         # GPS uncertainty handed to OSRM per fix
MIN_CONFIDENCE = 0.01       # OSRM reports low confidence on curves, so keep this loose
MAX_LEG_RATIO_SHORT = 1.8   # short hop: a detour around the block is obvious
MAX_LEG_RATIO_LONG = 2.5    # long hop: flyovers and loops legitimately add distance
SHORT_HOP_M = 300
# Base allowance for a matched path to stray from its own chord. Measured on a
# 300-segment live sample: p50 = 1 m, p90 = 14 m, p99 = 66 m, max = 78 m. So 80 m
# keeps every healthy trail and still catches a jump onto a parallel road.
MAX_CROSS_TRACK_M = 80
CROSS_TRACK_FRACTION = 0.25  # long hops may bow further (real curves)
CROSS_TRACK_CEILING = 200.0
LONG_HOP_M = 200            # above this, a failed match retries with /route
MAX_CHORD_DRAW_M = 350      # never paint a straight line longer than this

# ---- route corridors ------------------------------------------------------
# Each route has one fixed path, published by the DTC site and collected by
# tools/build_route_corridors.py. A painted trail must stay on its own route's
# corridor. This is the only check that knows bus 463 does not belong on the
# DND-KMP Expressway - no distance threshold can work that out.
# Both are env-tunable so the tolerance can be adjusted on a running deploy.
CORRIDOR_CELL_M = _env_float("CORRIDOR_CELL_M", 50.0)     # grid cell size
CORRIDOR_CELL_DEG = CORRIDOR_CELL_M / 111320.0            # 3x3 cells => ~1-2x this
CORRIDOR_STEP_M = 20.0        # densify the corridor so its cells are contiguous
CORRIDOR_MIN_INSIDE = _env_float("CORRIDOR_MIN_INSIDE", 0.8)

# ---- post-paint audit ------------------------------------------------------
# A hop is judged at paint time with only the fixes that existed then. The fixes
# that arrive afterwards say where the bus actually went, and OSRM matches far
# better with a wider window. So suspicious segments are re-examined a minute
# later and either repaired with a better geometry or removed.
AUDIT_ENABLED = _env_int("AUDIT_ENABLED", 1)
AUDIT_INTERVAL = _env_int("AUDIT_INTERVAL", 60)   # seconds between audit passes
AUDIT_MIN_AGE = 45          # let later fixes accumulate first
AUDIT_MAX_AGE = 900         # older than this, the track is gone anyway
AUDIT_BATCH = _env_int("AUDIT_BATCH", 40)         # re-match jobs per pass (OSRM cost)
AUDIT_SCAN = _env_int("AUDIT_SCAN", 600)          # corridor re-checks per pass (free)
AUDIT_BOW_M = 40.0          # above this a segment is worth re-checking
AUDIT_RATIO = 1.2
AUDIT_BEFORE = 4            # fixes of context before the hop
AUDIT_AFTER = 5             # ...and after it
TRACK_FIXES = 15            # per-bus history kept for the audit
TRACK_SEC = 600

# --------------------------------------------------------------------------
# Route configuration
# --------------------------------------------------------------------------
ROUTE_CONFIG: List[dict] = []
UID_INFO: Dict[str, dict] = {}


def load_route_config() -> Tuple[List[dict], int]:
    """Read routes.json -> (routes, poll_interval)."""
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # pragma: no cover - config problems must be loud
        print(f"[config] FAILED to read {ROUTES_FILE}: {exc}")
        return [], POLL_INTERVAL

    routes = []
    seen = set()
    for row in cfg.get("routes", []):
        uid = str(row.get("uid", "")).strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        routes.append(
            {
                "uid": uid,
                "route": str(row.get("route", "?")).strip(),
                "direction": str(row.get("direction", "")).strip().lower() or "up",
                "from": row.get("from", ""),
                "to": row.get("to", ""),
            }
        )
    # an explicit POLL_INTERVAL env var wins over routes.json (Render, Docker)
    if os.environ.get("POLL_INTERVAL", "").strip():
        return routes, max(5, POLL_INTERVAL)
    interval = int(cfg.get("poll_interval_sec", POLL_INTERVAL) or POLL_INTERVAL)
    return routes, max(5, interval)


CORRIDOR_CELLS: Dict[str, set] = {}


def _cell(lat: float, lon: float) -> Tuple[int, int]:
    return (int(lat // CORRIDOR_CELL_DEG), int(lon // CORRIDOR_CELL_DEG))


def _cells_along(coords: List[List[float]]) -> set:
    """Occupied grid cells of a polyline, densified so there are no holes."""
    cells = set()
    if not coords:
        return cells
    if len(coords) == 1:
        cells.add(_cell(coords[0][1], coords[0][0]))
        return cells
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        dist_m = haversine_km(lon1, lat1, lon2, lat2) * 1000.0
        steps = max(1, int(dist_m / CORRIDOR_STEP_M))
        for k in range(steps + 1):
            f = k / steps
            cells.add(_cell(lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f))
    return cells


def load_corridors() -> None:
    """Read data/route_corridors.json (optional - absent means no constraint)."""
    global CORRIDOR_CELLS
    if not os.path.exists(CORRIDOR_FILE):
        CORRIDOR_CELLS = {}
        print(f"[corridor] {CORRIDOR_FILE} not found - matching runs unconstrained "
              f"(build it with tools/build_route_corridors.py)")
        return
    try:
        with open(CORRIDOR_FILE, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"[corridor] FAILED to read corridors: {exc}")
        CORRIDOR_CELLS = {}
        return

    built, points = {}, 0
    for key, coords in raw.items():
        if isinstance(coords, list) and len(coords) >= 2:
            built[key] = _cells_along(coords)
            points += len(coords)
    CORRIDOR_CELLS = built
    print(f"[corridor] {len(built)} corridors loaded "
          f"({points} points, {sum(len(c) for c in built.values())} cells)")


def _densify(path: List[List[float]], step_m: float = 25.0) -> List[List[float]]:
    """
    Sample a polyline every ~step_m.

    Essential before the corridor test: a straight chord has only TWO points,
    both of them on the route, so testing the raw path would pass any line that
    cuts across a block between two on-route fixes. Sampling the line itself is
    what catches that.
    """
    if len(path) < 2:
        return list(path)
    out: List[List[float]] = []
    for (lon1, lat1), (lon2, lat2) in zip(path, path[1:]):
        dist_m = haversine_km(lon1, lat1, lon2, lat2) * 1000.0
        steps = max(1, int(dist_m / step_m))
        for k in range(steps):
            f = k / steps
            out.append([lon1 + (lon2 - lon1) * f, lat1 + (lat2 - lat1) * f])
    out.append(list(path[-1]))
    return out


def corridor_ok(key: str, path: List[List[float]]) -> bool:
    """True if the path stays on its route's corridor (or none is known)."""
    cells = CORRIDOR_CELLS.get(key)
    if not cells or not path:
        return True
    dense = _densify(path)
    inside = 0
    for lon, lat in dense:
        ix, iy = _cell(lat, lon)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (ix + dx, iy + dy) in cells:
                    inside += 1
                    break
            else:
                continue
            break
    return inside >= len(dense) * CORRIDOR_MIN_INSIDE


def refresh_route_config() -> None:
    global ROUTE_CONFIG, UID_INFO, POLL_INTERVAL
    ROUTE_CONFIG, POLL_INTERVAL = load_route_config()
    UID_INFO = {r["uid"]: r for r in ROUTE_CONFIG}
    load_corridors()


# NB: the initial refresh_route_config() call lives below the geo helpers,
# because building corridor cells needs haversine_km.

# --------------------------------------------------------------------------
# Live state
# --------------------------------------------------------------------------
last_positions: Dict[str, dict] = {}    # bus_id -> tracking state
active_segments: List[dict] = []        # painted road, ordered by seq
_seq_counter = 0
_cycle_stats = {
    "last_cycle_started": 0.0,
    "last_cycle_seconds": 0.0,
    "feeds_ok": 0,
    "feeds_failed": 0,
    "buses_seen": 0,
    "segments_added": 0,
    "hops_dropped": 0,
    "dropped_off_corridor": 0,
    "dropped_long_chord": 0,
}

# segments removed by the audit; clients learn about them through `removed`
REMOVALS: List[dict] = []
AUDIT_STATS = {"last_run": 0.0, "checked": 0, "repaired": 0, "removed": 0,
               "total_repaired": 0, "total_removed": 0}

# why the last few hops were dropped, for /api/debug/drops
DROP_SAMPLES: List[dict] = []
_drop_counts = {"corridor": 0, "long_chord": 0}

_dtc_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()
_last_session_reset = 0.0


# --------------------------------------------------------------------------
# Geo helpers
# --------------------------------------------------------------------------
def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * asin(sqrt(a)) * 6371.0


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    d_lon = lon2 - lon1
    x = sin(d_lon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - (sin(lat1) * cos(lat2) * cos(d_lon))
    return int((degrees(atan2(x, y)) + 360) % 360)


JAM_BELOW_KMH = _env_float("JAM_BELOW_KMH", 5.0)
FAST_ABOVE_KMH = _env_float("FAST_ABOVE_KMH", 11.0)


def color_for_speed(speed_kmh: float) -> str:
    """Three modes only: heavy jam / moderate / fast."""
    if speed_kmh < JAM_BELOW_KMH:
        return "#ff4d4d"      # heavy jam
    if speed_kmh <= FAST_ABOVE_KMH:
        return "#ffa502"      # moderate
    return "#2ed573"          # fast / free flow


def is_wandering(recent: List[Tuple[float, float, float]]) -> bool:
    """
    True when a bus is standing still and only its GPS is moving.

    A parked bus drifts 20-40 m in random directions every poll, which clears
    any per-hop distance filter and used to paint a star of crossing lines.
    Travel is straight-ish: net displacement divided by the length of the walked
    path stays high. Wander doubles back on itself, so that ratio collapses.
    """
    if len(recent) < RECENT_FIXES:
        return False
    path_m = sum(
        haversine_km(a[1], a[0], b[1], b[0]) * 1000.0
        for a, b in zip(recent, recent[1:])
    )
    net_m = haversine_km(recent[0][1], recent[0][0], recent[-1][1], recent[-1][0]) * 1000.0
    if net_m >= CLEAR_TRAVEL_M:
        return False                       # covered real ground, whatever the shape
    if net_m < STATIONARY_NET_M:
        return True
    return path_m > 0 and (net_m / path_m) < MIN_STRAIGHTNESS


def decimate(coords: List[List[float]]) -> List[List[float]]:
    """Round + thin a polyline; keeps first and last point."""
    if not coords:
        return coords
    if len(coords) > MAX_PATH_POINTS:
        step = len(coords) / float(MAX_PATH_POINTS)
        picked = [coords[int(i * step)] for i in range(MAX_PATH_POINTS)]
        picked[-1] = coords[-1]
        coords = picked
    return [[round(c[0], COORD_DP), round(c[1], COORD_DP)] for c in coords]


# Routes + corridors are loaded here: everything they need is defined above.
refresh_route_config()


# --------------------------------------------------------------------------
# DTC session / fetching
# --------------------------------------------------------------------------
async def get_dtc_session() -> aiohttp.ClientSession:
    global _dtc_session
    async with _session_lock:
        if _dtc_session is None or _dtc_session.closed:
            _dtc_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                connector=aiohttp.TCPConnector(limit=DTC_CONCURRENCY * 2, ssl=False),
            )
            try:
                await _dtc_session.get(DTC_HOME)     # picks up the csrftoken cookie
            except Exception as exc:
                print(f"[session] warm-up failed: {exc}")
        return _dtc_session


async def reset_dtc_session() -> None:
    """Drop the session at most once every 10s, even if many feeds fail at once."""
    global _dtc_session, _last_session_reset
    now = time.time()
    if now - _last_session_reset < 10:
        return
    async with _session_lock:
        if now - _last_session_reset < 10:
            return
        _last_session_reset = now
        old, _dtc_session = _dtc_session, None

    # Closing the old session straight away kills the requests still in flight
    # on it ("Connector is closed"), which matters on a shared cloud IP where
    # rate-limit responses arrive in bursts. Let them finish, then close.
    if old is not None and not old.closed:
        async def _close_later(session: aiohttp.ClientSession) -> None:
            try:
                await asyncio.sleep(20)
                await session.close()
            except Exception:
                pass

        asyncio.create_task(_close_later(old))
    print("[session] rotated after auth/rate-limit response")


def _csrf_from(session: aiohttp.ClientSession) -> str:
    for cookie in session.cookie_jar:
        if cookie.key == "csrftoken":
            return cookie.value
    return ""


async def fetch_route_buses(uid: str, sem: asyncio.Semaphore) -> Tuple[str, List[dict], bool]:
    """Return (uid, buses, ok)."""
    async with sem:
        session = await get_dtc_session()
        spoofed_ip = f"203.0.113.{random.randint(1, 250)}"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "X-CSRFToken": _csrf_from(session),
            "Referer": DTC_HOME,
            "Content-Type": "application/json",
            "X-Forwarded-For": spoofed_ip,
            "Client-IP": spoofed_ip,
        }
        try:
            async with session.post(
                DTC_LIVE_URL,
                headers=headers,
                json={"route_id": str(uid)},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    buses = data.get("buses") or []
                    return uid, buses if isinstance(buses, list) else [], True
                if resp.status in (401, 403, 429):
                    await reset_dtc_session()
                else:
                    print(f"[api] status {resp.status} for uid {uid}")
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            print(f"[api] {type(exc).__name__} for uid {uid}: {exc}")
        return uid, [], False


# --------------------------------------------------------------------------
# OSRM map matching
# --------------------------------------------------------------------------
def _m_per_deg(lat: float) -> Tuple[float, float]:
    return 111320.0 * cos(radians(lat)), 110540.0


def point_to_segment_m(px, py, ax, ay, bx, by, lat_ref) -> float:
    """Distance (metres) from point P to segment AB, equirectangular approx."""
    mx, my = _m_per_deg(lat_ref)
    pxm, pym = px * mx, py * my
    axm, aym = ax * mx, ay * my
    bxm, bym = bx * mx, by * my
    dx, dy = bxm - axm, bym - aym
    den = dx * dx + dy * dy
    if den == 0:
        return sqrt((pxm - axm) ** 2 + (pym - aym) ** 2)
    t = max(0.0, min(1.0, ((pxm - axm) * dx + (pym - aym) * dy) / den))
    cx, cy = axm + t * dx, aym + t * dy
    return sqrt((pxm - cx) ** 2 + (pym - cy) ** 2)


def max_cross_track_m(path: List[List[float]], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Worst deviation of a [lon,lat] path from the straight chord a->b (lat,lon)."""
    worst = 0.0
    for lon, lat in path:
        d = point_to_segment_m(lon, lat, a[1], a[0], b[1], b[0], a[0])
        if d > worst:
            worst = d
    return worst


def _leg_geometry(matching: dict, leg_index: int) -> List[List[float]]:
    """Stitch the step geometries of one leg into a single polyline."""
    legs = matching.get("legs") or []
    if not (0 <= leg_index < len(legs)):
        return []
    out: List[List[float]] = []
    for step in legs[leg_index].get("steps", []):
        coords = (step.get("geometry") or {}).get("coordinates") or []
        if out and coords and out[-1] == coords[0]:
            out.extend(coords[1:])
        else:
            out.extend(coords)
    return out


def _path_ok(path, chord_m, leg_m, a, b, ckey: str = "") -> bool:
    """Shared guards for both /match and /route results."""
    if len(path) < 2:
        return False
    if ckey and not corridor_ok(ckey, path):
        return False
    ratio_cap = MAX_LEG_RATIO_SHORT if chord_m < SHORT_HOP_M else MAX_LEG_RATIO_LONG
    if chord_m > 25 and leg_m > chord_m * ratio_cap:
        return False
    cross_cap = min(
        CROSS_TRACK_CEILING,
        max(MAX_CROSS_TRACK_M, chord_m * CROSS_TRACK_FRACTION),
    )
    return max_cross_track_m(path, a, b) <= cross_cap


async def route_hop(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    lat1: float, lon1: float, lat2: float, lon2: float, chord_m: float,
    ckey: str = "",
) -> List[List[float]]:
    """Plain A->B routing. Only used for long hops, where it is reliable."""
    url = (
        f"{OSRM_ROUTE_URL}{lon1},{lat1};{lon2},{lat2}"
        f"?overview=full&geometries=geojson"
    )
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                if data.get("code") != "Ok" or not data.get("routes"):
                    return []
                r = data["routes"][0]
                path = r["geometry"]["coordinates"]
                if _path_ok(path, chord_m, r.get("distance", 0.0), (lat1, lon1), (lat2, lon2), ckey):
                    return path
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
    return []


async def match_to_road(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    history: List[Tuple[float, float, float]],
    ckey: str = "",
) -> Tuple[List[List[float]], Optional[int], bool]:
    """
    Snap the newest hop of a bus onto the road network.

    Fallback chain, best first:
      1. OSRM /match (hidden-Markov over the last few fixes). Routing two noisy
         fixes in isolation is what used to send trails into service lanes; the
         matcher sees the whole recent track, so it stays on the right road.
      2. For a long hop (> LONG_HOP_M) where matching failed, plain A->B /route.
         Over that distance routing is reliable and beats a straight line.
      3. The straight chord - but only up to MAX_CHORD_DRAW_M, so a failed hop
         never paints a long diagonal across blocks and buildings.
    """
    lat1, lon1, _ = history[-2]
    lat2, lon2, _ = history[-1]
    bearing = calculate_bearing(lat1, lon1, lat2, lon2)
    a, b = (lat1, lon1), (lat2, lon2)
    chord = [[lon1, lat1], [lon2, lat2]]
    chord_m = haversine_km(lon1, lat1, lon2, lat2) * 1000.0

    def _give_up() -> Tuple[List[List[float]], Optional[int], bool]:
        # Painting the chord is only honest if the chord itself lies on the
        # route. A bus whose fixes have drifted off its corridor gets nothing.
        on_corridor = (not ckey) or corridor_ok(ckey, chord)
        if chord_m <= MAX_CHORD_DRAW_M and on_corridor:
            return chord, bearing, False
        reason = "long_chord" if chord_m > MAX_CHORD_DRAW_M else "corridor"
        _drop_counts[reason] += 1
        DROP_SAMPLES.append({
            "reason": reason, "route": ckey, "chord_m": round(chord_m),
            "from": [round(lat1, 5), round(lon1, 5)],
            "to": [round(lat2, 5), round(lon2, 5)],
            "ts": round(time.time()),
        })
        del DROP_SAMPLES[:-40]
        return [], bearing, False

    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon, _ in history)
    radiuses = ";".join([str(MATCH_RADIUS_M)] * len(history))
    stamps = ";".join(str(int(ts)) for _, _, ts in history)
    url = (
        f"{OSRM_MATCH_URL}{coords}"
        f"?geometries=geojson&overview=full&steps=true&annotations=false"
        f"&gaps=ignore&radiuses={radiuses}&timestamps={stamps}"
    )

    matched: List[List[float]] = []
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    tps = data.get("tracepoints") or []
                    matchings = data.get("matchings") or []
                    tp = tps[-1] if tps else None
                    if data.get("code") == "Ok" and tp:
                        mi, wi = tp.get("matchings_index"), tp.get("waypoint_index")
                        if mi is not None and wi and mi < len(matchings):
                            matching = matchings[mi]
                            if (matching.get("confidence") or 0) >= MIN_CONFIDENCE:
                                path = _leg_geometry(matching, wi - 1)
                                leg_m = (matching.get("legs") or [{}])[wi - 1].get("distance", 0.0)
                                if _path_ok(path, chord_m, leg_m, a, b, ckey):
                                    matched = path
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

    if matched:
        return matched, bearing, True

    if chord_m > LONG_HOP_M:
        routed = await route_hop(session, sem, lat1, lon1, lat2, lon2, chord_m, ckey)
        if routed:
            return routed, bearing, True

    return _give_up()


async def match_window(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    fixes: List[Tuple[float, float, float]],
    i: int,
    j: int,
    ckey: str = "",
) -> List[List[float]]:
    """
    Map-match a whole window of fixes and return only the piece between
    fixes[i] and fixes[j].

    This is the audit's tool: the live matcher sees a bus's last 5 fixes, this
    one can see several fixes on BOTH sides of the hop, which is exactly the
    context an HMM matcher needs to pick the right carriageway.
    """
    if not (0 <= i < j < len(fixes)):
        return []
    lat1, lon1, _ = fixes[i]
    lat2, lon2, _ = fixes[j]
    a, b = (lat1, lon1), (lat2, lon2)
    chord_m = haversine_km(lon1, lat1, lon2, lat2) * 1000.0

    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon, _ in fixes)
    radiuses = ";".join([str(MATCH_RADIUS_M)] * len(fixes))
    stamps = ";".join(str(int(ts)) for _, _, ts in fixes)
    url = (
        f"{OSRM_MATCH_URL}{coords}"
        f"?geometries=geojson&overview=full&steps=true&annotations=false"
        f"&gaps=ignore&radiuses={radiuses}&timestamps={stamps}"
    )

    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                if data.get("code") != "Ok":
                    return []
                tps = data.get("tracepoints") or []
                matchings = data.get("matchings") or []
                if len(tps) <= j or tps[i] is None or tps[j] is None:
                    return []
                ti, tj = tps[i], tps[j]
                mi = ti.get("matchings_index")
                if mi is None or mi != tj.get("matchings_index") or mi >= len(matchings):
                    return []
                wi, wj = ti.get("waypoint_index"), tj.get("waypoint_index")
                if wi is None or wj is None or wj <= wi:
                    return []
                matching = matchings[mi]

                path: List[List[float]] = []
                leg_m = 0.0
                legs = matching.get("legs") or []
                for k in range(wi, wj):
                    piece = _leg_geometry(matching, k)
                    if k < len(legs):
                        leg_m += legs[k].get("distance", 0.0)
                    if path and piece and path[-1] == piece[0]:
                        path.extend(piece[1:])
                    else:
                        path.extend(piece)

                if _path_ok(path, chord_m, leg_m, a, b, ckey):
                    return path
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
    return []


# --------------------------------------------------------------------------
# Post-paint audit
# --------------------------------------------------------------------------
def _segment_shape(seg: dict) -> Tuple[float, float]:
    """(ratio, bow) of an already painted segment."""
    path = seg["path"]
    a = (path[0][1], path[0][0])
    b = (path[-1][1], path[-1][0])
    chord_m = haversine_km(a[1], a[0], b[1], b[0]) * 1000.0
    length_m = sum(
        haversine_km(p[0], p[1], q[0], q[1]) * 1000.0 for p, q in zip(path, path[1:])
    )
    ratio = (length_m / chord_m) if chord_m > 1 else 1.0
    return ratio, max_cross_track_m(path, a, b)


def _is_suspicious(seg: dict) -> bool:
    if not seg.get("snapped", True) or seg.get("flagged"):
        return True
    ratio, bow = _segment_shape(seg)
    return bow > AUDIT_BOW_M or ratio > AUDIT_RATIO


def _remove_segment(seg: dict) -> None:
    """Drop a segment and tell clients about it on their next sync."""
    try:
        active_segments.remove(seg)
    except ValueError:
        return
    REMOVALS.append({"rev": _next_seq(), "seq": seg["seq"], "ts": time.time()})
    AUDIT_STATS["removed"] += 1
    AUDIT_STATS["total_removed"] += 1


def _audit_window(track: List[Tuple[float, float, float]], seg: dict):
    """Locate the segment's hop inside the bus's track and build a wider window."""
    ts_from, ts_to = seg.get("ts_from"), seg.get("ts_to")
    if ts_from is None or ts_to is None:
        return None
    idx_from = idx_to = None
    for n, (_, _, ts) in enumerate(track):
        if abs(ts - ts_from) < 0.6:
            idx_from = n
        if abs(ts - ts_to) < 0.6:
            idx_to = n
    if idx_from is None or idx_to is None or idx_to <= idx_from:
        return None
    lo = max(0, idx_from - AUDIT_BEFORE)
    hi = min(len(track), idx_to + AUDIT_AFTER + 1)
    return track[lo:hi], idx_from - lo, idx_to - lo


async def audit_cycle(session: aiohttp.ClientSession, sem: asyncio.Semaphore) -> None:
    """
    Two passes over recently painted segments:

    * corridor re-check on EVERY segment - cheap, local, and the one that
      catches a trail painted against the wrong direction's corridor. Such a
      segment looks geometrically perfect, so a "suspicious" filter would never
      surface it.
    * re-match only the suspicious ones - that costs an OSRM call each.
    """
    now = time.time()
    AUDIT_STATS.update(last_run=now, checked=0, repaired=0, removed=0)

    due = [
        s for s in active_segments
        if not s.get("audited") and AUDIT_MIN_AGE <= (now - s["ts"]) <= AUDIT_MAX_AGE
    ][:AUDIT_SCAN]
    if not due:
        return

    jobs = []
    for seg in due:
        seg["audited"] = True
        AUDIT_STATS["checked"] += 1
        state = last_positions.get(seg["bus_id"])

        # Judge it by what the bus turned out to be, not by what the feed said
        # at the time - the direction label may have settled since.
        ckey = f"{seg['route']}|{seg['direction']}"
        if state:
            ckey = f"{state.get('route')}|{state.get('direction')}"

        if not corridor_ok(ckey, seg["path"]):
            _remove_segment(seg)
            continue

        if not _is_suspicious(seg) or len(jobs) >= AUDIT_BATCH:
            continue

        win = _audit_window((state or {}).get("track") or [], seg)
        if not win or len(win[0]) < 3:
            continue
        fixes, i, j = win
        jobs.append((seg, ckey, fixes, i, j))

    if not jobs:
        return

    results = await asyncio.gather(
        *(match_window(session, sem, f, i, j, ck) for _, ck, f, i, j in jobs)
    )

    for (seg, _ck, _f, _i, _j), path in zip(jobs, results):
        if not path or len(path) < 2:
            # The wider window could not confirm it. A clean match stands; a
            # straight-line fallback does not.
            if not seg.get("snapped", True):
                _remove_segment(seg)
            continue
        candidate = dict(seg, path=decimate(path))
        _, new_bow = _segment_shape(candidate)
        _, old_bow = _segment_shape(seg)
        if not seg.get("snapped", True) or new_bow < old_bow - 5:
            old_seq = seg["seq"]
            seg["path"] = candidate["path"]
            seg["snapped"] = True
            seg["revised"] = True
            seg["seq"] = _next_seq()      # clients pick the repaired one up as new
            REMOVALS.append({"rev": _next_seq(), "seq": old_seq, "ts": now})
            AUDIT_STATS["repaired"] += 1
            AUDIT_STATS["total_repaired"] += 1


async def audit_loop() -> None:
    if not AUDIT_ENABLED:
        print("[audit] disabled")
        return
    sem = asyncio.Semaphore(max(2, OSRM_CONCURRENCY // 2))
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(AUDIT_INTERVAL)
            try:
                await audit_cycle(session, sem)
                if AUDIT_STATS["checked"]:
                    print(
                        f"[audit] checked {AUDIT_STATS['checked']} "
                        f"| repaired {AUDIT_STATS['repaired']} "
                        f"| removed {AUDIT_STATS['removed']}",
                        flush=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[audit] error: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Tracker loop
# --------------------------------------------------------------------------
def _next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter


def _prune_segments(now: float) -> None:
    global active_segments, REMOVALS
    REMOVALS = [r for r in REMOVALS if now - r["ts"] < 3600]
    cutoff = now - SEGMENT_TTL
    kept = [s for s in active_segments if s["ts"] >= cutoff]
    if len(kept) > MAX_SEGMENTS:
        kept = kept[-MAX_SEGMENTS:]
    active_segments = kept


async def run_cycle(
    dtc_sem: asyncio.Semaphore,
    osrm_session: aiohttp.ClientSession,
    osrm_sem: asyncio.Semaphore,
) -> None:
    now = time.time()
    uids = [r["uid"] for r in ROUTE_CONFIG]
    if not uids:
        print("[tracker] routes.json has no routes - nothing to track")
        return

    results = await asyncio.gather(*(fetch_route_buses(u, dtc_sem) for u in uids))

    feeds_ok = sum(1 for _, _, ok in results if ok)
    feeds_failed = len(results) - feeds_ok

    pending = []          # snapping work for this cycle
    bus_count = 0
    seen_now: set = set()   # a bus is handled by ONE feed per cycle

    for uid, buses, _ok in results:
        meta = UID_INFO.get(uid, {})
        for bus in buses:
            bid = bus.get("id")
            if not bid:
                continue
            try:
                lat = float(bus.get("lat", 0) or 0)
                lng = float(bus.get("lng", 0) or 0)
            except (TypeError, ValueError):
                continue
            if lat == 0 or lng == 0:
                continue

            # The DTC feeds return the same physical bus on BOTH directions of a
            # route. Letting the label flip each cycle flips the corridor with
            # it, and where the carriageways split - a flyover over a service
            # loop - the hop gets matched onto the wrong one and draws a hook.
            # So: first feed to report a bus this cycle owns it.
            if bid in seen_now:
                continue
            seen_now.add(bid)

            bus_count += 1
            state = last_positions.get(bid)

            if state is not None and state.get("uid") != uid:
                # Ownership really did move (the old feed stopped reporting it,
                # usually a turnaround at a terminal). Start a fresh track so
                # nothing is painted across the change of direction.
                state.update(
                    uid=uid,
                    route=meta.get("route", state.get("route")),
                    direction=meta.get("direction", state.get("direction")),
                    raw_route=bus.get("route", state.get("raw_route")),
                    lat=lat, lng=lng, seen=now,
                    anchor_lat=lat, anchor_lng=lng, anchor_ts=now,
                    bearing=None,
                    history=[(lat, lng, now)],
                    recent=[(lat, lng, now)],
                    track=[(lat, lng, now)],
                )
                continue
            if state is None:
                last_positions[bid] = {
                    "lat": lat, "lng": lng,
                    "anchor_lat": lat, "anchor_lng": lng, "anchor_ts": now,
                    "seen": now, "bearing": None, "speed": None,
                    "history": [(lat, lng, now)],
                    "recent": [(lat, lng, now)],
                    "track": [(lat, lng, now)],
                    "route": meta.get("route", bus.get("route", "?")),
                    "direction": meta.get("direction", ""),
                    "raw_route": bus.get("route", ""),
                    "uid": uid,
                }
                continue

            # keep metadata fresh (a bus can be re-assigned between feeds)
            state["route"] = meta.get("route", state.get("route"))
            state["direction"] = meta.get("direction", state.get("direction"))
            state["raw_route"] = bus.get("route", state.get("raw_route"))
            state["uid"] = uid
            state["lat"], state["lng"], state["seen"] = lat, lng, now

            # raw fix ring buffer - every fix lands here, filtered or not
            recent = [f for f in (state.get("recent") or []) if now - f[2] <= RECENT_SEC]
            recent.append((lat, lng, now))
            state["recent"] = recent[-RECENT_FIXES:]

            # longer history, used by the audit to re-match with wider context
            track = [f for f in (state.get("track") or []) if now - f[2] <= TRACK_SEC]
            track.append((lat, lng, now))
            state["track"] = track[-TRACK_FIXES:]

            dist_km = haversine_km(state["anchor_lng"], state["anchor_lat"], lng, lat)
            dist_m = dist_km * 1000.0
            dt = now - state["anchor_ts"]

            if dist_m < MIN_MOVE_M:
                # Parked / GPS shimmer: hold the anchor so noise never accumulates,
                # but bleed the smoothed speed towards zero so the next segment
                # correctly paints as a jam.
                if state["speed"] is not None:
                    state["speed"] = state["speed"] * (1 - SPEED_ALPHA)
                continue

            # Warm-up: paint nothing until the parked test has enough raw fixes,
            # otherwise a bus that is already standing still draws a star before
            # we can tell it apart from one that is moving.
            if len(state["recent"]) < RECENT_FIXES or is_wandering(state["recent"]):
                if state["speed"] is not None:
                    state["speed"] = state["speed"] * (1 - SPEED_ALPHA)
                continue

            # Direction reversal on a short hop is GPS noise, not a U-turn.
            hop_bearing = calculate_bearing(state["anchor_lat"], state["anchor_lng"], lat, lng)
            prev_bearing = state.get("bearing")
            if prev_bearing is not None and dist_m < SHORT_NOISE_HOP_M:
                diff = abs(hop_bearing - prev_bearing) % 360
                if min(diff, 360 - diff) > BEARING_FLIP_DEG:
                    if state["speed"] is not None:
                        state["speed"] = state["speed"] * (1 - SPEED_ALPHA)
                    continue

            if dist_km > MAX_JUMP_KM or dt <= 0:
                # real teleport: start a fresh track, do not paint across Delhi
                state.update(anchor_lat=lat, anchor_lng=lng, anchor_ts=now,
                             history=[(lat, lng, now)], recent=[(lat, lng, now)],
                             track=[(lat, lng, now)])
                continue

            # A fix that implies a silly speed is usually a delayed update, not a
            # teleport - clamp the speed but still paint the hop, otherwise the
            # trail gets a hole.
            raw_speed = min(dist_km / (dt / 3600.0), MAX_PLAUSIBLE_KMH)

            prev = state["speed"]
            speed = raw_speed if prev is None else (SPEED_ALPHA * raw_speed + (1 - SPEED_ALPHA) * prev)
            state["speed"] = speed

            # rolling track window that feeds the map matcher
            hist = list(state.get("history") or [])
            if not hist or (hist[-1][0], hist[-1][1]) != (state["anchor_lat"], state["anchor_lng"]):
                hist.append((state["anchor_lat"], state["anchor_lng"], state["anchor_ts"]))
            hist.append((lat, lng, now))
            hist = [h for h in hist if now - h[2] <= MATCH_WINDOW_SEC][-MATCH_WINDOW:]
            if len(hist) < 2:
                hist = [(state["anchor_lat"], state["anchor_lng"], state["anchor_ts"]),
                        (lat, lng, now)]
            state["history"] = hist

            pending.append({"bid": bid, "state": state, "speed": speed, "history": hist,
                            "ckey": f"{state.get('route')}|{state.get('direction')}",
                            "hop": (hist[-2], hist[-1])})
            # anchor moves forward whether or not OSRM answers
            state.update(anchor_lat=lat, anchor_lng=lng, anchor_ts=now)

    snapped = []
    if pending:
        snapped = await asyncio.gather(
            *(match_to_road(osrm_session, osrm_sem, p["history"], p["ckey"]) for p in pending)
        )

    _drop_counts["corridor"] = _drop_counts["long_chord"] = 0
    added = dropped = 0
    for p, (path, bearing, was_snapped) in zip(pending, snapped):
        if bearing is not None:
            p["state"]["bearing"] = bearing
        if not path or len(path) < 2:
            dropped += 1
            continue
        st = p["state"]
        active_segments.append(
            {
                "seq": _next_seq(),
                "bus_id": p["bid"],
                "route": st.get("route", "?"),
                "direction": st.get("direction", ""),
                "path": decimate(path),
                "color": color_for_speed(p["speed"]),
                "speed": round(p["speed"], 1),
                "snapped": was_snapped,
                "ts": now,
                "ts_from": p["hop"][0][2],
                "ts_to": p["hop"][1][2],
                "audited": False,
            }
        )
        added += 1

    # drop buses nobody has seen for a while
    for bid in [b for b, s in last_positions.items() if now - s["seen"] > BUS_STALE_SEC * 4]:
        last_positions.pop(bid, None)

    _prune_segments(now)

    _cycle_stats.update(
        last_cycle_started=now,
        last_cycle_seconds=round(time.time() - now, 2),
        feeds_ok=feeds_ok,
        feeds_failed=feeds_failed,
        buses_seen=bus_count,
        segments_added=added,
        hops_dropped=dropped,
        dropped_off_corridor=_drop_counts["corridor"],
        dropped_long_chord=_drop_counts["long_chord"],
    )
    print(
        f"[tracker] {bus_count} buses | feeds {feeds_ok}/{len(results)} ok "
        f"| +{added} segments (total {len(active_segments)}) "
        f"| {dropped} dropped "
        f"| {_cycle_stats['last_cycle_seconds']}s",
        flush=True,
    )


async def tracker_loop() -> None:
    dtc_sem = asyncio.Semaphore(DTC_CONCURRENCY)
    osrm_sem = asyncio.Semaphore(OSRM_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=OSRM_CONCURRENCY * 2)
    async with aiohttp.ClientSession(connector=connector) as osrm_session:
        while True:
            started = time.time()
            try:
                await run_cycle(dtc_sem, osrm_session, osrm_sem)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[tracker] cycle error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(2.0, POLL_INTERVAL - (time.time() - started)))


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
async def keep_alive_loop() -> None:
    """
    Render's free plan puts the service to sleep after ~15 min without traffic,
    which stops the tracker and wipes every painted trail. Pinging our own
    health endpoint keeps the instance awake. RENDER_EXTERNAL_URL is injected by
    Render; KEEP_ALIVE_URL overrides it. Both empty => this loop never starts.
    """
    url = os.environ.get("KEEP_ALIVE_URL", "").strip()
    if not url:
        base = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        if base:
            url = base.rstrip("/") + "/api/health"
    if not url:
        return

    every = max(60, _env_int("KEEP_ALIVE_SEC", 600))
    print(f"[keep-alive] pinging {url} every {every}s")
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(every)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    await r.read()
            except Exception as exc:
                print(f"[keep-alive] {type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("=" * 62)
    print(f" Delhi live traffic | {len(ROUTE_CONFIG)} feeds "
          f"({len({r['route'] for r in ROUTE_CONFIG})} routes x up/down)")
    print(f" Poll interval {POLL_INTERVAL}s | DTC x{DTC_CONCURRENCY} | OSRM x{OSRM_CONCURRENCY}")
    print("=" * 62)
    tasks = [
        asyncio.create_task(tracker_loop()),
        asyncio.create_task(keep_alive_loop()),
        asyncio.create_task(audit_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if _dtc_session is not None and not _dtc_session.closed:
            await _dtc_session.close()


app = FastAPI(title="Delhi Live Bus Traffic", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_frontend():
    return FileResponse(FRONTEND_INDEX)


@app.get("/api/traffic_segments")
async def get_traffic_segments(after: int = Query(0, ge=0)):
    """
    Incremental feed.

    `after` is the cursor the client got last time; only segments newer than
    that are returned. Pass 0 on first load to get everything still in memory.
    Bus positions are always returned in full (they are small and they move).
    """
    now = time.time()
    segs = [s for s in active_segments if s["seq"] > after]
    removed = [r["seq"] for r in REMOVALS if r["rev"] > after]

    features = [
        {
            "type": "Feature",
            "properties": {
                "seq": s["seq"],
                "color": s["color"],
                "speed": s["speed"],
                "bus_id": s["bus_id"],
                "route": s["route"],
                "direction": s["direction"],
                "snapped": s.get("snapped", True),
                "revised": bool(s.get("revised")),
                "ts": round(s["ts"], 1),
            },
            "geometry": {"type": "LineString", "coordinates": s["path"]},
        }
        for s in segs
    ]

    buses = [
        {
            "type": "Feature",
            "properties": {
                "bus_id": bid,
                "route": st.get("route", "?"),
                "direction": st.get("direction", ""),
                "raw_route": st.get("raw_route", ""),
                "speed": round(st["speed"], 1) if st.get("speed") is not None else None,
                "color": color_for_speed(st["speed"]) if st.get("speed") is not None else "#8fa6c4",
                "bearing": st.get("bearing"),
                "age": round(now - st["seen"], 1),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [round(st["lng"], COORD_DP), round(st["lat"], COORD_DP)],
            },
        }
        for bid, st in last_positions.items()
        if now - st["seen"] < BUS_STALE_SEC
    ]

    return {
        "cursor": _seq_counter,
        "server_time": round(now, 1),
        "segments": {"type": "FeatureCollection", "features": features},
        "removed": removed,
        "buses": {"type": "FeatureCollection", "features": buses},
        "stats": {
            "active_buses": len(buses),
            "new_segments": len(features),
            "removed_segments": len(removed),
            "audit": AUDIT_STATS,
            "total_segments": len(active_segments),
            "poll_interval": POLL_INTERVAL,
            **_cycle_stats,
        },
    }


@app.get("/api/routes")
async def get_routes():
    now = time.time()
    counts: Dict[str, int] = {}
    for st in last_positions.values():
        if now - st["seen"] < BUS_STALE_SEC:
            key = f"{st.get('route')}|{st.get('direction')}"
            counts[key] = counts.get(key, 0) + 1

    rows = []
    for r in ROUTE_CONFIG:
        rows.append(
            {
                **r,
                "live_buses": counts.get(f"{r['route']}|{r['direction']}", 0),
            }
        )
    return {"count": len(rows), "routes": rows}


@app.post("/api/reload_routes")
async def reload_routes():
    """Re-read routes.json without restarting the server."""
    refresh_route_config()
    return {"ok": True, "feeds": len(ROUTE_CONFIG)}


@app.get("/api/osm_roads")
async def osm_roads(layer: str = Query("major", pattern="^(major|all)$")):
    """
    Serve the downloaded Delhi OSM road network as a map overlay.

    Files are produced by tools/download_delhi_osm.py:
        data/delhi_roads_major.geojson  (motorway/trunk/primary/secondary)
        data/delhi_roads.geojson        (full drive network - large)
    """
    name = "delhi_roads_major.geojson" if layer == "major" else "delhi_roads.geojson"
    path = os.path.join(ROOT_DIR, "data", name)
    if not os.path.exists(path):
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"{name} not found - run tools/download_delhi_osm.py"},
        )
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/debug/segments")
async def debug_segments(
    s: float = Query(...), w: float = Query(...),
    n: float = Query(...), e: float = Query(...),
    limit: int = Query(60, ge=1, le=300),
    min_bow: float = Query(0.0, ge=0),
    min_ratio: float = Query(0.0, ge=0),
):
    """
    Diagnostics: which segments were painted inside a bounding box, and how.

    Open in a browser, e.g.
      /api/debug/segments?s=28.552&w=77.274&n=28.565&e=77.287

    `snapped` false means the straight chord was painted because matching
    failed or tripped a guard. `bow` is how far the painted path strays from
    its own straight chord, `ratio` its length against that chord - together
    they say whether a trail took a detour or followed the road.
    """
    rows = []
    scanned = 0
    bows: List[float] = []
    for seg in reversed(active_segments):
        path = seg["path"]
        if not any(s < p[1] < n and w < p[0] < e for p in path):
            continue
        a = (path[0][1], path[0][0])
        b = (path[-1][1], path[-1][0])
        chord_m = haversine_km(a[1], a[0], b[1], b[0]) * 1000.0
        length_m = sum(
            haversine_km(p[0], p[1], q[0], q[1]) * 1000.0
            for p, q in zip(path, path[1:])
        )
        bow = max_cross_track_m(path, a, b)
        ratio = (length_m / chord_m) if chord_m > 1 else 0.0
        scanned += 1
        bows.append(bow)
        if bow < min_bow or ratio < min_ratio:
            continue
        rows.append(
            {
                "seq": seg["seq"],
                "route": seg["route"],
                "dir": seg["direction"],
                "bus": seg["bus_id"],
                "speed": seg["speed"],
                "snapped": seg.get("snapped", True),
                "pts": len(path),
                "chord_m": round(chord_m),
                "len_m": round(length_m),
                "ratio": round(ratio, 2) if chord_m > 1 else None,
                "bow_m": round(bow),
                "age_s": round(time.time() - seg["ts"]),
                "start": [round(path[0][1], 5), round(path[0][0], 5)],
                "end": [round(path[-1][1], 5), round(path[-1][0], 5)],
            }
        )
        if len(rows) >= limit:
            break

    buses = {}
    for r in rows:
        buses.setdefault(r["bus"], 0)
        buses[r["bus"]] += 1

    live = []
    for bid, st in last_positions.items():
        if bid in buses and st.get("recent"):
            live.append(
                {
                    "bus": bid,
                    "route": st.get("route"),
                    "dir": st.get("direction"),
                    "speed": round(st["speed"], 1) if st.get("speed") is not None else None,
                    "bearing": st.get("bearing"),
                    "recent_fixes": [[round(f[0], 5), round(f[1], 5)] for f in st["recent"]],
                }
            )

    def pct(p: float) -> Optional[float]:
        if not bows:
            return None
        q = sorted(bows)
        return round(q[min(len(q) - 1, int(len(q) * p))], 1)

    return {
        "bbox": {"s": s, "w": w, "n": n, "e": e},
        "in_bbox": scanned,
        "returned": len(rows),
        "filters": {"min_bow": min_bow, "min_ratio": min_ratio},
        "bow_percentiles": {"p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99),
                            "max": round(max(bows), 1) if bows else None},
        "corridors_loaded": len(CORRIDOR_CELLS),
        "cross_track_cap_now": {
            "base_m": MAX_CROSS_TRACK_M,
            "fraction_of_chord": CROSS_TRACK_FRACTION,
            "ceiling_m": CROSS_TRACK_CEILING,
        },
        "by_bus": buses,
        "segments": rows,
        "raw_fixes_of_those_buses": live,
    }


@app.get("/api/debug/drops")
async def debug_drops():
    """The last few hops that were not painted, and why."""
    return {
        "corridor_tolerance": {
            "cell_m": CORRIDOR_CELL_M,
            "effective_m": round(CORRIDOR_CELL_M * 2),
            "min_inside": CORRIDOR_MIN_INSIDE,
            "corridors_loaded": len(CORRIDOR_CELLS),
        },
        "last_cycle": {
            "off_corridor": _cycle_stats["dropped_off_corridor"],
            "long_chord": _cycle_stats["dropped_long_chord"],
            "painted": _cycle_stats["segments_added"],
        },
        "samples": list(reversed(DROP_SAMPLES)),
    }


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "feeds": len(ROUTE_CONFIG),
        "corridors": len(CORRIDOR_CELLS),
        "audit": AUDIT_STATS,
        "buses": len(last_positions),
        "segments": len(active_segments),
        **_cycle_stats,
    }


# --------------------------------------------------------------------------
# Local / container entrypoint:  python backend/main.py
# (Render uses the startCommand in render.yaml instead.)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=_env_int("PORT", 8000),
        reload=False,
    )
