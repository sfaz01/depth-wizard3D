"""
pipeline.py - End-to-end scene builders: map area, uploaded photo, GeoTIFF, offline demo.

Each `run_*` function has the signature runner(job_dir, progress) expected by JobManager.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
from pathlib import Path
import threading
import time
from typing import Callable, Dict, Optional

import numpy as np
from PIL import Image

from src import scene as S
from src.tiles import BBox, TileFetcher, fetch_scene

Progress = Callable[[str, float], None]

_estimator = None
_estimator_lock = threading.Lock()


def get_estimator():
    """Shared Depth Anything V2 instance. DW_MODEL_SIZE=small|base|large, DW_DEVICE=auto|cuda|cpu."""
    global _estimator
    with _estimator_lock:
        if _estimator is None:
            from src.depth_extract import DepthEstimator

            _estimator = DepthEstimator(
                model_size=os.environ.get("DW_MODEL_SIZE", "small"),
                device=os.environ.get("DW_DEVICE", "auto"),
            )
        return _estimator


def _limit_size(img: Image.Image, max_dim: int = S.PROCESS_MAX_DIM) -> Image.Image:
    if max(img.size) <= max_dim:
        return img
    img = img.copy()
    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    return img


def _depth(rgb: np.ndarray, progress: Progress) -> np.ndarray:
    progress("Loading depth model", 0.25)
    est = get_estimator()
    progress(f"Estimating depth (Depth Anything V2 {est.model_size}, {est.device})", 0.35)
    return est.predict(Image.fromarray(rgb), robust=True).normalized_depth


_osm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dw-osm")
OSM_BUDGET_S = float(os.environ.get("DW_OSM_BUDGET_S", "60"))


def _start_osm(bbox: BBox) -> Optional[Future]:
    """Fetch OSM footprints in the background while the depth model runs."""
    if os.environ.get("DW_OSM", "1") == "0":
        return None
    from src.osm import fetch_osm_buildings

    return _osm_pool.submit(fetch_osm_buildings, bbox, 20.0, "data/cache/osm", 1)


def _calibrate(job_dir: Path, source: Dict, progress: Progress, osm: Optional[Future] = None,
               started: float = 0.0) -> None:
    scale = S.default_scale(source)
    if source.get("bbox") and osm is not None:
        try:
            progress("Fetching OpenStreetMap building footprints", 0.68)
            buildings = osm.result(timeout=max(5.0, OSM_BUDGET_S - (time.time() - started)))
            S.save_osm(job_dir, buildings)
            fit = S.calibrate_with_osm(job_dir, progress)
            if fit:
                scale.update(height_ref_m=fit["height_ref_m"], method="osm",
                             note=f"Fitted to {fit['n_buildings']} OpenStreetMap buildings with height tags.",
                             osm=fit)
        except Exception as e:  # OSM mirrors are best-effort; keep the prior
            reason = "timed out" if isinstance(e, FutureTimeout) else e.__class__.__name__
            scale["note"] = f"OpenStreetMap unavailable ({reason}); using an assumed scale."
    S.write_json(job_dir / "scale.json", scale)


def run_map(bbox: BBox, name: str = "", fetcher: Optional[TileFetcher] = None):
    def runner(job_dir: Path, progress: Progress) -> None:
        started = time.time()
        osm = _start_osm(bbox)
        data = fetch_scene(bbox, fetcher=fetcher, progress=progress)
        rel = _depth(data["rgb"], progress)
        source = {
            "kind": "map",
            "name": name or f"{data['lat']:.4f}, {data['lon']:.4f}",
            "lat": data["lat"],
            "lon": data["lon"],
            "bbox": bbox.__dict__,
            "zoom": data["zoom"],
            "gsd_m": data["gsd_m"],
            "crs": "EPSG:3857",
            "attribution": "Imagery: Esri, Maxar, Earthstar Geographics. Terrain: AWS Terrain Tiles (SRTM/NED). Heights: OpenStreetMap contributors.",
        }
        S.prepare_inputs(job_dir, data["rgb"], rel, source, dem=data["dem"])
        _calibrate(job_dir, source, progress, osm, started)
        S.build_assets(job_dir, progress)

    return runner


def run_upload(image_path: Path, name: str = "", gsd_m: Optional[float] = None, lat: Optional[float] = None,
               lon: Optional[float] = None):
    def runner(job_dir: Path, progress: Progress) -> None:
        if image_path.suffix.lower() in (".tif", ".tiff"):
            from src.georef import is_georeferenced

            if is_georeferenced(image_path):
                return _run_geotiff(image_path, name, job_dir, progress)
        progress("Reading image", 0.1)
        full = Image.open(image_path).convert("RGB")
        img = _limit_size(full)
        rgb = np.asarray(img)
        rel = _depth(rgb, progress)
        # gsd_m describes the pixels of the file the user gave us, not our downsampled copy.
        gsd = (gsd_m or 0.5) * full.size[0] / img.size[0]
        source = {"kind": "upload", "name": name or image_path.stem, "gsd_m": gsd}
        if lat is not None and lon is not None:
            source.update(lat=lat, lon=lon)
        S.prepare_inputs(job_dir, rgb, rel, source)
        _calibrate(job_dir, source, progress)
        S.build_assets(job_dir, progress)

    return runner


def _run_geotiff(path: Path, name: str, job_dir: Path, progress: Progress) -> None:
    from rasterio.warp import transform_bounds

    from src.georef import read_geotiff, to_metric_resolution

    progress("Reading GeoTIFF", 0.1)
    data, crs, transform, bounds, _ = read_geotiff(path)
    bands = data[:3] if data.shape[0] >= 3 else np.repeat(data[:1], 3, axis=0)
    rgb = bands.transpose(1, 2, 0).astype(np.float32)
    if data.dtype != np.uint8:
        p1, p99 = np.percentile(rgb, [1.0, 99.0])
        rgb = np.clip((rgb - p1) / max(1e-5, p99 - p1) * 255.0, 0, 255)
    img = Image.fromarray(rgb.astype(np.uint8))
    orig_w = img.size[0]
    img = _limit_size(img)
    rgb = np.asarray(img)
    gsd = to_metric_resolution(transform, crs) * orig_w / img.size[0]

    w, s, e, n = transform_bounds(crs, "EPSG:4326", *bounds)
    bbox = BBox(w, s, e, n)
    started = time.time()
    osm = _start_osm(bbox)
    rel = _depth(rgb, progress)
    progress("Downloading terrain elevation", 0.6)
    dem = None
    try:
        dem = TileFetcher().elevation(bbox, rgb.shape[:2])  # north-up approximation, fine below ~5 km
    except Exception as ex:
        print(f"[pipeline] terrain fetch failed, continuing without DEM: {ex}")
    lat, lon = (s + n) / 2, (w + e) / 2
    source = {"kind": "geotiff", "name": name or path.stem, "lat": lat, "lon": lon, "bbox": bbox.__dict__,
              "gsd_m": gsd, "crs": crs.to_string()}
    S.prepare_inputs(job_dir, rgb, rel, source, dem=dem)
    _calibrate(job_dir, source, progress, osm, started)
    S.build_assets(job_dir, progress)


def run_demo():
    """Procedural river town - needs neither network nor the depth model."""
    def runner(job_dir: Path, progress: Progress) -> None:
        from src.synthetic import make_river_town, relative_from_dsm

        progress("Generating procedural river town", 0.3)
        t = make_river_town(256, gsd_m=2.0)
        source = {"kind": "demo", "name": "Demo: River Town (synthetic)", "gsd_m": 2.0,
                  "lat": 26.18, "lon": 91.74}
        rel = relative_from_dsm(t["dsm"])
        S.prepare_inputs(job_dir, t["rgb"], rel, source)
        np.save(job_dir / "dsm.npy", t["dsm"])  # exact truth, bypasses compose_dsm
        np.save(job_dir / "water_mask.npy", t["river"])
        scale = S.default_scale(source)
        scale.update(method="ground_truth", note="Synthetic scene with exact heights.")
        S.write_json(job_dir / "scale.json", scale)
        S.build_assets(job_dir, progress)

    return runner
