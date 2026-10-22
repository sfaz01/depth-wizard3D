"""
tiles.py - Fetch satellite imagery and bare-earth elevation for any spot on Earth.

Both sources are free and need no API key:
    Imagery   : Esri World Imagery (XYZ tiles, attribution required in the UI).
    Elevation : AWS Terrain Tiles "terrarium" encoding (SRTM / NED / ETOPO composite),
                elevation_m = R * 256 + G + B / 256 - 32768.

All maths is Web Mercator (EPSG:3857), the projection both tile pyramids use.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import io
import math
import os
from pathlib import Path
from typing import Callable, Optional, Tuple

import numpy as np
from PIL import Image

TILE = 256
EARTH_CIRCUMFERENCE_M = 40075016.686
IMAGERY_URL = os.environ.get(
    "DW_IMAGERY_URL",
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
)
TERRAIN_URL = os.environ.get(
    "DW_TERRAIN_URL",
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
)
USER_AGENT = "DepthWizard/2.0 (+https://github.com/sfaz01/depth-wizard3D)"
MAX_IMAGERY_ZOOM = 19
MAX_TERRAIN_ZOOM = 15


@dataclass
class BBox:
    west: float
    south: float
    east: float
    north: float

    def validate(self, max_span_m: float = 4000.0) -> None:
        if not (-180 <= self.west < self.east <= 180 and -85 < self.south < self.north < 85):
            raise ValueError("Bounding box must be west<east, south<north, inside +-85 latitude.")
        w, h = self.size_m()
        if max(w, h) > max_span_m:
            raise ValueError(f"Area too large ({w:.0f} x {h:.0f} m). Max span is {max_span_m:.0f} m.")
        if min(w, h) < 50:
            raise ValueError("Area too small (min 50 m).")

    @property
    def center(self) -> Tuple[float, float]:
        return (self.south + self.north) / 2.0, (self.west + self.east) / 2.0

    def size_m(self) -> Tuple[float, float]:
        lat, _ = self.center
        k = math.cos(math.radians(lat)) * EARTH_CIRCUMFERENCE_M / 360.0
        return (self.east - self.west) * k, (self.north - self.south) * EARTH_CIRCUMFERENCE_M / 360.0


def lonlat_to_global_px(lon: float, lat: float, z: int) -> Tuple[float, float]:
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def gsd_at(lat: float, z: int) -> float:
    """Metres per pixel of a Web Mercator tile pyramid at this latitude and zoom."""
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat)) / (TILE * 2 ** z)


def choose_zoom(bbox: BBox, target_px: int = 1024, max_zoom: int = MAX_IMAGERY_ZOOM, round_up: bool = False) -> int:
    lat, _ = bbox.center
    width_m = max(bbox.size_m())
    rnd = math.ceil if round_up else math.floor
    z = int(rnd(math.log2(EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat)) * target_px / (TILE * width_m))))
    return int(np.clip(z, 1, max_zoom))


class TileFetcher:
    def __init__(self, cache_dir: Path | str = "data/cache/tiles", timeout: float = 15.0, workers: int = 8):
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.workers = workers

    def _get(self, url: str) -> bytes:
        key = hashlib.sha1(url.encode()).hexdigest()
        path = self.cache_dir / key[:2] / key
        if path.exists():
            return path.read_bytes()
        import requests

        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return r.content

    def mosaic(self, template: str, bbox: BBox, z: int, mode: str) -> np.ndarray:
        """Stitch the tiles covering bbox at zoom z and crop to the exact pixel extent."""
        x0, y0 = lonlat_to_global_px(bbox.west, bbox.north, z)
        x1, y1 = lonlat_to_global_px(bbox.east, bbox.south, z)
        tx0, ty0 = int(x0 // TILE), int(y0 // TILE)
        tx1, ty1 = int((x1 - 1e-9) // TILE), int((y1 - 1e-9) // TILE)
        n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if n_tiles > 400:
            raise ValueError(f"Request needs {n_tiles} tiles; zoom out or pick a smaller area.")

        jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]

        def load(t):
            tx, ty = t
            data = self._get(template.format(z=z, x=tx, y=ty))
            return t, Image.open(io.BytesIO(data)).convert(mode)

        canvas = Image.new(mode, ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE))
        with ThreadPoolExecutor(self.workers) as pool:
            for (tx, ty), img in pool.map(load, jobs):
                canvas.paste(img, ((tx - tx0) * TILE, (ty - ty0) * TILE))

        left, top = x0 - tx0 * TILE, y0 - ty0 * TILE
        crop = canvas.crop((int(round(left)), int(round(top)),
                            int(round(left + (x1 - x0))), int(round(top + (y1 - y0)))))
        return np.asarray(crop)

    def imagery(self, bbox: BBox, target_px: int = 1024) -> Tuple[np.ndarray, int, float]:
        """Fetch one zoom level finer than needed, then downsample to <= target_px (sharper)."""
        z = choose_zoom(bbox, target_px, round_up=True)
        rgb = self.mosaic(IMAGERY_URL, bbox, z, "RGB")
        gsd = gsd_at(bbox.center[0], z)
        h, w = rgb.shape[:2]
        if max(h, w) > target_px:
            f = target_px / max(h, w)
            img = Image.fromarray(rgb).resize((max(1, round(w * f)), max(1, round(h * f))), Image.Resampling.LANCZOS)
            rgb, gsd = np.asarray(img), gsd * w / img.size[0]
        return rgb, z, gsd

    def elevation(self, bbox: BBox, out_shape: Tuple[int, int]) -> np.ndarray:
        """Bare-earth elevation (m) resampled to out_shape (H, W)."""
        z = min(MAX_TERRAIN_ZOOM, choose_zoom(bbox, target_px=256, max_zoom=MAX_TERRAIN_ZOOM))
        raw = self.mosaic(TERRAIN_URL, bbox, z, "RGB").astype(np.float32)
        dem = decode_terrarium(raw)
        img = Image.fromarray(dem).resize((out_shape[1], out_shape[0]), Image.Resampling.BICUBIC)
        return np.asarray(img, dtype=np.float32)


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, np.float32)
    return (rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0).astype(np.float32)


def fetch_scene(bbox: BBox, target_px: int = 1024, fetcher: Optional[TileFetcher] = None,
                progress: Optional[Callable[[str, float], None]] = None) -> dict:
    """Imagery + bare-earth DEM for a bounding box, ready for the scene pipeline."""
    bbox.validate()
    fetcher = fetcher or TileFetcher()
    if progress:
        progress("Downloading satellite imagery", 0.08)
    rgb, z, gsd = fetcher.imagery(bbox, target_px)
    if progress:
        progress("Downloading terrain elevation", 0.18)
    dem = fetcher.elevation(bbox, rgb.shape[:2])
    lat, lon = bbox.center
    return {"rgb": rgb, "dem": dem, "gsd_m": gsd, "zoom": z, "lat": lat, "lon": lon}
