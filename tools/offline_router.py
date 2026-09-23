"""
A tiny router over data/delhi_roads_major.geojson - no internet, no API key.

The public OSRM server refuses `exclude=motorway` ("Exclude flag combination is
not supported"), so it cannot be told to keep a bus off the Ashram Flyover or the
DND Flyway. That leaves the flyover stretches unfixable through it.

The file we already have is an osmnx edge export: every feature carries `u` and
`v` node ids, a length and a oneway flag, so it IS a graph. Building it without
the motorway edges and running Dijkstra over what remains gives a path that
*cannot* use a motorway - the exclusion is structural, not a request a server may
decline. 140 of 7306 edges are motorway and the rest stay connected as one
component, so nothing useful is lost by dropping them.

Only major roads are in the extract, so the path follows the surface main road
rather than a service lane. For a flyover that is exactly right: the bus runs on
the surface carriageway underneath.
"""

import heapq
import json
import math
import os
from collections import defaultdict

from road_index import ROADS_FILE, is_motorway

_LAT0 = 28.62
_MX = 111320.0 * math.cos(math.radians(_LAT0))
_MY = 110540.0
CELL_M = 300.0


def _xy(lon, lat):
    return (lon * _MX, lat * _MY)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle(a, b):
    """Smallest angle between two bearings, in degrees."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


class OfflineRouter:
    def __init__(self, path=ROADS_FILE, allow_motorway=False):
        self.ok = os.path.exists(path)
        self.adj = defaultdict(list)      # node -> [(other, cost, geometry)]
        self.node_xy = {}
        self.edges = []                   # (A, B, geom, u, v, oneway)
        self.grid = defaultdict(list)
        if not self.ok:
            return
        with open(path, encoding="utf-8") as fh:
            gj = json.load(fh)
        for f in gj.get("features", []):
            g = f.get("geometry") or {}
            pr = f.get("properties") or {}
            if g.get("type") != "LineString":
                continue
            cs = g.get("coordinates") or []
            if len(cs) < 2:
                continue
            if not allow_motorway and is_motorway(pr.get("highway")):
                continue
            u, v = pr.get("u"), pr.get("v")
            if u is None or v is None:
                continue
            length = float(pr.get("length") or 0) or sum(
                _dist(_xy(*a), _xy(*b)) for a, b in zip(cs, cs[1:]))
            oneway = bool(pr.get("oneway"))
            self.node_xy[u] = _xy(*cs[0])
            self.node_xy[v] = _xy(*cs[-1])
            self.adj[u].append((v, length, cs))
            if not oneway:
                self.adj[v].append((u, length, list(reversed(cs))))
            idx = len(self.edges)
            self.edges.append((u, v, cs, length, oneway))
            self._index(idx, cs)

    def _index(self, idx, cs):
        for a, b in zip(cs, cs[1:]):
            A, B = _xy(*a), _xy(*b)
            x0, x1 = sorted((A[0], B[0]))
            y0, y1 = sorted((A[1], B[1]))
            for cx in range(int(x0 // CELL_M), int(x1 // CELL_M) + 1):
                for cy in range(int(y0 // CELL_M), int(y1 // CELL_M) + 1):
                    self.grid[(cx, cy)].append(idx)

    def candidates(self, lat, lon, rings=3, bearing=None, max_m=60.0, k=8):
        """
        Up to k nearby edges, nearest first, as
        (distance_m, edge_index, position_along_edge_m, edge_bearing).

        One candidate is not enough. The extract holds major roads only, so a
        one-way edge can simply stop where the road continues into a class that
        was not exported - snap to that and the graph goes nowhere. Offering
        Dijkstra every edge within reach lets it use whichever one is actually
        connected.
        """
        P = _xy(lon, lat)
        cx, cy = int(P[0] // CELL_M), int(P[1] // CELL_M)
        out, seen = [], set()
        for i in range(-rings, rings + 1):
            for j in range(-rings, rings + 1):
                for idx in self.grid.get((cx + i, cy + j), ()):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    _, _, cs, _, _ = self.edges[idx]
                    run, best = 0.0, None
                    for a, b in zip(cs, cs[1:]):
                        A, B = _xy(*a), _xy(*b)
                        dx, dy = B[0] - A[0], B[1] - A[1]
                        den = dx * dx + dy * dy
                        t = 0.0 if den == 0 else max(
                            0.0, min(1.0, ((P[0] - A[0]) * dx + (P[1] - A[1]) * dy) / den))
                        d = math.hypot(P[0] - (A[0] + t * dx), P[1] - (A[1] + t * dy))
                        if best is None or d < best[0]:
                            brg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
                            best = (d, idx, run + t * math.hypot(dx, dy), brg)
                        run += math.hypot(dx, dy)
                    if best:
                        out.append(best)
        if not out:
            return []
        out.sort(key=lambda c: c[0])
        limit = max(max_m, out[0][0] + 20.0)
        return [c for c in out if c[0] <= limit][:k]

    def nearest_edge(self, lat, lon, rings=3, bearing=None, near_m=45.0):
        cs = self.candidates(lat, lon, rings=rings, bearing=bearing, max_m=near_m)
        if not cs:
            return None
        if bearing is not None:
            for c in cs:
                _, _, _, _, oneway = self.edges[c[1]]
                if not oneway or _angle(c[3], bearing) <= 90:
                    return c[:3]
        return cs[0][:3]

    def _dijkstra(self, sources, targets):
        """sources/targets: {node: extra_cost}. -> (cost, [nodes]) or None."""
        dist = dict(sources)
        prev = {}
        pq = [(c, n) for n, c in sources.items()]
        heapq.heapify(pq)
        best = None
        while pq:
            c, n = heapq.heappop(pq)
            if c > dist.get(n, math.inf):
                continue
            if n in targets:
                total = c + targets[n]
                if best is None or total < best[0]:
                    best = (total, n)
            if best and c > best[0]:
                break
            for m, w, _ in self.adj.get(n, ()):
                nc = c + w
                if nc < dist.get(m, math.inf):
                    dist[m] = nc
                    prev[m] = n
                    heapq.heappush(pq, (nc, m))
        if not best:
            return None
        path, n = [], best[1]
        while n is not None:
            path.append(n)
            n = prev.get(n)
        return best[0], list(reversed(path))

    def _geom_between(self, a, b):
        for m, _, cs in self.adj.get(a, ()):
            if m == b:
                return cs
        return None

    @staticmethod
    def _split(cs, pos):
        """
        Cut an edge's geometry at `pos` metres along it.

        Returns (head, tail): head runs start -> cut point, tail runs cut point
        -> end. Both are in the edge's own direction, so a piece travelled the
        other way has to be reversed by the caller.
        """
        pts = [list(p) for p in cs]
        run = 0.0
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            seg = _dist(_xy(*a), _xy(*b))
            if run + seg >= pos:
                t = 0.0 if seg == 0 else max(0.0, min(1.0, (pos - run) / seg))
                mid = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
                return pts[:i + 1] + [mid], [mid] + pts[i + 1:]
            run += seg
        return pts, [pts[-1]]

    def route(self, a, b, snap_m=120.0, bearing_a=None, bearing_b=None,
              wrong_way_penalty_m=200.0):
        """
        a, b = (lat, lon) -> ([[lon, lat], ...], None) or (None, reason).

        Every nearby edge at each end becomes a way in or out, priced by how far
        it is and whether it runs the bus's way, and Dijkstra picks the cheapest
        combination that actually connects. A dead-ended one-way stub therefore
        costs nothing more than being ignored.
        """
        ca = self.candidates(*a, bearing=bearing_a)
        cb = self.candidates(*b, bearing=bearing_b)
        if not ca or not cb:
            return None, "no road near an end"
        if ca[0][0] > snap_m or cb[0][0] > snap_m:
            return None, f"ends {ca[0][0]:.0f}/{cb[0][0]:.0f} m from any major road"

        def pen(c, bearing):
            if bearing is None:
                return 0.0
            return 0.0 if _angle(c[3], bearing) <= 90 else wrong_way_penalty_m

        sources, src_via = {}, {}
        for d, idx, pos, brg in ca:
            u, v, cs, ln, oneway = self.edges[idx]
            base = d + pen((d, idx, pos, brg), bearing_a)
            head, tail = self._split(cs, pos)
            for node, cost, geom in ((v, max(0.0, ln - pos), tail),
                                     (u, pos, list(reversed(head)))):
                if node == u and oneway:
                    continue
                c = base + cost
                if c < sources.get(node, math.inf):
                    sources[node] = c
                    src_via[node] = geom

        targets, tgt_via = {}, {}
        for d, idx, pos, brg in cb:
            u, v, cs, ln, oneway = self.edges[idx]
            base = d + pen((d, idx, pos, brg), bearing_b)
            head, tail = self._split(cs, pos)
            for node, cost, geom in ((u, pos, head),
                                     (v, max(0.0, ln - pos), list(reversed(tail)))):
                if node == v and oneway:
                    continue
                c = base + cost
                if c < targets.get(node, math.inf):
                    targets[node] = c
                    tgt_via[node] = geom

        if not sources or not targets:
            return None, "no usable road direction at an end"
        got = self._dijkstra(sources, targets)
        if not got:
            return None, "no path on the non-motorway network"
        _, nodes = got

        out = [[a[1], a[0]]]

        def push(pts):
            for p in pts:
                q = [round(float(p[0]), 5), round(float(p[1]), 5)]
                if q != out[-1]:
                    out.append(q)

        push(src_via.get(nodes[0]) or [])
        for x, y in zip(nodes, nodes[1:]):
            cs = self._geom_between(x, y)
            if not cs:
                return None, "graph gap"
            push(cs)
        push(tgt_via.get(nodes[-1]) or [])
        push([[b[1], b[0]]])
        return out, None
