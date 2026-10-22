# Results

Everything here was measured on this codebase, either by `python -m pytest tests` or by building the named scenes with `dw.py` on a CPU-only machine (Depth Anything V2 Small).

## 1. Analytics against known ground truth

The procedural "River Town" (`src/synthetic.py`) has exact terrain, building footprints and heights: 256 × 256 cells at 2 m, 139 buildings from 6 m to 30 m, and a river valley rising 8 → 60 m.

| Check | Result |
|---|---|
| Buildings detected | **139 / 139** matched to their true footprint, with no merges |
| Building height error | **0.99 m** mean absolute (bias +0.99 m; the p90 roof height is measured against the local ground) |
| Bare-earth ground error | **0.05 m** mean absolute (two-pass opening, then normalised-convolution refinement) |
| Flood connectivity | water at 9 m fills > 95% of the river channel and < 5% of the banks; a basin behind a ridge stays dry until its own source is chosen |
| Permanent-water detection | river IoU > 0.6 from RGB + depth + DEM alone |
| Sun position | matches the NOAA reference to < 1°; JS and Python agree to 1e-6° |
| Browser vs server flood fill | identical cell counts for 12 level/source combinations, with and without a water mask |

## 2. Height scale against independent ground truth (OpenStreetMap)

Scale is fitted on half the OSM buildings that carry `height` or `building:levels` tags. Error is reported on the **other half**, which never touched the fit.

| Scene | Tagged buildings used | Held out | Mean abs error | Median abs error | Pearson r |
|---|---|---|---|---|---|
| Lower Manhattan, 0.9 m/px | 107 | 53 | **33.5 m** | 50% | 0.49 |

Manhattan is a worst case: 20–300 m towers, deep shadows, and visible building lean. A power-law fit (h = a·relᵇ) improved the held-out MAE only to 28.9 m, so the simpler linear fit is kept. **Takeaway:** monocular depth orders buildings well enough for triage (which blocks are tall, which roofs are low), but individual absolute heights carry roughly ±50% error. The app surfaces this number on every calibrated scene.

## 3. Real scenes

| Scene | Size | Water found | Buildings | Notes |
|---|---|---|---|---|
| Chennai, Adyar river | 1.2 km | 26% | 114 | River flattened to one surface; +3 m reaches 36 buildings (≈660 people) along the banks |
| Guwahati, Brahmaputra | 1.2 km | 53% | 73 | Silty, bright river detected via the "glassy" rule; islands kept dry |
| Lower Manhattan | 0.8 km | 0% (correct) | 146 (OSM footprints) | Shaded streets and City Hall Park correctly rejected as water |

The first version of the water detector found the Adyar but read Manhattan's shaded streets and park as water and missed the bright, silty Brahmaputra. Each failure became a rule in `detect_water` (`src/scene.py`): a DEM-position test, a shoreline test, and a "glassy" texture rule. Hole filling was also limited so river islands stay dry. Only the synthetic case is in the automated tests, because real imagery isn't committed to the repo.

## 4. Speed (CPU laptop, Depth Anything V2 Small)

| Step | Time |
|---|---|
| Imagery + terrain tiles, 1.2 km | about 2 s (cached afterwards) |
| Depth inference, 1024 px | about 8–20 s |
| OSM footprints | runs in parallel with inference; capped at 60 s, falls back to the assumed scale |
| Asset build + analytics | under 1 s |
| Whole map scene | 47–60 s typical, 124 s worst seen (slow OSM mirror) |
| Flood report API (512² grid) | 23 ms |
| Evacuation points API | 30 ms |
| Solar estimate, ~80 sun positions with ray-marched shadows | 0.9 s |
| Browser flood fill per slider move | 3.7 ms |

## 5. Earlier research results (CLI pipeline, `run_day1.py` / `run_day2.py`)

These come from the original prototype and still hold for the GeoTIFF RANSAC path.

- **RANSAC vs OLS calibration** on a synthetic DEM with 9.3% building outliers: recovered slope error 0.02% (RANSAC) vs 8.12% (OLS); terrain RMSE 2.01 m vs 4.87 m.
- **Robust normalisation:** 2nd/98th-percentile clipping keeps 100% of the terrain's dynamic range against a +2000 glint outlier, where min/max scaling keeps 1.78%.
- **16-bit heightmaps:** 7.6e-6 maximum round-trip error vs 2.0e-3 for 8-bit, so no terracing in the mesh.
