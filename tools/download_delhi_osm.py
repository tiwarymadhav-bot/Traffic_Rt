import osmnx as ox
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

print("Downloading major roads for Delhi...")
G = ox.graph_from_place("Delhi, India", network_type="drive", custom_filter='["highway"~"motorway|trunk|primary|secondary"]')
nodes, edges = ox.graph_to_gdfs(G)

cols = [c for c in ['geometry', 'name', 'highway', 'length', 'oneway'] if c in edges.columns]
out_path = os.path.join(DATA_DIR, "major_roads.geojson")
edges[cols].reset_index().to_file(out_path, driver="GeoJSON")
print(f"Saved {len(edges)} roads to {out_path}")
