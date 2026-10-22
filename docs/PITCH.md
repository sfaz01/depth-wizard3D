# Pitch and demo script

## One line
**One satellite image in, a measurable 3D twin out. Raise the river, see who floods, and send people to safety in under a minute, with free data and free AI on a laptop.**

## The problem (30 s)
When a river is rising, a district control room needs three answers: *which streets go under, how many people are in those buildings, and where they should go.* High-resolution 3D city models (LiDAR, stereo) exist for a few rich cities and take months to commission. Most flood-prone towns have none. What every town does have is satellite imagery.

## What we built
- **3D from one image.** A monocular depth model reconstructs buildings, which sit on real free terrain (SRTM) and are scaled against OpenStreetMap building heights. We report our own error on buildings held out of the fit.
- **Decisions, not just visuals.**
  - A physically connected flood simulation from the river, storm surge, a cloudburst or an embankment breach.
  - Water depth at every door, people on flooded floors, and ranked evacuation points.
  - Landslide-slope classes, and shadow-aware rooftop solar for the planning side.
- **Anyone can use it.** Ask in plain language. A free local LLM (Ollama) picks the actions; the numbers come from the analytics, so it can't make up a casualty count.
- **It leaves the laptop.** A printable impact brief for the control room, AR on a phone, a 3D-printable model for the town hall, and GeoJSON for the GIS team.

## Live demo (3 minutes)

Pre-bake the scenes the night before (`python dw.py presets`) so the demo never waits on Wi-Fi.

| Time | Do | Say |
|---|---|---|
| 0:00 | App is open on **Chennai, Adyar** (Recent scenes) | "This is the Adyar river in Chennai, which flooded in 2015. Everything you see came from one satellite image." |
| 0:15 | Orbit the city; open **Scene** and point at the scale card | "Heights are calibrated against OpenStreetMap, and here's our own error on buildings we held out. We don't hide it." |
| 0:35 | **Flood** tab, then drag the gauge to +3 m | "The water only goes where it can flow from the river. Notice the brown floodwater against the blue river." |
| 0:55 | Read the impact panel aloud | "36 buildings reached, about 660 people on flooded floors, and the worst houses have 3 m at the door." |
| 1:10 | **Find evacuation points** | "Dry assembly areas and tall rooftops, ranked by how far above the water they are." |
| 1:30 | Copilot: *"storm surge of 2 m and where should people evacuate"* | "Plain language works too, and every number in that answer is computed, not generated." |
| 1:55 | **Breach point**, then click the bank | "Or an embankment fails right here." |
| 2:10 | Export, then **Open printable impact brief** | "This is the one-page brief that goes to the district collector." |
| 2:25 | Hand a judge a phone and scan the QR | "And this is the same town on your table." |
| 2:40 | **Sun** tab: *Play the day*, then **Estimate rooftop solar** | "On a dry day it's a planning tool: real shadows, and rooftop solar for every building." |
| 2:55 | Close | "One image, free data, a laptop. Any town, today." |

**Backup if the network or model fails:** the built-in synthetic *River Town* works fully offline with exact heights. Every feature above runs on it.

## Why it's credible (for technical judges)
- **Measured, not claimed:** synthetic ground truth gives a 0.99 m building-height error with 139/139 buildings found. On real data, Lower Manhattan scored ±33.5 m on held-out OSM buildings, and we show that on screen.
- **Physics where it matters:** a connected flood fill seeded from detected permanent water; SRTM noise over rivers removed by flattening the water surface.
- **Engineering:** 37 automated tests run in CI, including a test that the browser flood fill matches the server cell for cell. Hardened uploads. One command to run; Docker image ready.

## Questions to expect

**"Isn't monocular depth inaccurate?"**
Yes, for absolute heights. That's why it is calibrated against OSM and the error is shown. For floods the critical variable is ground elevation (from the DEM) and connectivity; building height mostly drives occupancy estimates, where ±50% still separates a 2-storey house from a 15-storey tower.

**"Why not use a real hydrodynamic model?"**
Speed and availability. This is a minutes-to-answer triage tool. The DSM, footprints and water mask it produces are exactly the inputs a 2D shallow-water solver needs; that is the next step (see LIMITATIONS).

**"What does it cost to run?"**
Nothing. Esri imagery, AWS Terrain Tiles and OpenStreetMap are free, Depth Anything V2 Small is open (Apache-2.0), and the copilot runs on Ollama locally. It works on a CPU laptop; a 6 GB GPU makes it fast.

**"What's next?"**
NDVI or land-cover masks to separate trees from roofs; national DEMs (CartoDEM) instead of SRTM; rainfall-driven runoff; WorldPop occupancy; a field-team mobile mode.

## Slide outline (5 slides max)
1. The question a control room asks when the river rises. Use a photo of a real flood.
2. One image → 3D twin → decisions. Use the pipeline diagram from the README.
3. Live demo (no slide; switch to the app).
4. How we know it's right: the synthetic truth table, the OSM hold-out number, and the limitations listed openly.
5. Free stack, runs anywhere, next steps, team.
