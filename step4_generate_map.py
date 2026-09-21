"""
STEP 4 - Generate Interactive Map
Combines OSM road network + Route 85 bus stops + live bus positions
Outputs: route_85_map.html (open in any browser)
"""
import json, os, folium, math
from folium.plugins import MarkerCluster, AntPath

BASE_DIR = r"C:\Delhi_Bus_Traffic"
DATA_DIR = os.path.join(BASE_DIR, "data")

# ── Helper ─────────────────────────────────────────────────────────────────────
def bearing_arrow(bearing):
    """CSS transform for direction arrow based on bearing."""
    return f"rotate({bearing}deg)" if bearing else "rotate(0deg)"

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

# ── Load data ──────────────────────────────────────────────────────────────────
stops_data  = load_json(os.path.join(DATA_DIR, "route_85_stops.json"))
stops       = stops_data["stops"]

# Load live buses (or use empty if not available)
live_path = os.path.join(DATA_DIR, "live_snapshot.json")
live_buses = []
if os.path.exists(live_path):
    live_data  = load_json(live_path)
    live_buses = live_data.get("buses", [])

# ── Create Folium map centred on Delhi Route 85 ────────────────────────────────
center_lat = (stops[0]["lat"] + stops[-1]["lat"]) / 2
center_lon = (stops[0]["lon"] + stops[-1]["lon"]) / 2

m = folium.Map(
    location=[center_lat, center_lon],
    zoom_start=13,
    tiles="OpenStreetMap",
    control_scale=True,
)

# ── Route polyline (bus path along stops) ──────────────────────────────────────
route_coords = [(s["lat"], s["lon"]) for s in stops if s["lat"] and s["lon"]]

# Animated route line
AntPath(
    route_coords,
    color="#1565C0",
    weight=4,
    opacity=0.8,
    delay=800,
    tooltip="DTC Route 85: Anand Vihar ISBT → Punjabi Bagh Terminal",
).add_to(m)

# ── Bus Stop Markers ───────────────────────────────────────────────────────────
stop_group = folium.FeatureGroup(name="🚌 Bus Stops", show=True)

for stop in stops:
    if not stop["lat"] or not stop["lon"]:
        continue

    is_terminal = stop["index"] in (1, 52)
    color  = "red"   if is_terminal else "blue"
    icon   = "flag"  if is_terminal else "bus"
    size   = "sm"

    popup_html = f"""
    <div style='font-family:sans-serif;min-width:180px;'>
      <b>#{stop['index']} {stop['name']}</b><br>
      <span style='color:#555;font-size:11px;'>
        Route 85 | DTC<br>
        📍 {stop['lat']:.5f}, {stop['lon']:.5f}
      </span>
      <div id='eta-{stop['index']}' style='margin-top:4px;font-size:11px;color:#1565C0;'></div>
    </div>
    """

    folium.Marker(
        location=[stop["lat"], stop["lon"]],
        popup=folium.Popup(popup_html, max_width=220),
        tooltip=f"#{stop['index']} {stop['name']}",
        icon=folium.Icon(color=color, icon=icon, prefix="fa", icon_size=(20, 20)),
    ).add_to(stop_group)

stop_group.add_to(m)

# ── Live Bus Markers ───────────────────────────────────────────────────────────
bus_group = folium.FeatureGroup(name="🚍 Live Buses", show=True)
traffic_colors = {
    "heavy_jam":  "#FF0000",
    "moderate":   "#FF8C00",
    "slow":       "#FFD700",
    "free_flow":  "#00CC00",
    "unknown":    "#808080",
}

for bus in live_buses:
    lat, lng = bus["lat"], bus["lng"]
    bearing  = bus.get("bearing")
    traffic  = bus.get("traffic", "unknown")
    color    = traffic_colors.get(traffic, "#808080")
    speed    = bus.get("speed_kmh", "?")
    direction = bus.get("direction", "?")
    sim_label = " (demo)" if bus.get("simulated") else ""
    
    popup_html = f"""
    <div style='font-family:sans-serif;min-width:200px;padding:4px;'>
      <h4 style='margin:0 0 6px;color:#1565C0;'>🚌 {bus['id']}{sim_label}</h4>
      <table style='font-size:12px;border-collapse:collapse;width:100%'>
        <tr><td><b>Route</b></td><td>{bus.get('route','85')}</td></tr>
        <tr><td><b>Agency</b></td><td>{bus.get('agency','DTC')}</td></tr>
        <tr><td><b>AC</b></td><td>{"Yes" if bus.get("ac")=="ac" else "No"}</td></tr>
        <tr><td><b>Speed</b></td><td>{speed} km/h</td></tr>
        <tr><td><b>Heading</b></td><td>{direction} ({bearing}°)</td></tr>
        <tr><td><b>Near Stop</b></td><td>{bus.get('nearest_stop','N/A')}</td></tr>
        <tr><td><b>Dist to stop</b></td><td>{bus.get('dist_to_stop_m','?')} m</td></tr>
        <tr><td><b>Traffic</b></td>
            <td><span style='background:{color};color:#fff;padding:1px 6px;border-radius:3px;'>{traffic.replace('_',' ').title()}</span></td>
        </tr>
      </table>
      <p style='font-size:10px;color:#888;margin:4px 0 0;'>{bus.get('timestamp','')}</p>
    </div>
    """
    
    # Bus icon with direction arrow
    bus_icon_html = f"""
    <div style='
        background:{color};
        border:2px solid white;
        border-radius:50%;
        width:28px;height:28px;
        display:flex;align-items:center;justify-content:center;
        box-shadow:0 2px 4px rgba(0,0,0,0.4);
        font-size:14px;
        transform:{bearing_arrow(bearing)};
    '>🚌</div>
    """
    
    folium.Marker(
        location=[lat, lng],
        popup=folium.Popup(popup_html, max_width=250),
        tooltip=f"Bus {bus['id']} | {direction} | {speed} km/h | {traffic.replace('_',' ').title()}",
        icon=folium.DivIcon(html=bus_icon_html, icon_size=(30, 30), icon_anchor=(15, 15)),
    ).add_to(bus_group)

bus_group.add_to(m)

# ── Traffic Legend ─────────────────────────────────────────────────────────────
legend_html = """
<div style='
    position:fixed; bottom:30px; right:10px; z-index:9999;
    background:white; padding:12px 16px; border-radius:10px;
    box-shadow:0 2px 8px rgba(0,0,0,0.25); font-family:sans-serif;
    font-size:12px; min-width:160px;
'>
  <b style='font-size:13px;'>🚦 Traffic Legend</b><br><br>
  <span style='color:#FF0000;'>●</span> Heavy Jam (&lt;5 km/h)<br>
  <span style='color:#FF8C00;'>●</span> Moderate (5–15 km/h)<br>
  <span style='color:#FFD700;'>●</span> Slow (15–30 km/h)<br>
  <span style='color:#00CC00;'>●</span> Free Flow (&gt;30 km/h)<br><br>
  <b>Route 85</b><br>
  <span style='color:#555;font-size:11px;'>Anand Vihar ISBT →<br>Punjabi Bagh Terminal<br>52 stops | DTC</span>
</div>
"""
m.get_root().html.add_child(folium.Element(legend_html))

# ── Layer control ──────────────────────────────────────────────────────────────
folium.LayerControl(position="topright", collapsed=False).add_to(m)

# ── Save map ───────────────────────────────────────────────────────────────────
out_path = os.path.join(BASE_DIR, "route_85_map.html")
m.save(out_path)
print(f"✅ Interactive map saved to: {out_path}")
print("   Open this HTML file in your browser to view the map!")
print(f"   Bus stops plotted   : {len(route_coords)}")
print(f"   Live buses on map   : {len(live_buses)}")

