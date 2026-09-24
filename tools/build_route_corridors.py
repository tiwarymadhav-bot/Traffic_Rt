"""
Build the route corridors that constrain map matching.
======================================================
Every tracked route has one fixed path. The DTC site publishes it: each route
page carries a `_mapData` object whose `route_coords` is the full LineString of
that route/direction. This script collects those polylines into

    data/route_corridors.json      { "<route>|<direction>": [[lon,lat], ...] }

The backend then refuses any matched path that strays off its own route's
corridor - so bus 463 can never be painted onto the DND-KMP Expressway, because
that expressway is not on route 463 at all. No threshold tuning can achieve
this; the corridor is ground truth.

Usage (needs internet, run from the project root):

    python tools/build_route_corridors.py
    python tools/build_route_corridors.py --only 463 473
    python tools/build_route_corridors.py --refresh        # ignore cached sids

The search id of each route is cached back into backend/routes.json as "sid",
so later runs need one request per route instead of several.
"""

import argparse
import json
import math
import os
import re
import sys
import time

import requests

BASE = "https://www.dtcbusroutes.in"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"}

OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/"
SMOOTH_GAP_M = 200.0      # a published line sometimes jumps straight over a curve
SMOOTH_MAX_RATIO = 2.5    # reject a routed replacement that detours

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES_FILE = os.path.join(ROOT, "backend", "routes.json")
OUT_FILE = os.path.join(ROOT, "data", "route_corridors.json")
STOPS_FILE = os.path.join(ROOT, "data", "route_stops.json")

UID_RE = re.compile(r"_liveBusRouteUID\s*=\s*'([^']*)'")


def extract_map_data(html):
    """Pull the `_mapData = {...}` object out of a route page."""
    i = html.find("_mapData")
    if i < 0:
        return None
    start = html.find("{", i)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for p in range(start, len(html)):
        ch = html[p]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:p + 1])
                except Exception:
                    return None
    return None


def fetch_route_page(session, sid):
    r = session.get(f"{BASE}/bus/search/", params={"id": sid}, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None, None
    m = UID_RE.search(r.text)
    return (m.group(1) if m else None), r.text


def search_candidates(session, term):
    r = session.get(
        f"{BASE}/ajax/bus/search/",
        params={"q": term, "limit": 60},
        headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"},
        timeout=25,
    )
    if r.status_code != 200 or "DOCTYPE" in r.text[:200]:
        return []
    out = []
    norm = lambda s: re.sub(r"\s*-\s*Cluster$", "", s, flags=re.I).strip().upper()
    want = norm(term)
    for line in r.text.splitlines():
        if "|" not in line:
            continue
        sid, rest = line.split("|", 1)
        name = rest.split("(")[0].strip()
        if norm(name) == want or want in norm(name):
            out.append(sid.strip())
    return out


def _haversine_m(a, b):
    r = math.radians
    dlon, dlat = r(b[0] - a[0]), r(b[1] - a[1])
    h = (math.sin(dlat / 2) ** 2
         + math.cos(r(a[1])) * math.cos(r(b[1])) * math.sin(dlon / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def smooth_gaps(session, coords, label=""):
    """
    Replace long straight jumps in the published line with the real road path.

    The DTC line follows road geometry closely (median point spacing ~30 m) but
    occasionally leaps several hundred metres in one straight segment. A trail
    painted along such a leap cuts the corner, so each gap is routed once, here,
    and the result is baked into the corridor file. Done offline, never at
    request time.
    """
    out = [coords[0]]
    gaps = fixed = 0
    for a, b in zip(coords, coords[1:]):
        gap = _haversine_m(a, b)
        if gap <= SMOOTH_GAP_M:
            out.append(b)
            continue
        gaps += 1
        try:
            url = f"{OSRM_ROUTE}{a[0]},{a[1]};{b[0]},{b[1]}"
            r = session.get(url, params={"overview": "full", "geometries": "geojson"},
                            timeout=25)
            data = r.json() if r.status_code == 200 else {}
            route = (data.get("routes") or [{}])[0]
            geom = (route.get("geometry") or {}).get("coordinates") or []
            if len(geom) >= 2 and route.get("distance", 0) <= gap * SMOOTH_MAX_RATIO:
                for pt in geom[1:-1]:
                    out.append([round(pt[0], 5), round(pt[1], 5)])
                fixed += 1
        except Exception:
            pass
        out.append(b)
        time.sleep(0.25)
    if gaps:
        print(f"     gaps > {SMOOTH_GAP_M:.0f} m: {gaps}, routed: {fixed}"
              f"  ({len(coords)} -> {len(out)} points)  {label}")
    return out


def stop_points(md, label=""):
    """
    Pull the stop coordinates out of `_mapData.stops` as [[lon,lat], ...].

    The site is not consistent about the key names or the coordinate order, so
    every plausible shape is accepted. Order is decided by value, not by name:
    Delhi sits at lat ~28, lon ~77, so the larger of the two is always the
    longitude.
    """
    out = []
    for st in (md or {}).get("stops") or []:
        a = b = None
        name = ""
        if isinstance(st, dict):
            props = st.get("properties")
            if isinstance(props, dict):
                name = props.get("title") or props.get("name") or ""
            name = name or st.get("name") or st.get("title") or ""
        if isinstance(st, dict):
            # GeoJSON Feature - what the site actually publishes:
            # {"type":"Feature","geometry":{"type":"Point","coordinates":[lon,lat]},
            #  "properties":{"title":"Patparganj Industrial Area","marker-symbol":"1"}}
            geom = st.get("geometry")
            if isinstance(geom, dict):
                c = geom.get("coordinates")
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    a, b = c[0], c[1]
        if a is None and isinstance(st, dict):
            for kx, ky in (("lng", "lat"), ("lon", "lat"),
                           ("longitude", "latitude"), ("x", "y")):
                if st.get(kx) is not None and st.get(ky) is not None:
                    a, b = st[kx], st[ky]
                    break
            if a is None:
                for k in ("coords", "coordinates", "location", "latlng", "position"):
                    c = st.get(k)
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        a, b = c[0], c[1]
                        break
                    if isinstance(c, dict):
                        a = c.get("lng", c.get("lon", c.get("longitude")))
                        b = c.get("lat", c.get("latitude"))
                        if a is not None and b is not None:
                            break
        elif isinstance(st, (list, tuple)) and len(st) >= 2:
            a, b = st[0], st[1]
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        lon, lat = (a, b) if a >= b else (b, a)
        if not (27.0 < lat < 30.0 and 75.5 < lon < 78.5):
            continue
        # [lon, lat, name] - the name is what a rider recognises, so the arrival
        # panel can say "Ashram Chowk" instead of "stop #17". Readers accept a
        # bare [lon, lat] too, so an older file keeps working.
        out.append([round(lon, 5), round(lat, 5), str(name).strip()])
    if not out and (md or {}).get("stops"):
        first = ((md or {}).get("stops") or [None])[0]
        print(f"     !! could not read stop coordinates {label}; first entry looks like:"
              f" {str(first)[:160]}")
    return out


def tidy(coords):
    """Round and drop consecutive duplicates - the site repeats points a lot."""
    out = []
    for c in coords:
        try:
            p = [round(float(c[0]), 5), round(float(c[1]), 5)]
        except (TypeError, ValueError, IndexError):
            continue
        if not out or out[-1] != p:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="limit to these route numbers")
    ap.add_argument("--refresh", action="store_true", help="ignore cached sids")
    ap.add_argument("--no-smooth", action="store_true",
                    help="keep long straight jumps in the published line as they are")
    ap.add_argument("--stops-only", action="store_true",
                    help="collect stops only and leave data/route_corridors.json "
                         "exactly as it is (no re-smoothing, much faster)")
    ap.add_argument("--new-only", action="store_true",
                    help="build only the routes that have no corridor yet, so "
                         "repairs already applied to the existing ones are kept")
    args = ap.parse_args()

    with open(ROUTES_FILE, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    routes = cfg.get("routes", [])

    corridors = {}
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as fh:
                corridors = json.load(fh)
        except Exception:
            corridors = {}

    stops_out = {}
    if os.path.exists(STOPS_FILE):
        try:
            with open(STOPS_FILE, encoding="utf-8") as fh:
                stops_out = json.load(fh)
        except Exception:
            stops_out = {}

    session = requests.Session()
    session.get(BASE + "/", headers=HEADERS, timeout=25)

    ok = miss = skipped = 0
    for row in routes:
        name, direction, uid = row["route"], row["direction"], str(row["uid"])
        if args.only and name not in args.only:
            continue
        key = f"{name}|{direction}"
        if args.new_only and key in corridors:
            skipped += 1
            continue

        sid = None if args.refresh else row.get("sid")
        html = None
        if sid:
            got_uid, html = fetch_route_page(session, sid)
            if got_uid != uid:
                sid, html = None, None          # cached sid is stale

        if not sid:
            for cand in search_candidates(session, name):
                got_uid, page = fetch_route_page(session, cand)
                time.sleep(0.4)
                if got_uid == uid:
                    sid, html = cand, page
                    break

        if not html:
            print(f"  !! {key:<18} no page found for uid {uid}")
            miss += 1
            continue

        md = extract_map_data(html)
        coords = tidy((md or {}).get("route_coords") or [])
        if len(coords) < 2:
            print(f"  !! {key:<18} page had no route_coords")
            miss += 1
            continue

        if not args.no_smooth and not args.stops_only:
            coords = tidy(smooth_gaps(session, coords, key))

        row["sid"] = sid
        if not args.stops_only:
            corridors[key] = coords
        pts = stop_points(md, key)
        if pts:
            stops_out[key] = pts
        stops = len((md or {}).get("stops") or [])
        if args.stops_only:
            print(f"  ok {key:<18} sid={sid:<6} {len(pts)}/{stops} stops")
        else:
            print(f"  ok {key:<18} sid={sid:<6} {len(coords):>4} points, "
                  f"{stops} stops ({len(pts)} with coordinates)")
        ok += 1
        time.sleep(0.4)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(corridors, fh, separators=(",", ":"))
    with open(STOPS_FILE, "w", encoding="utf-8") as fh:
        json.dump(stops_out, fh, separators=(",", ":"))
    with open(ROUTES_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)

    mb = os.path.getsize(OUT_FILE) / 1e6
    if args.stops_only:
        print(f"\ncorridors left untouched -> data/route_corridors.json ({mb:.2f} MB)")
    else:
        print(f"\n{ok} corridors written, {miss} missing -> "
              f"data/route_corridors.json ({mb:.2f} MB)")
    print(f"{sum(len(v) for v in stops_out.values())} stops on {len(stops_out)} routes "
          f"-> data/route_stops.json  (used to tell a bus stop apart from a jam)")
    print("Commit that file so Render gets it, then restart / redeploy the backend.")
    if skipped:
        print(f"{skipped} route(s) already had a corridor and were left untouched "
              f"(--new-only), so any repairs on them are kept.")
    if miss:
        print("Routes without a corridor are simply left unconstrained - nothing breaks.")


if __name__ == "__main__":
    sys.exit(main())
