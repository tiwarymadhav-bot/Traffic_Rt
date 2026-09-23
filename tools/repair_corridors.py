"""
Repair the corridor stretches that tools/audit_corridors.py flagged.

The audit finds where a route line ran away from its own stops - the signature
of a line that climbed onto a flyover while the bus stayed on the service road
below. This tool rebuilds only those stretches, and rebuilds them *through the
stops*: the stops are handed to OSRM as via-points, so the returned path has no
choice but to come down to the road that actually serves them. A flyover has no
stops, so it can no longer be chosen.

Nothing else in the corridor file is touched. Every rebuilt stretch must pass a
check before it is kept - it has to bring its stops closer AND not wander - so a
repair can only improve a line, never make it worse.

    python tools/repair_corridors.py --dry-run     # look first, change nothing
    python tools/repair_corridors.py               # repair and write
    python tools/repair_corridors.py --only 534 543

Re-run tools/audit_corridors.py afterwards to see the before/after.
"""

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_corridors import (CORRIDOR_FILE, STOPS_FILE, build_line,
                             haversine_m, point_at, project)
from road_index import RoadIndex
from offline_router import OfflineRouter

OSRM_ROUTE = "https://router.project-osrm.org/route/v1/driving/"

PAD_STOPS = 1        # anchor the rebuild one good stop either side
SNAP_RADIUS_M = 30   # how close OSRM must snap a via-point to the stop itself
BEARING_RANGE = 60   # +/- degrees allowed around the direction of travel
RESIDUAL_MOTORWAY_M = 80    # a ramp junction brushes past; this much is noise
MAX_LEN_RATIO = 1.6  # a replacement longer than this is a detour, not a fix
END_TOL_M = 25       # the new piece must start and end where the old one did
GOOD_OFF_M = 20      # a stop this close to the line counts as served


def bearing_deg(a, b):
    """Bearing in degrees from (lat, lon) a to b."""
    from math import atan2, cos, degrees, radians, sin
    la1, lo1, la2, lo2 = map(radians, (a[0], a[1], b[0], b[1]))
    x = sin(lo2 - lo1) * cos(la2)
    y = cos(la1) * sin(la2) - sin(la1) * cos(la2) * cos(lo2 - lo1)
    return int((degrees(atan2(x, y)) + 360) % 360)


def osrm_route(session, pts, exclude=None, bearings=None):
    """
    pts = [(lat, lon), ...] -> ([[lon, lat], ...], error, exclude_applied).

    `bearings` matters more than it looks. Delhi's main roads are divided, and a
    stop sits on one carriageway. Handed a bare coordinate, OSRM snaps it to
    whichever carriageway is nearest - often the opposite one - and then has to
    drive to the next U-turn and back, which is why a 455 m stretch came back as
    a 10 km detour. Telling OSRM which way the bus is travelling at each
    waypoint pins it to the right side of the road.
    """
    coords = ";".join(f"{lon},{lat}" for lat, lon in pts)
    radiuses = ";".join(["50"] + [str(SNAP_RADIUS_M)] * (len(pts) - 2) + ["50"])
    params = {"overview": "full", "geometries": "geojson",
              "radiuses": radiuses, "continue_straight": "false"}
    if bearings:
        params["bearings"] = ";".join(f"{b},{BEARING_RANGE}" for b in bearings)
    if exclude:
        params["exclude"] = exclude
    try:
        r = session.get(OSRM_ROUTE + coords, params=params, timeout=40)
    except Exception as exc:
        return None, f"router unreachable ({type(exc).__name__})", bool(exclude)
    try:
        data = r.json()
    except ValueError:
        return None, f"HTTP {r.status_code} (not JSON)", bool(exclude)
    if r.status_code != 200 or data.get("code") != "Ok" or not data.get("routes"):
        msg = data.get("code") or f"HTTP {r.status_code}"
        if data.get("message"):
            msg += f" - {data['message']}"
        return None, msg, bool(exclude)
    return data["routes"][0]["geometry"]["coordinates"], None, bool(exclude)


def route_with_fallbacks(session, via, bearings, exclude=None):
    """Try the strictest request first, then loosen - reporting what was used."""
    for bg, ex in ((bearings, exclude), (None, exclude), (bearings, None), (None, None)):
        geom, err, applied = osrm_route(session, via, exclude=ex, bearings=bg)
        if geom:
            return geom, None, applied, bool(bg)
    return None, err, False, False


def line_bearing(line, chain):
    """Direction of travel along the route line at this chainage."""
    a = point_at(line, max(0.0, chain - 25.0))
    b = point_at(line, min(line["cum"][-1], chain + 25.0))
    return bearing_deg(a, b)


def path_len(geom):
    return sum(haversine_m(p, q) for p, q in zip(geom, geom[1:]))


def motorway_len(ix, geom):
    """Metres of a path that sit on a motorway (length, not point count)."""
    tot = 0.0
    for p, q in zip(geom, geom[1:]):
        if ix.on_motorway(float(p[0]), float(p[1])) and \
           ix.on_motorway(float(q[0]), float(q[1])):
            tot += haversine_m(p, q)
    return tot


def motorway_runs(ix, coords, min_pts=3):
    """Index ranges of `coords` that sit on a motorway."""
    runs, start = [], None
    for i, p in enumerate(list(coords) + [None]):
        bad = p is not None and ix.on_motorway(float(p[0]), float(p[1]))
        if bad and start is None:
            start = i
        elif not bad and start is not None:
            if i - start >= min_pts:
                runs.append((start, i - 1))
            start = None
    return runs


def motorway_points(ix, geom):
    return sum(1 for p in geom if ix.on_motorway(float(p[0]), float(p[1])))


def splice(coords, line, c0, c1, new_piece):
    """Replace the part of `coords` between chainages c0..c1 with new_piece."""
    pts, cum = line["pts"], line["cum"]
    head = [[lo, la] for (la, lo), c in zip(pts, cum) if c < c0]
    tail = [[lo, la] for (la, lo), c in zip(pts, cum) if c > c1]
    out, seen = [], None
    for p in head + [[round(x, 5), round(y, 5)] for x, y in new_piece] + tail:
        q = [round(p[0], 5), round(p[1], 5)]
        if q != seen:
            out.append(q)
            seen = q
    return out


def lone_is_real(rows, run, args):
    """
    Is a single off stop evidence about the LINE, or just about that stop?

    Usually the latter. 306|up stop #14 sits 43 m out while its neighbours are
    0 m and 4 m: the corridor there is perfect and the stop coordinate is not.
    Routing through such a point asked for 18 km of detour to replace 455 m of
    road. A wrong line drags its neighbours off too, so that is what is required
    before a lone stop counts.
    """
    if run[0]["off"] < args.lone:
        return False
    j = run[0]["i"]
    near = sorted(rows[k]["off"] for k in (j - 2, j - 1, j + 1, j + 2)
                  if 0 <= k < len(rows))
    return bool(near) and near[len(near) // 2] >= args.neighbour


def worst_off(line, stops):
    return max((project(line, la, lo)[0] for la, lo in stops), default=0.0)


def check_server():
    """
    Ask the router what it will and will not accept, one parameter at a time.

    The repair passes try several parameter combinations and quietly fall back,
    so a parameter the server dislikes never surfaces. Here each one is sent on
    its own, so a refusal is attributed to the right parameter instead of being
    guessed at - and the last test answers the question that decides whether a
    flyover can be repaired at all.
    """
    session = requests.Session()
    a, b = (28.5720, 77.2620), (28.5670, 77.3060)     # Ashram -> Noida, via DND
    coords = f"{a[1]},{a[0]};{b[1]},{b[0]}"
    base = {"overview": "full", "geometries": "geojson"}

    tests = [
        ("bare route", {}),
        ("+ radiuses", {"radiuses": "50;50"}),
        ("+ continue_straight", {"continue_straight": "false"}),
        ("+ bearings", {"bearings": f"90,{BEARING_RANGE};90,{BEARING_RANGE}"}),
        ("+ exclude=motorway", {"exclude": "motorway"}),
    ]
    print(f"router: {OSRM_ROUTE}\n")
    lengths = {}
    for label, extra in tests:
        try:
            r = session.get(OSRM_ROUTE + coords, params={**base, **extra}, timeout=40)
            data = r.json()
        except Exception as exc:
            print(f"  {label:<22} FAILED   {exc}")
            continue
        if r.status_code == 200 and data.get("code") == "Ok" and data.get("routes"):
            geom = data["routes"][0]["geometry"]["coordinates"]
            lengths[label] = path_len(geom)
            print(f"  {label:<22} ok       {lengths[label]:>7.0f} m, "
                  f"{len(geom)} points")
        else:
            msg = data.get("code") or f"HTTP {r.status_code}"
            if data.get("message"):
                msg += f" - {data['message']}"
            print(f"  {label:<22} REFUSED  {msg}")

    print()
    if "+ exclude=motorway" not in lengths:
        print("-> This server will not apply exclude=motorway, so a stretch that "
              "runs on a\n   flyover cannot be pushed off it here. The motorway "
              "pass will reject those\n   stretches rather than apply them - "
              "nothing gets worse, but nothing improves.")
    elif "bare route" in lengths and \
            abs(lengths["+ exclude=motorway"] - lengths["bare route"]) < 50:
        print("-> exclude=motorway is accepted but IGNORED - the same route came "
              "back. Same\n   effect as not supporting it.")
    else:
        print("-> exclude=motorway is honoured. The motorway pass can do its job.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="limit to these route numbers")
    ap.add_argument("--off", type=float, default=20.0)
    ap.add_argument("--lone", type=float, default=30.0)
    ap.add_argument("--neighbour", type=float, default=15.0,
                    help="a lone off stop only counts if its neighbours drift too")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    ap.add_argument("--skip-stops", action="store_true",
                    help="only do the motorway pass")
    ap.add_argument("--skip-motorway", action="store_true",
                    help="only do the stop-offset pass")
    ap.add_argument("--check", action="store_true",
                    help="ask the OSRM server whether it honours exclude=motorway, "
                         "then exit")
    args = ap.parse_args()

    if args.check:
        return check_server()

    corridors = json.load(open(CORRIDOR_FILE, encoding="utf-8-sig"))
    stops_all = json.load(open(STOPS_FILE, encoding="utf-8-sig"))
    session = requests.Session()
    fallback = OfflineRouter()
    if not fallback.ok:
        fallback = None

    fixed = kept = failed = 0
    for key in sorted(stops_all):
        name = key.split("|")[0]
        if args.only and name not in args.only:
            continue
        coords = corridors.get(key)
        stops = stops_all.get(key) or []
        line = build_line(coords) if coords else None
        if not line or len(stops) < 3:
            continue

        if args.skip_stops:
            continue

        rows = []
        for i, s in enumerate(stops):
            la, lo = float(s[1]), float(s[0])
            off, chain, side = project(line, la, lo)
            rows.append({"i": i, "lat": la, "lon": lo, "off": off,
                         "chain": chain, "side": side})

        # the same grouping the audit uses
        runs, run = [], []
        for r in rows + [None]:
            bad = r is not None and r["off"] > args.off
            same = bad and (not run or r["side"] == run[0]["side"])
            if bad and same:
                run.append(r)
                continue
            if run and (len(run) >= 2 or lone_is_real(rows, run, args)):
                runs.append(run)
            run = [r] if bad else []

        for run in runs:
            lo_i = max(0, run[0]["i"] - PAD_STOPS)
            hi_i = min(len(rows) - 1, run[-1]["i"] + PAD_STOPS)
            window = rows[lo_i:hi_i + 1]
            c0, c1 = window[0]["chain"], window[-1]["chain"]
            if c1 - c0 < 50:
                c0, c1 = c0 - 150, c1 + 150
            c0 = max(0.0, c0)
            c1 = min(line["cum"][-1], c1)

            a, b = point_at(line, c0), point_at(line, c1)
            via = [a] + [(r["lat"], r["lon"]) for r in window] + [b]
            bgs = ([line_bearing(line, c0)]
                   + [line_bearing(line, r["chain"]) for r in window]
                   + [line_bearing(line, c1)])
            geom, err, _, _ = route_with_fallbacks(session, via, bgs)
            if not geom and fallback is not None:
                # No internet, or the router refused. The offline graph has only
                # major roads, so it cannot thread through every stop - but it
                # can still answer, and the checks below decide whether its
                # answer is worth keeping.
                geom, err = fallback.route(a, b, bearing_a=bgs[0], bearing_b=bgs[-1])
            time.sleep(0.3)
            label = f"{key} stops #{run[0]['i']+1}-#{run[-1]['i']+1}"
            if not geom:
                print(f"  -- {label:<34} OSRM said: {err}")
                failed += 1
                continue

            new_len = path_len(geom)
            old_len = c1 - c0
            d0 = haversine_m((geom[0][0], geom[0][1]), (a[1], a[0]))
            d1 = haversine_m((geom[-1][0], geom[-1][1]), (b[1], b[0]))

            reason = None
            if d0 > END_TOL_M or d1 > END_TOL_M:
                reason = f"ends moved {d0:.0f}/{d1:.0f} m"
            elif old_len > 0 and new_len > old_len * MAX_LEN_RATIO:
                reason = f"replacement {new_len:.0f} m vs {old_len:.0f} m"
            if reason:
                print(f"  -- {label:<34} rejected: {reason}")
                kept += 1
                continue

            cand_coords = splice(coords, line, c0, c1, geom)
            cand_line = build_line(cand_coords)
            win_pts = [(r["lat"], r["lon"]) for r in window]
            before = worst_off(line, win_pts)
            after = worst_off(cand_line, win_pts) if cand_line else 1e9

            if after >= before or after > GOOD_OFF_M:
                print(f"  -- {label:<34} rejected: worst stop "
                      f"{before:.0f} m -> {after:.0f} m, no better")
                kept += 1
                continue

            print(f"  ok {label:<34} worst stop {before:>4.0f} m -> {after:>3.0f} m"
                  f"   ({old_len:.0f} m rebuilt through {len(window)} stops)")
            if not args.dry_run:
                corridors[key] = cand_coords
                coords = cand_coords
                line = cand_line
            fixed += 1

    # ---- second pass: stretches that sit on a motorway --------------------
    # A bus does not run on the DND Flyway or over the Ashram Flyover. Those
    # carry no stops, so the pass above is blind to them - the nearest stops sit
    # before and after, both close to the line. Here the stretch is rebuilt with
    # motorways excluded outright, which forces the path down onto the surface
    # road the bus actually uses.
    ix = RoadIndex() if not args.skip_motorway else None
    router = None
    if ix is not None and not ix.ok:
        print("\n(no data/delhi_roads_major.geojson - skipping the motorway pass)")
        ix = None
    if ix is not None:
        router = OfflineRouter()
        if not router.ok:
            ix = None
    if ix is not None:
        print("\n--- motorway pass ---")
        for key in sorted(corridors):
            name = key.split("|")[0]
            if args.only and name not in args.only:
                continue
            coords = corridors[key]
            line = build_line(coords)
            if not line:
                continue
            stops = stops_all.get(key) or []
            runs = motorway_runs(ix, coords)
            for lo_i, hi_i in reversed(runs):     # back to front: indices stay valid
                coords = corridors[key]
                line = build_line(coords)
                if not line or hi_i >= len(line["cum"]):
                    continue
                c0 = max(0.0, line["cum"][lo_i] - 250.0)
                c1 = min(line["cum"][-1], line["cum"][hi_i] + 250.0)
                a, b = point_at(line, c0), point_at(line, c1)
                inside = []
                for st in stops:
                    la, lo = float(st[1]), float(st[0])
                    off, chain, _ = project(line, la, lo)
                    if c0 < chain < c1 and off < 80:
                        inside.append((chain, la, lo))
                inside.sort()
                via = [a] + [(la, lo) for _, la, lo in inside] + [b]

                # Routed offline, on a graph the motorway edges were never put
                # into. The public OSRM server refuses exclude=motorway
                # ("Exclude flag combination is not supported"), so asking it to
                # keep off a flyover is simply not possible there; here the
                # exclusion is structural and cannot be declined.
                geom, err = router.route(
                    a, b,
                    bearing_a=line_bearing(line, c0),
                    bearing_b=line_bearing(line, c1))
                label = f"{key} @ {a[0]:.4f},{a[1]:.4f}"
                if not geom:
                    print(f"  -- {label:<34} {err}")
                    failed += 1
                    continue

                # Judge by metres on a motorway, not by point count. The surface
                # road runs within 20 m of the ramp at an interchange, so a
                # correct path always brushes a point or two; what matters is
                # whether it RUNS on the motorway.
                was = motorway_len(ix, [(p[0], p[1]) for p in coords[lo_i:hi_i + 1]])
                left = motorway_len(ix, geom)
                new_len = path_len(geom)
                old_len = c1 - c0
                d0 = haversine_m((geom[0][0], geom[0][1]), (a[1], a[0]))
                d1 = haversine_m((geom[-1][0], geom[-1][1]), (b[1], b[0]))

                reason = None
                if left > RESIDUAL_MOTORWAY_M and left > was * 0.3:
                    reason = f"still {left:.0f} m on a motorway (was {was:.0f} m)"
                elif d0 > END_TOL_M or d1 > END_TOL_M:
                    reason = f"ends moved {d0:.0f}/{d1:.0f} m"
                elif old_len > 0 and new_len > old_len * 1.8:
                    reason = f"replacement {new_len:.0f} m vs {old_len:.0f} m"
                if reason:
                    print(f"  -- {label:<34} rejected: {reason}")
                    kept += 1
                    continue

                cand = splice(coords, line, c0, c1, geom)
                cand_line = build_line(cand)
                win = [(la, lo) for _, la, lo in inside]
                before = worst_off(line, win) if win else 0.0
                after = worst_off(cand_line, win) if (win and cand_line) else 0.0
                if win and after > max(before, GOOD_OFF_M):
                    print(f"  -- {label:<34} rejected: stops would move "
                          f"{before:.0f} m -> {after:.0f} m")
                    kept += 1
                    continue

                print(f"  ok {label:<34} motorway {was:>5.0f} m -> {left:.0f} m"
                      f"  ({len(inside)} stops kept, worst {after:.0f} m)")
                if not args.dry_run:
                    corridors[key] = cand
                fixed += 1

    print(f"\n{fixed} stretches repaired, {kept} left alone (no improvement), "
          f"{failed} could not be routed")
    if args.dry_run:
        print("dry run - nothing written")
    elif fixed:
        with open(CORRIDOR_FILE, "w", encoding="utf-8") as fh:
            json.dump(corridors, fh, separators=(",", ":"))
        print(f"written -> data/route_corridors.json")
        print("Now re-run:  python tools/audit_corridors.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
