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
import os
import re
import sys
import time

import requests

BASE = "https://www.dtcbusroutes.in"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES_FILE = os.path.join(ROOT, "backend", "routes.json")
OUT_FILE = os.path.join(ROOT, "data", "route_corridors.json")

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

    session = requests.Session()
    session.get(BASE + "/", headers=HEADERS, timeout=25)

    ok = miss = 0
    for row in routes:
        name, direction, uid = row["route"], row["direction"], str(row["uid"])
        if args.only and name not in args.only:
            continue
        key = f"{name}|{direction}"

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

        row["sid"] = sid
        corridors[key] = coords
        stops = len((md or {}).get("stops") or [])
        print(f"  ok {key:<18} sid={sid:<6} {len(coords):>4} points, {stops} stops")
        ok += 1
        time.sleep(0.4)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(corridors, fh, separators=(",", ":"))
    with open(ROUTES_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)

    mb = os.path.getsize(OUT_FILE) / 1e6
    print(f"\n{ok} corridors written, {miss} missing -> data/route_corridors.json ({mb:.2f} MB)")
    print("Commit that file so Render gets it, then restart / redeploy the backend.")
    if miss:
        print("Routes without a corridor are simply left unconstrained - nothing breaks.")


if __name__ == "__main__":
    sys.exit(main())
