# Architecture

## Components

```
browser ─────────────────────────────────────────────────────────────────────────────
  index.html + js/  main.js (controller)  viewer.js (Three.js)  flood.js  sun.js  newscene.js
  ar.html (model-viewer)    brief.html (printable report)
        │  REST (JSON + raw float32 grids)
server ──┴────────────────────────────────────────────────────────────────────────────
  api/main.py        FastAPI: scene creation, job status, results/assets, analytics, exports
  src/jobs.py        single-worker queue (the GPU is one resource), status.json per job
  src/pipeline.py    runners: map area | upload | GeoTIFF | synthetic demo
  src/tiles.py       Esri imagery + AWS terrarium DEM mosaics (Web Mercator, disk cache)
  src/osm.py         Overpass footprints, height tags, hold-out calibration
  src/scene.py       depth → metric DSM, water detection, web assets, analysis.json
  src/analytics.py   ground, buildings, flood, evacuation, slope, sun, solar  (pure numpy/scipy)
  src/copilot.py     intent parsing (LLM or rules) → analytics → grounded reply + UI actions
  src/exporters.py   GLB (AR) and watertight STL (3D print) writers
```

## Scene pipeline (map area)

1. **Start the OSM fetch on a thread.** It runs in parallel with everything below, with a 60 s budget.
2. **Fetch imagery** one zoom level finer than needed, then Lanczos-downsample to ≤ 1024 px (sharper than fetching at the target zoom).
3. **Fetch terrain:** terrarium tiles decoded as `R·256 + G + B/256 − 32768` and resampled to the image grid.
4. **Depth:** Depth Anything V2, with 2/98-percentile normalisation.
5. **Persist the inputs** (`rgb.png`, `rel.npy`, `dem.npy`, `source.json`), so any later rescale rebuilds in under a second without the model.
6. **Calibrate** from the OSM future: rasterise the footprints, read the median relative structure height inside each eroded footprint, fit metres per unit on even-indexed buildings, and report error on the odd ones.
7. **Build assets** (`scene.build_assets`):
   - `split_structures`: a large-window grey opening of `rel` gives the low-frequency tilt and terrain; the remainder is structure, normalised by its 99.5th percentile.
   - `detect_water` → mask.
   - `DSM = median-filtered DEM + structure × height_ref_m`, with water flattened to its 20th-percentile surface.
   - `mesh_export.export_for_web`: 512-px float32 heightmaps (raw + "sharp cuboid" variant) and texture.
   - Buildings from OSM footprints when there are ≥ 15, else watershed segmentation (`analytics.extract_buildings`).
   - `ground.bin`, `labels.bin`, `water.bin`, `buildings.json`, `analysis.json`, `manifest.json` (version 2).

Uploads skip the tiles. Without a DEM the terrain comes from the low-frequency depth component, stretched to a user-editable relief. GeoTIFFs get their bounds reprojected to lon/lat, so they receive a DEM and OSM like map scenes.

## Key design decisions

| Decision | Alternative | Why |
|---|---|---|
| Separate tilt from structures with a morphological opening | Use the raw relative depth as height | Monocular models invent a perspective ground plane on nadir imagery; on raw depth, the top of every scene floods first |
| Real DEM for terrain, model only for structures | Fit everything to the DEM with RANSAC (the original v1) | SRTM can't resolve buildings, and the model can't resolve absolute terrain; each does what it is good at |
| OSM height tags as ground truth with a hold-out split | Validate against the SRTM used for fitting | v1's validation was circular; a hold-out gives an honest, independent number |
| Connected flood fill from a source | Threshold `DSM < level` | Thresholding floods basins behind levees and ridges; connectivity is the first-order physics of inundation |
| Permanent water as the default source | Lowest DEM pixels | SRTM pits over water would otherwise seed floods in random hollows |
| LLM picks actions, Python computes numbers | LLM answers directly | Small free models are good at intent and bad at arithmetic; nothing in a reply can be invented |
| Single worker thread | Process pool | One GPU; jobs are minutes apart in a demo; status survives restarts on disk |
| Duplicate only the flood fill in JS | Server round-trip per slider tick | 60 fps water; a parity test keeps the two implementations identical |
| No frontend build step | React + bundler | Clone, run and present; ES modules from a CDN |

## Data contracts

All grids are row-major, north row first, little-endian, at the web resolution in `manifest.json` (`width`, `height`, `gsd_m`):

| File | Type | Meaning |
|---|---|---|
| `heightmap.bin` | float32 | DSM in metres |
| `heightmap_sharp.bin` | float32 | DSM with sharpened walls (display only) |
| `ground.bin` | float32 | bare-earth estimate in metres |
| `labels.bin` | int32 | building id (0 = none), ids match `buildings.json` |
| `water.bin` | uint8 | permanent water mask |
