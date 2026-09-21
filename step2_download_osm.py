import osmnx as ox
import os

DATA_DIR = r'c:\Upwork\06_Branding\delhi_bus_tracking\data'
os.makedirs(DATA_DIR, exist_ok=True)

# Download only the corridor around Route 85
# Using a smaller, more targeted bounding box
# This covers: Anand Vihar (E) to Punjabi Bagh (W) corridor
print('Downloading OSM road network - Route 85 corridor...')

# Use graph_from_address for a smaller area first - then expand
# Approach: download by individual district zones
zones = [
    ('Anand Vihar Delhi', 2000),    # East end
    ('Laxmi Nagar Delhi', 2000),    # Middle east
    ('ITO Delhi', 2000),            # Centre
    ('Karol Bagh Delhi', 2000),     # Centre west
    ('Punjabi Bagh Delhi', 2000),   # West end
]

graphs = []
for place, dist in zones:
    try:
        print(f'  Downloading: {place} ({dist}m radius)...')
        G = ox.graph_from_address(place, dist=dist, network_type='drive')
        graphs.append(G)
        print(f'    Got {len(G.nodes)} nodes, {len(G.edges)} edges')
    except Exception as e:
        print(f'    Failed: {e}')

if not graphs:
    print('No graphs downloaded!')
else:
    import networkx as nx
    # Combine all zone graphs
    G_combined = graphs[0]
    for g in graphs[1:]:
        G_combined = nx.compose(G_combined, g)

    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G_combined)
    print(f'\nCombined graph: {len(nodes_gdf)} nodes, {len(edges_gdf)} edges')

    # Save GeoPackage
    gpkg_path = os.path.join(DATA_DIR, 'osm_road_network.gpkg')
    nodes_gdf.to_file(gpkg_path, layer='nodes', driver='GPKG')
    edges_gdf.to_file(gpkg_path, layer='edges', driver='GPKG')
    print(f'Saved: {gpkg_path}')

    # Save GeoJSON edges
    cols = [c for c in ['geometry','name','highway','length','oneway'] if c in edges_gdf.columns]
    edges_gdf[cols].reset_index().to_file(os.path.join(DATA_DIR, 'osm_edges.geojson'), driver='GeoJSON')
    print('Saved: osm_edges.geojson')

    # Save GraphML
    ox.save_graphml(G_combined, os.path.join(DATA_DIR, 'osm_road_graph.graphml'))
    print('Saved: osm_road_graph.graphml')
    print('OSM road data download complete!')

