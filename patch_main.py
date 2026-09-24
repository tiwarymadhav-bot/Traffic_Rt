import re

with open("C:/Delhi_Bus_Traffic/backend/main.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add import and OFFROUTE_RECORDS
if "import collections" not in content:
    content = content.replace("import time", "import time\nimport collections")

if "OFFROUTE_RECORDS" not in content:
    old_state = "last_positions: Dict[str, dict] = {}    # bus_id -> tracking state"
    new_state = "last_positions: Dict[str, dict] = {}    # bus_id -> tracking state\nOFFROUTE_RECORDS = collections.deque(maxlen=4000)"
    content = content.replace(old_state, new_state)

# 2. Add recording logic
old_logic = """                    if status == "offroute":
                        state["chain"] = None
                    continue"""
new_logic = """                    if status == "offroute":
                        state["chain"] = None
                        OFFROUTE_RECORDS.append({
                            "ts": now,
                            "route": state.get("route", "?"),
                            "direction": state.get("direction", "?"),
                            "lat": lat,
                            "lon": lng,
                            "bid": bid,
                            "off_m": lr.get("off_m", 0.0)
                        })
                    continue"""
content = content.replace(old_logic, new_logic)

# 3. Add API endpoint
if "/api/debug/offroute" not in content:
    api_code = """
@app.get("/api/debug/offroute")
async def debug_offroute():
    now = time.time()
    while OFFROUTE_RECORDS and now - OFFROUTE_RECORDS[0]["ts"] > 21600:
        OFFROUTE_RECORDS.popleft()
    
    route_totals = collections.defaultdict(int)
    for r in OFFROUTE_RECORDS:
        key = f"{r['route']}|{r['direction']}"
        route_totals[key] += 1
        
    clusters = {}
    for r in OFFROUTE_RECORDS:
        key = f"{r['route']}|{r['direction']}"
        clat = round(r["lat"], 3)
        clon = round(r["lon"], 3)
        ckey = (key, clat, clon)
        if ckey not in clusters:
            clusters[ckey] = {
                "route_key": key,
                "lat": clat,
                "lon": clon,
                "buses": set(),
                "fixes": 0,
                "total_off_m": 0.0,
                "min_ts": r["ts"],
                "max_ts": r["ts"]
            }
        
        c = clusters[ckey]
        c["buses"].add(r["bid"])
        c["fixes"] += 1
        c["total_off_m"] += r["off_m"]
        c["min_ts"] = min(c["min_ts"], r["ts"])
        c["max_ts"] = max(c["max_ts"], r["ts"])

    out = []
    for c in clusters.values():
        total_route_fixes = route_totals[c["route_key"]]
        share = c["fixes"] / total_route_fixes if total_route_fixes > 0 else 0
        
        duration_min = (c["max_ts"] - c["min_ts"]) / 60.0
        avg_m = c["total_off_m"] / c["fixes"]
        
        eval_msg = "unknown"
        if len(c["buses"]) >= 5 and c["fixes"] >= 15:
            eval_msg = "line is probably wrong here"
        elif len(c["buses"]) <= 2:
            eval_msg = "looks like a diversion"
            
        out.append({
            "route_key": c["route_key"],
            "lat": c["lat"],
            "lon": c["lon"],
            "buses": len(c["buses"]),
            "fixes": c["fixes"],
            "avg_off_m": round(avg_m, 1),
            "duration_min": round(duration_min, 1),
            "share_of_route_offroute": round(share, 3),
            "evaluation": eval_msg,
            "summary": f"{c['route_key']}   {len(c['buses'])} buses,  {c['fixes']} fixes, avg {round(avg_m)} m over {round(duration_min, 1)} min  -> {eval_msg}"
        })
        
    out.sort(key=lambda x: x["fixes"], reverse=True)
    return {"total_recorded": len(OFFROUTE_RECORDS), "clusters": out}

@app.get("/api/debug/segments")"""
    content = content.replace("@app.get(\"/api/debug/segments\")", api_code)

with open("C:/Delhi_Bus_Traffic/backend/main.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Patch applied successfully")
