"""
scene.py - Turn (image, relative depth, optional DEM) into a metric 3D twin + analytics.

Height model
------------
Monocular depth on a nadir image is good at *which* pixels are structures and how they
rank, but it also hallucinates a large-scale tilt (the model assumes a perspective
ground plane). So we split it:

    rel        = depth model output (0..1)
    ground_rel = large-window morphological opening of rel   (tilt + terrain, low freq)
    struct     = (rel - ground_rel) / p99.5                   (buildings, trees, ~0..1)

    DSM = base_terrain + struct * height_ref_m

base_terrain is a real bare-earth DEM when we know where the scene is (map picker or
GeoTIFF: AWS Terrain Tiles / SRTM). For a plain photo it is ground_rel stretched to a
user-editable `terrain_relief_m`. height_ref_m is fitted against OpenStreetMap building
heights when available (see osm.py), else a user-editable prior.

Job directory layout
--------------------
    source.json   what the scene is (kind, lat/lon, bbox, gsd)
    rgb.png       image at processing resolution
    rel.npy       relative depth at processing resolution
    dem.npy       bare-earth DEM (optional)
    scale.json    height_ref_m, terrain_relief_m, how they were chosen, OSM validation
    -- rebuilt by build_assets() (cheap, no model) --
    texture.png, heightmap*.bin/png, manifest.json   (mesh_export)
    ground.bin    bare-earth estimate at web resolution (float32)
    labels.bin    building id per web pixel (int32)
    buildings.json, analysis.json, thumb.jpg
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np
from PIL import Image
import scipy.ndimage as ndi

from src import analytics as A
from src.mesh_export import export_for_web

DEFAULT_HEIGHT_REF_M = 35.0
DEFAULT_TERRAIN_RELIEF_M = 8.0
WEB_MAX_DIM = 512
PROCESS_MAX_DIM = 1024

Progress = Optional[Callable[[str, float], None]]


def _report(progress: Progress, msg: str, frac: float) -> None:
    if progress:
        progress(msg, frac)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def split_structures(rel: np.ndarray, gsd_m: float, window_m: float = 90.0):
    """Return (ground_rel, struct) - see module docstring."""
    rel = np.asarray(rel, np.float32)
    win = A._window_px(rel, gsd_m, window_m)
    padded = np.pad(rel, win, mode="edge")
    ground_rel = ndi.grey_opening(padded, size=(win, win))
    ground_rel = ndi.gaussian_filter(ground_rel, max(1.0, win / 6.0))[win:-win, win:-win]
    ground_rel = np.minimum(ground_rel, rel)
    struct = np.clip(rel - ground_rel, 0.0, None)
    p = float(np.percentile(struct, 99.5))
    struct = np.clip(struct / max(p, 1e-6), 0.0, 1.2).astype(np.float32)
    return ground_rel.astype(np.float32), struct


def detect_water(rgb: np.ndarray, struct: np.ndarray, gsd_m: float, dem: Optional[np.ndarray] = None,
                 min_area_m2: float = 5000.0) -> np.ndarray:
    """
    Permanent water (rivers, lakes, sea) from an RGB image without a NIR band.

    Water is dark, smooth, flat in the depth map, not red-dominant, large, and - when a
    DEM is available - sits in the lower part of the terrain. The last cue rejects shaded
    parks and street canyons, which pass the colour tests.
    """
    f = np.asarray(rgb, np.float32) / 255.0
    lum = f.mean(-1)
    mean = ndi.uniform_filter(lum, 5)
    tex = np.sqrt(np.clip(ndi.uniform_filter(lum ** 2, 5) - mean ** 2, 0, None))
    relaxed = ((lum < np.percentile(lum, 35)) & (ndi.uniform_filter(struct, 9) < 0.06)
               & (f[..., 0] <= f[..., 1] + 0.04))
    cand = relaxed & (tex < 0.035)
    cand = ndi.binary_closing(ndi.binary_opening(cand, iterations=2), iterations=3)
    # Smooth-texture cores miss the strip along each bank (the texture window sees the
    # shore); grow back into adjacent dark, flat pixels.
    cand |= ndi.binary_dilation(cand, iterations=3, mask=relaxed)
    lab, n = ndi.label(cand)
    if n == 0:
        return cand
    idx = np.arange(1, n + 1)
    keep = np.bincount(lab.ravel(), minlength=n + 1)[1:] * gsd_m * gsd_m >= min_area_m2
    if dem is not None:
        low = np.percentile(dem, 40)
        keep &= np.asarray(ndi.median(dem, lab, idx)) <= low
    # Shadowed street canyons pass the colour tests too. Accept a body if it is wide
    # (half-width > 30 m) or if its shoreline is not lined with structures.
    half_width = np.asarray(ndi.maximum(ndi.distance_transform_edt(lab > 0) * gsd_m, lab, idx))
    shore = ndi.binary_dilation(lab > 0, iterations=max(2, int(6 / gsd_m))) & (lab == 0)
    shore_lab = np.where(shore, ndi.grey_dilation(lab, size=2 * max(2, int(6 / gsd_m)) + 1), 0)
    shore_p75 = np.asarray(ndi.labeled_comprehension(struct, shore_lab, idx, lambda v: np.percentile(v, 75), float, 0.0))
    keep &= (half_width > 30.0) | (shore_p75 < 0.055)
    ok = np.zeros(n + 1, bool)
    ok[1:] = keep
    return ndi.binary_fill_holes(ok[lab])


def compose_dsm(rel: np.ndarray, gsd_m: float, scale: Dict, dem: Optional[np.ndarray] = None,
                water: Optional[np.ndarray] = None) -> np.ndarray:
    ground_rel, struct = split_structures(rel, gsd_m)
    if dem is not None:
        # SRTM-class DEMs carry 30 m speckle and void pits; a median removes them.
        size = max(3, int(round(45.0 / gsd_m)) | 1)
        base = ndi.gaussian_filter(ndi.median_filter(np.asarray(dem, np.float32), size=size), 2.0)
    else:
        span = float(ground_rel.max() - ground_rel.min())
        base = (ground_rel - ground_rel.min()) / max(span, 1e-6) * float(scale["terrain_relief_m"])
    dsm = base + struct * float(scale["height_ref_m"])
    if water is not None and water.any():
        # Water surfaces are flat; DEMs are at their noisiest over them.
        dsm[water] = float(np.percentile(base[water], 20))
    return dsm.astype(np.float32)


def prepare_inputs(job_dir: Path, rgb: np.ndarray, rel: np.ndarray, source: Dict,
                   dem: Optional[np.ndarray] = None) -> None:
    """Persist processing-resolution inputs so assets can be rebuilt without the model."""
    job_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(rgb, np.uint8)).save(job_dir / "rgb.png")
    np.save(job_dir / "rel.npy", np.asarray(rel, np.float32))
    if dem is not None:
        np.save(job_dir / "dem.npy", np.asarray(dem, np.float32))
    write_json(job_dir / "source.json", source)


def default_scale(source: Dict) -> Dict:
    return {
        "height_ref_m": DEFAULT_HEIGHT_REF_M,
        "terrain_relief_m": DEFAULT_TERRAIN_RELIEF_M,
        "gsd_m": float(source.get("gsd_m") or 0.5),
        "method": "prior",
        "note": "Assumed scale - calibrate with a building of known height or let OSM fit it.",
    }


def save_osm(job_dir: Path, buildings) -> None:
    write_json(job_dir / "osm_buildings.json", [b.to_dict() for b in buildings])


def _osm(job_dir: Path):
    from src.osm import OsmBuilding

    return [OsmBuilding.from_dict(d) for d in read_json(job_dir / "osm_buildings.json", [])]


def calibrate_with_osm(job_dir: Path, progress: Progress = None) -> Optional[Dict]:
    """Fit height_ref_m against tagged OSM buildings (needs fetch_osm first)."""
    from src.osm import calibrate_scale, rasterize
    from src.tiles import BBox

    source = read_json(job_dir / "source.json", {})
    buildings = _osm(job_dir)
    if not source.get("bbox") or not buildings:
        return None
    _report(progress, "Calibrating heights against OpenStreetMap", 0.72)
    rel = np.load(job_dir / "rel.npy")
    _, struct = split_structures(rel, float(source["gsd_m"]))
    labels = rasterize(buildings, BBox(**source["bbox"]), struct.shape)
    return calibrate_scale(struct, labels, buildings)


MIN_OSM_FOOTPRINTS = 15


def _buildings(job_dir: Path, source: Dict, dsm: np.ndarray, gsd: float):
    """Footprints from OSM when the area is mapped, else our own segmentation."""
    osm = _osm(job_dir)
    if source.get("bbox") and len(osm) >= MIN_OSM_FOOTPRINTS:
        from src.osm import rasterize
        from src.tiles import BBox

        raw = rasterize(osm, BBox(**source["bbox"]), dsm.shape)
        g0 = A.estimate_ground(dsm, gsd)
        elevated = ((dsm - g0) > 2.5) | (raw > 0)
        ground = A.refine_ground(dsm, elevated, gsd)
        labels, buildings = A.buildings_from_labels(dsm, ground, gsd, raw, min_area_m2=20.0, min_height_m=1.0)
        # Map our ids back to OSM ids / reference heights via the label each footprint drew.
        for b in buildings:
            src_ids = raw[labels == b.id]
            if src_ids.size:
                ob = osm[int(np.bincount(src_ids).argmax()) - 1]
                b.osm_id, b.ref_height_m = ob.osm_id, ob.height_m
        return ground, labels, buildings, "osm_footprints"
    ground, labels, buildings = A.analyze_surface(dsm, gsd)
    return ground, labels, buildings, "segmentation"


def build_assets(job_dir: Path, progress: Progress = None) -> Dict:
    """(Re)build mesh assets + analytics from the stored inputs and scale.json."""
    job_dir = Path(job_dir)
    source = read_json(job_dir / "source.json", {})
    scale = read_json(job_dir / "scale.json") or default_scale(source)
    write_json(job_dir / "scale.json", scale)

    rgb = np.asarray(Image.open(job_dir / "rgb.png").convert("RGB"))
    rel = np.load(job_dir / "rel.npy")
    dem = np.load(job_dir / "dem.npy") if (job_dir / "dem.npy").exists() else None
    gsd = float(scale["gsd_m"])

    _report(progress, "Composing metric surface model", 0.8)
    if (job_dir / "water_mask.npy").exists():
        water = np.load(job_dir / "water_mask.npy").astype(bool)
    else:
        water = detect_water(rgb, split_structures(rel, gsd)[1], gsd, dem)
    if (job_dir / "dsm.npy").exists():  # synthetic / externally supplied metric DSM
        dsm = np.load(job_dir / "dsm.npy")
    else:
        dsm = compose_dsm(rel, gsd, scale, dem, water)

    _report(progress, "Exporting 3D mesh", 0.85)
    export = export_for_web(dsm, rgb, job_dir, max_dim=WEB_MAX_DIM, is_georeferenced=True,
                            crs_str=source.get("crs", "EPSG:3857" if source.get("bbox") else "Local metric"),
                            gsd_m=gsd)
    manifest = export["manifest"]
    w, h = manifest["width"], manifest["height"]
    web_gsd = float(manifest["gsd_m"])
    web_dsm = np.fromfile(job_dir / "heightmap.bin", dtype=np.float32).reshape(h, w)

    web_water = np.asarray(Image.fromarray(water.astype(np.uint8) * 255).resize((w, h), Image.Resampling.NEAREST)) > 127
    web_water.astype(np.uint8).tofile(job_dir / "water.bin")

    _report(progress, "Extracting buildings & hazards", 0.92)
    ground, labels, buildings, footprint_source = _buildings(job_dir, source, web_dsm, web_gsd)
    if web_water.any():
        labels[web_water] = 0  # nothing stands on open water
        buildings = [b for b in buildings if (labels == b.id).any()]
    ground.astype(np.float32).tofile(job_dir / "ground.bin")
    labels.astype(np.int32).tofile(job_dir / "labels.bin")
    write_json(job_dir / "buildings.json", [A.building_to_dict(b) for b in buildings])

    analysis = {
        "buildings": A.buildings_summary(buildings),
        "footprint_source": footprint_source,
        "terrain": A.landslide_susceptibility(ground, web_gsd),
        "ground_min_m": round(float(ground.min()), 2),
        "ground_max_m": round(float(ground.max()), 2),
        "suggested_flood_levels_m": _flood_levels(web_dsm, ground, web_water),
        "flood_datum_m": _flood_levels(web_dsm, ground, web_water)["min"],
        "water_fraction": round(float(web_water.mean()), 4),
        "water_level_m": round(float(np.median(web_dsm[web_water])), 2) if web_water.any() else None,
    }
    write_json(job_dir / "analysis.json", analysis)

    manifest.update({
        "version": 2,
        "metric": True,
        "units": "m",
        "scene": {k: source.get(k) for k in ("kind", "name", "lat", "lon", "bbox", "zoom", "attribution") if k in source},
        "scale": scale,
        "ground_bin_file": "ground.bin",
        "labels_bin_file": "labels.bin",
        "water_bin_file": "water.bin",
    })
    write_json(job_dir / "manifest.json", manifest)

    thumb = Image.fromarray(rgb).copy()
    thumb.thumbnail((320, 320))
    thumb.save(job_dir / "thumb.jpg", quality=85)
    _report(progress, "Done", 1.0)
    return manifest


def _flood_levels(dsm: np.ndarray, ground: np.ndarray, water: Optional[np.ndarray] = None) -> Dict[str, float]:
    # The gauge zero is the river surface when there is one, otherwise the lowest ground.
    lo = float(np.median(dsm[water])) if water is not None and water.any() else float(np.percentile(ground, 1))
    # Gauge tops out a few metres above the median ground: enough to drown half the scene,
    # fine-grained enough that a 0.5 m step is visible on the staff.
    hi = float(np.clip(np.percentile(ground, 50) + 4.0, lo + 6.0, lo + 20.0))
    return {"min": round(lo, 1), "max": round(hi, 1), "default": round(lo + 2.0, 1)}


def load_grids(job_dir: Path):
    """Web-resolution grids + buildings for analytics endpoints."""
    m = read_json(job_dir / "manifest.json")
    w, h = m["width"], m["height"]
    dsm = np.fromfile(job_dir / "heightmap.bin", dtype=np.float32).reshape(h, w)
    ground = np.fromfile(job_dir / "ground.bin", dtype=np.float32).reshape(h, w)
    labels = np.fromfile(job_dir / "labels.bin", dtype=np.int32).reshape(h, w)
    buildings = [A.building_from_dict(d) for d in read_json(job_dir / "buildings.json", [])]
    return m, dsm, ground, labels, buildings


def load_water(job_dir: Path) -> Optional[np.ndarray]:
    m = read_json(job_dir / "manifest.json")
    p = job_dir / "water.bin"
    if not p.exists():
        return None
    water = np.fromfile(p, dtype=np.uint8).reshape(m["height"], m["width"]).astype(bool)
    return water if water.any() else None
