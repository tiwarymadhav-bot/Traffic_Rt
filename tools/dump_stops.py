"""
Show what a DTC route page actually publishes about its stops.

The corridor builder reads `_mapData.route_coords` for the route line and
`_mapData.stops` for the stops. The line works; the stops came back empty, which
means they are shaped differently than assumed. Run this once and paste the
output - it prints the page's own structure, nothing is guessed.

    python tools/dump_stops.py            # uses route 85
    python tools/dump_stops.py 463
"""

import json
import re
import sys

import requests

from build_route_corridors import (BASE, HEADERS, extract_map_data,
                                   fetch_route_page, search_candidates)


def preview(v, n=200):
    t = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return t[:n] + (" ..." if len(t) > n else "")


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "85"
    session = requests.Session()
    session.get(BASE + "/", headers=HEADERS, timeout=25)

    sid = None
    with open("backend/routes.json", encoding="utf-8-sig") as fh:
        for row in json.load(fh).get("routes", []):
            if row["route"] == name and row.get("sid"):
                sid = row["sid"]
                break
    if not sid:
        cands = search_candidates(session, name)
        if not cands:
            print(f"no search result for {name}")
            return 1
        sid = cands[0]

    uid, html = fetch_route_page(session, sid)
    if not html:
        print(f"could not open the page for {name} (sid {sid})")
        return 1
    print(f"route {name}  sid={sid}  uid={uid}  page {len(html)} bytes\n")

    md = extract_map_data(html) or {}
    print("_mapData keys and what each holds:")
    for k, v in md.items():
        kind = type(v).__name__
        size = f"[{len(v)}]" if isinstance(v, (list, dict, str)) else ""
        print(f"  {k:<22} {kind}{size:<8} {preview(v, 120)}")

    stops = md.get("stops")
    print(f"\n_mapData.stops -> {type(stops).__name__}, "
          f"{len(stops) if isinstance(stops, (list, dict)) else '-'} entries")
    if isinstance(stops, list) and stops:
        for i, st in enumerate(stops[:3]):
            print(f"  [{i}] {type(st).__name__}: {preview(st, 300)}")
    elif isinstance(stops, dict):
        for i, (k, v) in enumerate(list(stops.items())[:3]):
            print(f"  {k!r}: {preview(v, 300)}")

    # stops may not live in _mapData at all - look for other likely variables
    print("\nother stop-ish variables on the page:")
    found = False
    for m in re.finditer(r"(?:var|let|const)?\s*(_?\w*[Ss]top\w*)\s*=\s*([\[{])", html):
        var, opener = m.group(1), m.group(2)
        start = m.start(2)
        depth, in_str, esc, end = 0, False, False, None
        close = "]" if opener == "[" else "}"
        for p in range(start, min(len(html), start + 400000)):
            ch = html[p]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch in "\"'":
                    in_str = False
                continue
            if ch in "\"'":
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == close:
                depth -= 1
                if depth == 0:
                    end = p + 1
                    break
        body = html[start:end] if end else html[start:start + 200]
        print(f"  {var:<22} {len(body):>7} chars  {preview(body, 220)}")
        found = True
    if not found:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
