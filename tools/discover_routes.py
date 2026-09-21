"""
Route UID discovery for the Delhi live-traffic backend.
=======================================================
The DTC live-bus API is keyed by an internal route UID (e.g. 2343), NOT by the
public bus number (85). This script resolves any bus number to its UIDs for
both directions and appends them to backend/routes.json.

Usage (run from the project root, needs internet):

    python tools/discover_routes.py 764 502A 181
    python tools/discover_routes.py --contains OMS
    python tools/discover_routes.py 764 --dry-run

How it works:
    1. /ajax/bus/search/?q=<num>   -> "<search_id>|<route name> (from -> to)"
    2. /bus/search/?id=<search_id> -> page contains  _liveBusRouteUID = '<uid>'
    3. that uid is what /api/live/buses/ accepts as route_id
"""

import argparse
import json
import os
import re
import sys

import requests

BASE = "https://www.dtcbusroutes.in"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": BASE + "/"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES_FILE = os.path.join(ROOT, "backend", "routes.json")

UID_RE = re.compile(r"_liveBusRouteUID\s*=\s*'([^']*)'")


def search(session, query, limit=60):
    """Return [{'sid', 'name', 'from', 'to'}] for a search term."""
    r = session.get(
        f"{BASE}/ajax/bus/search/",
        params={"q": query, "limit": limit},
        headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    if r.status_code != 200 or "DOCTYPE" in r.text[:200]:
        return []

    rows = []
    for line in r.text.splitlines():
        if "|" not in line:
            continue
        sid, rest = line.split("|", 1)
        name, frm, to = rest.strip(), "", ""
        if "(" in rest:
            name = rest.split("(")[0].strip()
            od = rest[rest.index("(") + 1:].rstrip(")").strip()
            parts = re.split(r"→|->", od)
            frm = parts[0].strip() if parts else ""
            to = parts[1].strip() if len(parts) > 1 else ""
        rows.append({"sid": sid.strip(), "name": name, "from": frm, "to": to})
    return rows


def live_uid(session, sid):
    r = session.get(f"{BASE}/bus/search/", params={"id": sid}, headers=HEADERS, timeout=25)
    m = UID_RE.search(r.text)
    return m.group(1) if m else None


def resolve(session, term, contains=False):
    rows = search(session, term)
    if contains:
        hits = [r for r in rows if term.upper() in r["name"].upper()]
    else:
        norm = lambda s: re.sub(r"\s*-\s*Cluster$", "", s, flags=re.I).strip().upper()
        hits = [r for r in rows if norm(r["name"]) == term.strip().upper()]

    out = []
    for i, h in enumerate(hits):
        uid = live_uid(session, h["sid"])
        if not uid:
            print(f"   ! no live UID for {h['name']} ({h['sid']})")
            continue
        out.append(
            {
                "route": h["name"].replace(" - Cluster", ""),
                "direction": "up" if i % 2 == 0 else "down",
                "uid": uid,
                "from": h["from"],
                "to": h["to"],
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("terms", nargs="+", help="bus numbers, e.g. 764 502A")
    ap.add_argument("--contains", action="store_true",
                    help="match any route whose name CONTAINS the term (use for OMS etc.)")
    ap.add_argument("--dry-run", action="store_true", help="print only, do not write routes.json")
    args = ap.parse_args()

    with open(ROUTES_FILE, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    existing = {str(r["uid"]) for r in cfg.get("routes", [])}

    session = requests.Session()
    session.get(BASE + "/", headers=HEADERS, timeout=20)

    added = []
    for term in args.terms:
        print(f"[*] {term}")
        for row in resolve(session, term, contains=args.contains):
            mark = "dup" if row["uid"] in existing else "NEW"
            print(f"    {mark}  {row['route']:<12} {row['direction']:<4} uid={row['uid']:<6} "
                  f"{row['from']} -> {row['to']}")
            if row["uid"] not in existing:
                existing.add(row["uid"])
                added.append(row)

    if not added:
        print("\nNothing new to add.")
        return
    if args.dry_run:
        print(f"\n[dry-run] {len(added)} route(s) would be added.")
        return

    cfg["routes"].extend(added)
    with open(ROUTES_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    print(f"\n[+] {len(added)} route(s) added to backend/routes.json")
    print("    Restart the backend, or POST /api/reload_routes to pick them up.")


if __name__ == "__main__":
    sys.exit(main())
