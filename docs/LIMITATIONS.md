# Limitations

DepthWizard is a rapid situational-awareness tool. These are the places it is wrong, why, and what would fix each.

| Limitation | Why | Mitigation today | Proper fix |
|---|---|---|---|
| **Absolute building heights carry about ±50% error** (worse on skyscrapers) | A monocular model is trained on ground-level photos, not nadir imagery, and has no metric scale | OSM-tag calibration with a reported hold-out error; one-click "match this height" calibration; numbers labelled "assumed" when uncalibrated | Fine-tune on nadir imagery with LiDAR truth (e.g. ISPRS Potsdam, DFC2019); use stereo pairs where available |
| **Trees are sometimes counted as buildings** | Canopy is elevated and has soft edges, like blurred roofs; satellite shadows are green-tinted, so a simple greenness test fails | OSM footprints replace our segmentation wherever the area is mapped | A land-cover segmentation head (e.g. SegFormer on LoveDA), or NIR/NDVI when the imagery has it |
| **Elevated roads and bridges read as buildings** | Same height signature as a long, thin building | Visible in the 3D view; they rarely change flood counts | Filter by shape (aspect ratio) or OSM `bridge=yes` |
| **Flood model is static ("bathtub with connectivity")** | No flow velocity, rainfall timing, drainage or infiltration | Connectivity from a chosen source, which beats plain thresholding; four source types | Couple to a 2D shallow-water solver (e.g. LISFLOOD-FP style) on the same DSM |
| **Terrain under the city comes from SRTM (30 m)** | It's the best free global DEM; its vertical error is several metres | Median filtering; rivers flattened; the gauge zero is the actual water surface | Use national DEMs where available (e.g. CartoDEM in India, 3DEP in the US) |
| **Water detection uses RGB heuristics** | Web imagery has no infrared band | Five cues plus three real-scene regression rules; tested | OSM water polygons, or NDWI from Sentinel-2 |
| **Occupancy is a planning estimate** | 40 m² of floor area per person and 3.2 m floors, applied uniformly | Assumptions printed next to every number | Census / WorldPop rasters |
| **Solar is a clear-sky model with one cloud derate** | No local weather data | Shadows are real (ray-marched through the DSM); assumptions shown | NASA POWER or PVGIS irradiance for the location |
| **Off-nadir imagery** | Building lean breaks the disparity-to-height assumption | Esri imagery is mostly near-nadir orthophotos | RPC-based rectification for raw satellite scenes |
| **Scenes up to about 4 km across** | Browser mesh is 512 × 512; the tile budget is capped | Pick a smaller area for more detail | Tiled level-of-detail streaming |
| **Android WebXR AR needs HTTPS** | Browser security rules | iOS Quick Look and the 3D viewer work over LAN HTTP; use a free tunnel for Android AR | Serve over HTTPS by default |
| **The server is open on the LAN while running** | It binds to `0.0.0.0` so phones can reach it | Upload size limit, filename sanitising, id/asset whitelists | Set `DW_HOST=127.0.0.1` when phone access isn't needed; add auth before any public deployment |
