# torque-town-map-data

Map data for the game Torque Town, derived from OpenStreetMap.
© OpenStreetMap contributors, available under the Open Database License (ODbL) 1.0.

This repository is the offer of the Derivative Database required by ODbL §4.6:
the complete map database the game ships to players, free of charge, in machine-readable form,
together with the method used to produce it.

## What is inside

| Path | What | License |
|---|---|---|
| `data/OsmBuildings.luau`, `data/OsmRoads.luau`, `data/OsmAreas.luau` | The database exactly as used in the game (Luau tables) | ODbL 1.0 |
| `data/json/*.json` | The same three tables as JSON | ODbL 1.0 |
| `input/overrides.csv` | Our corrections to building attributes (levels, roof, wall/accent colours, facade type, entrances, balconies, ground floor, pattern) keyed by OSM way id | ODbL 1.0 |
| `tools/osm_import.py` | The import script (Python 3, standard library only) | MIT |
| `tools/export_json.luau` | Luau → JSON conversion (Lune) | MIT |
| `tools/stylua.toml` | Formatter settings used for the .luau output | MIT |

Coordinates are in studs (2.5 studs per metre) on a local plane: x east, y north, origin at
57.180 N, 65.641 E (equirectangular projection, see `project()` in `tools/osm_import.py`).
Object ids are our own sequential ids, not OSM ids.

## How it was made

1. OSM extract: `https://api.openstreetmap.org/api/0.6/map?bbox=65.623,57.170,65.662,57.190`,
   downloaded on 2026-09-24, saved as `input/raw.osm` (not included here — it is plain OSM data,
   download it yourself; the current OSM state will differ from that snapshot).
2. `tools/osm_import.py`:
   - projects lat/lon to the local plane and clips to the play area;
   - drops detached private houses;
   - **ignores all name, address and brand tags** — none of them reach the output;
   - simplifies outlines (Ramer–Douglas–Peucker, 0.5 m), infers missing building levels
     (tag → same-series neighbour → area → defaults), classifies roads, sidewalks and areas;
3. Output is formatted with StyLua using `tools/stylua.toml`.

Run (from the repository root):

```
python tools/osm_import.py            # writes data/*.luau (needs input/raw.osm; stylua on PATH is optional)
python tools/osm_import.py --check    # rebuild in memory and compare with data/
lune run tools/export_json            # writes data/json/*.json
```

With the snapshot from step 1 the script reproduces the files in `data/` byte for byte.

## Attribution when you reuse the data

"© OpenStreetMap contributors" and the ODbL must stay with the data. See `LICENSE`.
