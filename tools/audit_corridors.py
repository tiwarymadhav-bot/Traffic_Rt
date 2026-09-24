"""
Find the stretches where a route corridor left the road the bus actually uses.

The trail is painted straight along `data/route_corridors.json`, so a trail on a
flyover means the *corridor* is on the flyover. The bus is not: it has to serve
its stops, and a flyover has none. That makes the stop list the ground truth.

So every stop is projected onto its own route line and the offset is measured.
One stop sitting 30 m off is ordinary GPS/tagging noise. Several *consecutive*
stops sitting well off, all on the SAME side, is the signature of a line that
climbed onto the flyover while the stops stayed on the service road below.

Read-only - this only reports. tools/repair_corridors.py does the fixing.

    python tools/audit_corridors.py
    python tools/audit_corridors.py --off 25 --run 2
    python tools/audit_corridors.py --only 543 534 OMS
    python tools/audit_corridors.py --json data/corridor_audit.json
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from road_index import RoadIndex

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORRIDOR_FILE = os.path.join(ROOT, "data", "route_corridors.json")
STOPS_FILE = os.path.join(ROOT, "data", "route_stops.json")


def build_line(coords):
    pts = []
    for c in coords:
        try:
            p = (float(c[1]), float(c[0]))
        except (TypeError, ValueError, IndexError):
            continue
        if not pts or pts[-1] != p:
            pts.append(p)
    if len(pts) < 2:
        return None
    cum = [0.0]
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        cum.append(cum[-1] + haversine_m((lo1, la1), (lo2, la2)))
    return {"pts": pts, "cum": cum}


def haversine_m(a, b):
    r = math.radians
    dlon, dlat = r(b[0] - a[0]), r(b[1] - a[1])
    h = (math.sin(dlat / 2) ** 2
         + math.cos(r(a[1])) * math.cos(r(b[1])) * math.sin(dlon / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def project(line, lat, lon):
    """(offset_m, chainage_m, side) - side is +1 left of travel, -1 right."""
    pts, cum = line["pts"], line["cum"]
    mx = 111320.0 * math.cos(math.radians(lat))
    my = 110540.0
    px, py = lon * mx, lat * my
    best = (float("inf"), 0.0, 0)
    for i in range(len(pts) - 1):
        (alat, alon), (blat, blon) = pts[i], pts[i + 1]
        ax, ay, bx, by = alon * mx, alat * my, blon * mx, blat * my
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        t = 0.0 if den == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        if d < best[0]:
            cross = dx * (py - ay) - dy * (px - ax)
            best = (d, cum[i] + t * (cum[i + 1] - cum[i]), 1 if cross >= 0 else -1)
    return best


def point_at(line, chain):
    pts, cum = line["pts"], line["cum"]
    chain = max(0.0, min(chain, cum[-1]))
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= chain:
            lo = mid
        else:
            hi = mid
    span = cum[lo + 1] - cum[lo]
    t = 0.0 if span <= 0 else (chain - cum[lo]) / span
    (alat, alon), (blat, blon) = pts[lo], pts[lo + 1]
    return (alat + (blat - alat) * t, alon + (blon - alon) * t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", type=float, default=20.0,
                    help="a stop further than this from its line is 'off' (m)")
    ap.add_argument("--lone", type=float, default=30.0,
                    help="a single stop this far off is reported on its own (m)")
    ap.add_argument("--neighbour", type=float, default=15.0,
                    help="a lone off stop only counts if its neighbours are also "
                         "at least this far off (m)")
    ap.add_argument("--run", type=int, default=2,
                    help="how many consecutive off stops make a suspect stretch")
    ap.add_argument("--cluster", type=float, default=120.0,
                    help="findings within this distance are one place (m)")
    ap.add_argument("--only", nargs="*", help="limit to these route numbers")
    ap.add_argument("--json", help="also write the findings to this file")
    ap.add_argument("--no-motorway-check", action="store_true",
                    help="skip the 'is this line on a motorway' test")
    ap.add_argument("--all-stops", action="store_true",
                    help="list every off stop, not just the stretches")
    ap.add_argument("--serves", type=float, default=40.0,
                    help="a stop further than this from its line is not served (m)")
    args = ap.parse_args()

    for f in (CORRIDOR_FILE, STOPS_FILE):
        if not os.path.exists(f):
            print(f"missing {f} - run tools/build_route_corridors.py first")
            return 1
    corridors = json.load(open(CORRIDOR_FILE, encoding="utf-8-sig"))
    stops_all = json.load(open(STOPS_FILE, encoding="utf-8-sig"))

    findings = []
    served = {}
    tot_stops = tot_off = 0
    per_route = {}

    for key, stops in sorted(stops_all.items()):
        name = key.split("|")[0]
        if args.only and name not in args.only:
            continue
        coords = corridors.get(key)
        line = build_line(coords) if coords else None
        if not line or not stops:
            continue

        rows = []
        for i, s in enumerate(stops):
            try:
                lon, lat = float(s[0]), float(s[1])
            except (TypeError, ValueError, IndexError):
                continue
            off, chain, side = project(line, lat, lon)
            rows.append({"i": i, "lat": lat, "lon": lon,
                         "off": off, "chain": chain, "side": side})
        if not rows:
            continue
        offs = sorted(r["off"] for r in rows)
        back = sum(1 for a, b in zip(rows, rows[1:]) if b["chain"] < a["chain"])
        served[key] = {
            "stops": len(rows),
            "median": offs[len(offs) // 2],
            "p90": offs[min(len(offs) - 1, int(len(offs) * 0.9))],
            "worst": offs[-1],
            "missed": sum(1 for o in offs if o > args.serves),
            "backwards": back,
        }
        tot_stops += len(rows)
        off_rows = [r for r in rows if r["off"] > args.off]
        tot_off += len(off_rows)
        per_route[key] = (len(rows), len(off_rows),
                          max(r["off"] for r in rows))

        # consecutive off stops on the same side = a stretch, not noise
        run = []
        for r in rows + [None]:
            bad = r is not None and r["off"] > args.off
            same = bad and (not run or r["side"] == run[0]["side"])
            if bad and same:
                run.append(r)
                continue
            if 0 < len(run) < args.run:
                # A single stop far from an otherwise perfect line is almost
                # always the stop's own coordinate, not the corridor: 306|up
                # stop #14 is 43 m out while #13 and #15 are 0 m and 4 m. Asking
                # a router to drive through such a point produced 18 km of
                # detour for 455 m of road. So a lone stop only counts when its
                # neighbours are drifting too - that is what a wrong line looks
                # like.
                j = run[0]["i"]
                near = [rows[k]["off"] for k in (j - 2, j - 1, j + 1, j + 2)
                        if 0 <= k < len(rows)]
                near.sort()
                median = near[len(near) // 2] if near else 0.0
                if run[0]["off"] < args.lone or median < args.neighbour:
                    run = [r] if bad else []
                    continue
            if run:
                c0 = min(x["chain"] for x in run)
                c1 = max(x["chain"] for x in run)
                mid = point_at(line, (c0 + c1) / 2)
                findings.append({
                    "route": key,
                    "stops": len(run),
                    "from_stop": run[0]["i"] + 1,
                    "to_stop": run[-1]["i"] + 1,
                    "chain_from": round(c0), "chain_to": round(c1),
                    "length_m": round(c1 - c0),
                    "mean_off_m": round(sum(x["off"] for x in run) / len(run)),
                    "max_off_m": round(max(x["off"] for x in run)),
                    "side": "left" if run[0]["side"] > 0 else "right",
                    "lat": round(mid[0], 5), "lon": round(mid[1], 5),
                })
            run = [r] if bad else []

    # One bad junction shows up once per route that passes through it. Group the
    # findings by place, so the list is of PLACES to fix, not of routes.
    places = []
    for f in sorted(findings, key=lambda x: -x["max_off_m"]):
        for p in places:
            if haversine_m((p["lon"], p["lat"]), (f["lon"], f["lat"])) <= args.cluster:
                p["routes"].append(f["route"])
                p["max_off_m"] = max(p["max_off_m"], f["max_off_m"])
                p["length_m"] = max(p["length_m"], f["length_m"])
                break
        else:
            places.append({"lat": f["lat"], "lon": f["lon"], "routes": [f["route"]],
                           "max_off_m": f["max_off_m"], "length_m": f["length_m"],
                           "side": f["side"]})
    places.sort(key=lambda p: (-len(p["routes"]), -p["max_off_m"]))

    # ---- the question that actually matters ---------------------------
    # Does each line go where its bus goes? A bus is defined by the stops it
    # serves, so the test is whether the line passes close to every stop, in
    # order. Whether it does that on a flyover or under one is not the point.
    bad = {k: v for k, v in served.items() if v["missed"] or v["backwards"]}
    print(f"\n=== does each route line follow its own stops? ===\n")
    print(f"{len(served) - len(bad)} of {len(served)} route lines pass every one "
          f"of their stops within {args.serves:.0f} m, in order.\n")
    if bad:
        print(f"  {'route':<16}{'stops':>6}{'median':>8}{'p90':>6}{'worst':>7}"
              f"{'missed':>8}{'out of order':>14}")
        for k, v in sorted(bad.items(), key=lambda kv: (-kv[1]["missed"],
                                                        -kv[1]["worst"])):
            print(f"  {k:<16}{v['stops']:>6}{v['median']:>7.0f}m{v['p90']:>5.0f}m"
                  f"{v['worst']:>6.0f}m{v['missed']:>8}{v['backwards']:>14}")
        print(f"\n  'missed'       = stops further than {args.serves:.0f} m from "
              f"the line - the bus could not stop there without leaving it.")
        print("  'out of order' = the line reaches a later stop before an earlier "
              "one, so the\n                   sequence doubles back.")

    print(f"\n{tot_stops} stops checked on {len(per_route)} route lines; "
          f"{tot_off} sit more than {args.off:.0f} m off "
          f"({100 * tot_off / max(1, tot_stops):.1f}%)\n")

    if not places:
        print("no suspect places - every line follows its own stops")
    else:
        print(f"{len(places)} suspect places ({len(findings)} route stretches):\n")
        for i, p in enumerate(places[:25], 1):
            rs = ", ".join(sorted(set(p["routes"]))[:6])
            more = len(set(p["routes"])) - 6
            print(f"  {i:>2}. {p['max_off_m']:>3} m off, {p['length_m']:>4} m long, "
                  f"stops on the {p['side']}")
            print(f"      https://www.openstreetmap.org/#map=18/{p['lat']}/{p['lon']}")
            print(f"      {len(set(p['routes']))} route(s): {rs}"
                  f"{f' +{more} more' if more > 0 else ''}")
        if len(places) > 25:
            print(f"  ... and {len(places) - 25} more places")

    if args.all_stops:
        print("\nworst routes by share of off stops:")
        for key, (n, off, mx) in sorted(per_route.items(),
                                        key=lambda kv: -kv[1][1] / max(1, kv[1][0]))[:15]:
            print(f"  {key:<16} {off:>3}/{n:<3} off  worst {mx:>4.0f} m")

    # ---- second test: is the line itself on a motorway? -------------------
    # A flyover has no stops, so the stop test above is blind to it: the nearest
    # stops sit before and after, both close to the line. The road class is not
    # blind. A DTC bus does not run on the DND Flyway or over the Ashram
    # Flyover, so a corridor point whose nearest road is motorway is wrong by
    # class alone.
    mot_places = []
    if not args.no_motorway_check:
        ix = RoadIndex()
        if not ix.ok:
            print("\n(no data/delhi_roads_major.geojson - skipping the motorway "
                  "test; build it with tools/download_delhi_osm.py)")
        else:
            runs = []
            for key, coords in sorted(corridors.items()):
                name = key.split("|")[0]
                if args.only and name not in args.only:
                    continue
                run = []
                for p in list(coords) + [None]:
                    bad = p is not None and ix.on_motorway(float(p[0]), float(p[1]))
                    if bad:
                        run.append(p)
                        continue
                    if len(run) >= 3:
                        mid = run[len(run) // 2]
                        length = sum(haversine_m(a, b) for a, b in zip(run, run[1:]))
                        runs.append({"route": key, "points": len(run),
                                     "length_m": round(length),
                                     "lat": round(float(mid[1]), 5),
                                     "lon": round(float(mid[0]), 5),
                                     "road": ix.nearest(float(mid[0]), float(mid[1]))[1]})
                    run = []
            for f in sorted(runs, key=lambda x: -x["length_m"]):
                for p in mot_places:
                    if haversine_m((p["lon"], p["lat"]), (f["lon"], f["lat"])) <= args.cluster * 4:
                        p["routes"].append(f["route"])
                        p["length_m"] = max(p["length_m"], f["length_m"])
                        break
                else:
                    mot_places.append({"lat": f["lat"], "lon": f["lon"], "road": f["road"],
                                       "length_m": f["length_m"], "routes": [f["route"]]})
            mot_places.sort(key=lambda p: (-len(p["routes"]), -p["length_m"]))

            print(f"\n--- FYI: stretches on an expressway or flyover ---")
            print("    Not wrong by itself - some routes really do use one. It "
                  "only matters when\n    the stops above say the line is not "
                  "serving them.\n")
            if not mot_places:
                print("  none - no route line uses an expressway or flyover ramp")
            for i, p in enumerate(mot_places[:20], 1):
                rs = sorted(set(p["routes"]))
                print(f"  {i:>2}. {p['length_m']:>4} m on {p['road']}")
                print(f"      https://www.openstreetmap.org/#map=17/{p['lat']}/{p['lon']}")
                print(f"      {len(rs)} route(s): {', '.join(rs[:8])}"
                      f"{f' +{len(rs) - 8} more' if len(rs) > 8 else ''}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"served": served, "places": places,
                       "stretches": findings, "motorway": mot_places}, fh, indent=1)
        print(f"\nwritten -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
