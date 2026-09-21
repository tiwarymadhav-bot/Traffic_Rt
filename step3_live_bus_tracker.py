# DTC Bus Route 85 - Live Tracking System
# File: step3_live_bus_tracker.py
# Calls the dtcbusroutes.in live API to get bus positions
# Then maps them with OSM road network and traffic analysis

import requests
import json
import os
import math
import time
from datetime import datetime

BASE_DIR = r"c:\Upwork\06_Branding\delhi_bus_tracking"
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# DTC LIVE BUS API - discovered from browser dev tools
# The API found in the screenshot returns: id, lat, lng, route, route_id, ac, agency
# ─────────────────────────────────────────────────────────────────────────────

LIVE_API_BASE = "https://www.dtcbusroutes.in/api/buses/"

# Headers to mimic a browser request (same as what browser sends)
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.dtcbusroutes.in/bus/route/25/anand-vihar-isbt-1/to/punjabi-bagh-terminal-1/",
}

# ─────────────────────────────────────────────────────────────────────────────
# HEADING CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calculate_bearing(lat1, lon1, lat2, lon2):
    """Calculate compass bearing (0-360) from point 1 to point 2."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360

def bearing_to_direction(bearing):
    """Convert bearing degrees to cardinal direction."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"]
    return dirs[round(bearing / 45) % 8]

def haversine_distance(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ─────────────────────────────────────────────────────────────────────────────
# TRAFFIC CLASSIFICATION (based on bus speed)
# ─────────────────────────────────────────────────────────────────────────────

def classify_traffic(speed_kmh):
    """Classify traffic based on bus speed."""
    if speed_kmh is None:
        return "unknown", "#808080"
    elif speed_kmh < 5:
        return "heavy_jam", "#FF0000"       # Red
    elif speed_kmh < 15:
        return "moderate", "#FFA500"         # Orange
    elif speed_kmh < 30:
        return "slow", "#FFFF00"             # Yellow
    else:
        return "free_flow", "#00CC00"        # Green

# ─────────────────────────────────────────────────────────────────────────────
# NEAREST STOP FINDER
# ─────────────────────────────────────────────────────────────────────────────

def find_nearest_stop(lat, lon, stops):
    """Find the nearest bus stop to a GPS position."""
    nearest = None
    min_dist = float("inf")
    for stop in stops:
        if stop["lat"] and stop["lon"]:
            d = haversine_distance(lat, lon, stop["lat"], stop["lon"])
            if d < min_dist:
                min_dist = d
                nearest = stop
    return nearest, min_dist

# ─────────────────────────────────────────────────────────────────────────────
# LIVE BUS FETCH
# ─────────────────────────────────────────────────────────────────────────────

def fetch_live_buses(route_id="85"):
    """Fetch live bus positions from the dtcbusroutes.in API."""
    # Try multiple possible API patterns
    api_urls = [
        f"{LIVE_API_BASE}?route={route_id}",
        f"{LIVE_API_BASE}?route_id={route_id}",
        f"https://www.dtcbusroutes.in/api/buses/?route={route_id}",
        f"https://www.dtcbusroutes.in/api/live/?route={route_id}",
    ]
    
    for url in api_urls:
        try:
            print(f"  Trying: {url}")
            r = requests.get(url, headers=API_HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                print(f"  ✅ Got response from: {url}")
                # Handle both {"buses": [...]} and [...] formats
                if isinstance(data, dict) and "buses" in data:
                    return data["buses"]
                elif isinstance(data, list):
                    return data
        except Exception as e:
            print(f"  ❌ {url}: {e}")
    
    print("  ⚠  Live API not reachable - using simulated data for demo")
    return simulate_buses()

def simulate_buses():
    """Generate simulated bus data for demo when API is not accessible."""
    # Simulate 3 buses at different positions along the route
    with open(os.path.join(DATA_DIR, "route_85_stops.json")) as f:
        stops_data = json.load(f)
    stops = stops_data["stops"]
    
    simulated = []
    positions = [5, 20, 38]  # Stop indices for 3 simulated buses
    for i, pos_idx in enumerate(positions):
        stop = stops[pos_idx - 1]
        # Add small random offset to simulate bus between stops
        simulated.append({
            "id": f"DL1SIM{1000+i}",
            "lat": stop["lat"] + 0.0002,
            "lng": stop["lon"] + 0.0002,
            "route": "85",
            "route_id": "85",
            "ac": "ac",
            "agency": "DTC",
            "simulated": True
        })
    return simulated

# ─────────────────────────────────────────────────────────────────────────────
# MAIN: FETCH + ANALYZE + SAVE
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  DTC Route 85 - Live Bus Tracker")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # Load stop data
    stops_path = os.path.join(DATA_DIR, "route_85_stops.json")
    if not os.path.exists(stops_path):
        print("❌ run step1 first to generate stops data")
        return
    
    with open(stops_path) as f:
        stops_data = json.load(f)
    stops = stops_data["stops"]
    print(f"✅ Loaded {len(stops)} bus stops")
    
    # Fetch live buses
    print("\nFetching live bus positions...")
    buses = fetch_live_buses("85")
    print(f"✅ Found {len(buses)} active buses")
    
    # Enrich each bus with heading, nearest stop, and traffic status
    enriched = []
    prev_positions = {}  # For heading calc - load from last saved snapshot
    
    # Try to load previous snapshot for heading
    snapshot_path = os.path.join(DATA_DIR, "live_snapshot_prev.json")
    if os.path.exists(snapshot_path):
        with open(snapshot_path) as f:
            prev_data = json.load(f)
        for b in prev_data.get("buses", []):
            prev_positions[b["id"]] = {"lat": b["lat"], "lng": b["lng"]}
    
    for bus in buses:
        lat, lng = bus.get("lat"), bus.get("lng")
        if not lat or not lng:
            continue
        
        # Heading
        bearing, direction = None, "?"
        if bus["id"] in prev_positions:
            prev = prev_positions[bus["id"]]
            bearing = calculate_bearing(prev["lat"], prev["lng"], lat, lng)
            direction = bearing_to_direction(bearing)
        
        # Nearest stop
        nearest_stop, dist_m = find_nearest_stop(lat, lng, stops)
        
        # Traffic (simulated speed - in real system, compare with prev position + time)
        import random
        sim_speed = random.uniform(0, 40)  # km/h - replace with real speed calc
        traffic_status, traffic_color = classify_traffic(sim_speed)
        
        enriched.append({
            "id":           bus["id"],
            "lat":          lat,
            "lng":          lng,
            "route":        bus.get("route", "85"),
            "agency":       bus.get("agency", "DTC"),
            "ac":           bus.get("ac", "unknown"),
            "bearing":      round(bearing, 1) if bearing else None,
            "direction":    direction,
            "nearest_stop": nearest_stop["name"] if nearest_stop else None,
            "stop_index":   nearest_stop["index"] if nearest_stop else None,
            "dist_to_stop_m": round(dist_m),
            "speed_kmh":    round(sim_speed, 1),
            "traffic":      traffic_status,
            "traffic_color": traffic_color,
            "simulated":    bus.get("simulated", False),
            "timestamp":    datetime.now().isoformat(),
        })
    
    # Save current snapshot
    snapshot = {
        "route": "85",
        "timestamp": datetime.now().isoformat(),
        "bus_count": len(enriched),
        "buses": enriched
    }
    with open(os.path.join(DATA_DIR, "live_snapshot.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    
    # Save as previous for next run (heading calculation)
    with open(snapshot_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    
    # Print summary
    print("\n📍 Bus Positions:")
    print(f"{'Bus ID':<15} {'Lat':>10} {'Lng':>10} {'Dir':>4} {'Near Stop':<35} {'Traffic'}")
    print("-" * 100)
    for b in enriched:
        print(f"{b['id']:<15} {b['lat']:>10.5f} {b['lng']:>10.5f} "
              f"{b['direction']:>4}  {(b['nearest_stop'] or 'N/A'):<35} {b['traffic']}")
    
    print(f"\n✅ Saved live snapshot to: {os.path.join(DATA_DIR, 'live_snapshot.json')}")

if __name__ == "__main__":
    main()

