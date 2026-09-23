"""
A lookup from a point to the OSM road under it, built from
data/delhi_roads_major.geojson (tools/download_delhi_osm.py writes it).

Used by the corridor audit and repair for one specific question: is this bit of
route line sitting on a motorway? A DTC bus does not use the DND Flyway or the
Ashram Flyover - those have no stops and buses are not allowed on some of them -
so a corridor point whose road is `motorway` or `motorway_link` is wrong by
class, no matter how plausible it looks on screen.

This catches what the stop test cannot. A flyover carries no stops at all, so the
nearest stops sit comfortably before and after it, each close to the line, and
the stop test sees nothing wrong. The road class sees it at once.
"""

import json
import math
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROADS_FILE = os.path.join(ROOT, "data", "delhi_roads_major.geojson")

MOTORWAY = {"motorway", "motorway_link"}
CELL_M = 150.0
_LAT0 = 28.62
_MX = 111320.0 * math.cos(math.radians(_LAT0))
_MY = 110540.0


def _flat(v):
    """osmnx merges parallel edges, so a tag can be a list."""
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(_flat(x))
        return out
    return [v] if v is not None else []


def is_motorway(highway):
    return any(str(h) in MOTORWAY for h in _flat(highway))


class RoadIndex:
    def __init__(self, path=ROADS_FILE):
        self.grid = defaultdict(list)
        self.ok = os.path.exists(path)
        if not self.ok:
            return
        with open(path, encoding="utf-8") as fh:
            gj = json.load(fh)
        for f in gj.get("features", []):
            g = f.get("geometry") or {}
            pr = f.get("properties") or {}
            t = g.get("type")
            if t == "LineString":
                lines = [g.get("coordinates") or []]
            elif t == "MultiLineString":
                lines = g.get("coordinates") or []
            else:
                continue
            name = "/".join(sorted(set(str(x) for x in _flat(pr.get("name"))))) or "?"
            hw = pr.get("highway")
            mot = is_motorway(hw)
            hwtxt = "/".join(sorted(set(str(x) for x in _flat(hw)))) or "?"
            for cs in lines:
                for a, b in zip(cs, cs[1:]):
                    self._add(a, b, name, hwtxt, mot)

    def _add(self, a, b, name, hw, mot):
        A = (a[0] * _MX, a[1] * _MY)
        B = (b[0] * _MX, b[1] * _MY)
        rec = (A, B, name, hw, mot)
        x0, x1 = sorted((A[0], B[0]))
        y0, y1 = sorted((A[1], B[1]))
        for cx in range(int(x0 // CELL_M), int(x1 // CELL_M) + 1):
            for cy in range(int(y0 // CELL_M), int(y1 // CELL_M) + 1):
                self.grid[(cx, cy)].append(rec)

    def nearest(self, lon, lat, rings=2):
        """(distance_m, road_name, highway, is_motorway) - inf if nothing near."""
        P = (lon * _MX, lat * _MY)
        best = (float("inf"), "?", "?", False)
        cx, cy = int(P[0] // CELL_M), int(P[1] // CELL_M)
        for i in range(-rings, rings + 1):
            for j in range(-rings, rings + 1):
                for A, B, name, hw, mot in self.grid.get((cx + i, cy + j), ()):
                    dx, dy = B[0] - A[0], B[1] - A[1]
                    den = dx * dx + dy * dy
                    t = 0.0 if den == 0 else max(
                        0.0, min(1.0, ((P[0] - A[0]) * dx + (P[1] - A[1]) * dy) / den))
                    d = math.hypot(P[0] - (A[0] + t * dx), P[1] - (A[1] + t * dy))
                    if d < best[0]:
                        best = (d, name, hw, mot)
        return best

    def on_motorway(self, lon, lat, tol_m=20.0):
        """
        True only when the NEAREST road is a motorway and it is genuinely close.

        A surface road running beside a flyover is often the second-nearest, so
        the nearest-wins rule is what separates 'on the flyover' from 'beside
        it'. The tolerance keeps a point that is far from every road - typically
        a service lane missing from the major-roads extract - out of the count.
        """
        d, _, _, mot = self.nearest(lon, lat)
        return mot and d <= tol_m
