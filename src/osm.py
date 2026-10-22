"""
osm.py - Metric height calibration against OpenStreetMap building tags.

Monocular depth gives the *shape* of a city but not its scale. Many OSM buildings carry a
`height` or `building:levels` tag, which is free ground truth. We rasterise those
footprints onto the scene grid, read the relative structure height under each one, and
fit a single scale factor. Half the buildings are held out to report an honest,
independent error - no circular validation against the data used for fitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw
import scipy.ndimage as ndi

from src.tiles import BBox, USER_AGENT, lonlat_to_global_px

OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
LEVEL_HEIGHT_M = 3.2


@dataclass
class OsmBuilding:
    osm_id: int
    height_m: Optional[float]  # None when the footprint carries no height information
    source: str  # "height" | "levels" | "none"
    ring: List[Tuple[float, float]]  # (lon, lat)

    def to_dict(self) -> Dict:
        return {"osm_id": self.osm_id, "height_m": self.height_m, "source": self.source,
                "ring": [[round(a, 7), round(b, 7)] for a, b in self.ring]}

    @staticmethod
    def from_dict(d: Dict) -> "OsmBuilding":
        return OsmBuilding(d["osm_id"], d["height_m"], d["source"], [tuple(p) for p in d["ring"]])


def parse_height(tags: Dict[str, str]) -> Optional[Tuple[float, str]]:
    h = tags.get("height")
    if h:
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(m|ft|')?\s*$", h)
        if m:
            val = float(m.group(1))
            if m.group(2) in ("ft", "'"):
                val *= 0.3048
            if 2.0 <= val <= 600.0:
                return val, "height"
    lv = tags.get("building:levels")
    if lv:
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", lv)
        if m:
            val = float(m.group(1)) * LEVEL_HEIGHT_M
            if 2.0 <= val <= 600.0:
                return val, "levels"
    return None


def fetch_osm_buildings(bbox: BBox, timeout: float = 30.0, cache_dir: Path | str = "data/cache/osm",
                        rounds: int = 2) -> List[OsmBuilding]:
    """Footprints in bbox. Responses are cached on disk; public mirrors are tried in turn."""
    import hashlib
    import json
    import time

    import requests

    key = hashlib.sha1(f"{bbox.west:.6f},{bbox.south:.6f},{bbox.east:.6f},{bbox.north:.6f}".encode()).hexdigest()
    cache = Path(cache_dir) / f"{key}.json"
    if cache.exists():
        return parse_overpass(json.loads(cache.read_text()))

    # A plain building query is far cheaper for public mirrors than tag-filtered unions;
    # untagged buildings are dropped client-side in parse_overpass().
    q = f'[out:json][timeout:30];way["building"]({bbox.south},{bbox.west},{bbox.north},{bbox.east});out tags geom;'
    last_err: Optional[Exception] = None
    for attempt in range(rounds):
        for url in OVERPASS_MIRRORS:
            try:
                r = requests.post(url, data={"data": q}, headers={"User-Agent": USER_AGENT}, timeout=timeout)
                r.raise_for_status()
                payload = r.json()
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(payload))
                return parse_overpass(payload)
            except Exception as e:  # mirror down / rate limited -> try the next one
                last_err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"All Overpass mirrors failed: {last_err}")


def parse_overpass(payload: Dict) -> List[OsmBuilding]:
    out: List[OsmBuilding] = []
    seen = set()
    for el in payload.get("elements", []):
        if el.get("type") != "way" or el.get("id") in seen or "geometry" not in el:
            continue
        parsed = parse_height(el.get("tags", {})) or (None, "none")
        ring = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(ring) < 3:
            continue
        seen.add(el["id"])
        out.append(OsmBuilding(el["id"], parsed[0], parsed[1], ring))
    return out


def rasterize(buildings: Sequence[OsmBuilding], bbox: BBox, shape: Tuple[int, int]) -> np.ndarray:
    """Label image (int32, 0 = none, i+1 = buildings[i]) on the scene pixel grid."""
    h, w = shape
    z = 20  # any zoom works; we only need the ratio to the bbox extent
    x0, y0 = lonlat_to_global_px(bbox.west, bbox.north, z)
    x1, y1 = lonlat_to_global_px(bbox.east, bbox.south, z)
    sx, sy = w / (x1 - x0), h / (y1 - y0)
    img = Image.new("I", (w, h), 0)
    draw = ImageDraw.Draw(img)
    # Draw larger footprints first so small ones on top keep their own id.
    order = sorted(range(len(buildings)), key=lambda i: -len(buildings[i].ring))
    for i in order:
        pts = []
        for lon, lat in buildings[i].ring:
            gx, gy = lonlat_to_global_px(lon, lat, z)
            pts.append(((gx - x0) * sx, (gy - y0) * sy))
        draw.polygon(pts, fill=i + 1)
    return np.asarray(img, dtype=np.int32)


def calibrate_scale(structure_rel: np.ndarray, osm_labels: np.ndarray,
                    buildings: Sequence[OsmBuilding], min_px: int = 20) -> Optional[Dict]:
    """
    Fit metres-per-unit of relative structure height. Returns None when fewer than four
    usable reference buildings exist.
    """
    if not any(b.height_m for b in buildings):
        return None
    # Erode so that edge pixels blurred into the street do not dilute the roof reading.
    inner = np.zeros_like(osm_labels)
    for i in range(1, len(buildings) + 1):
        m = osm_labels == i
        if m.sum() >= min_px:
            e = ndi.binary_erosion(m, iterations=2)
            inner[e if e.sum() >= min_px // 2 else m] = i
    idx = np.arange(1, len(buildings) + 1)
    counts = np.bincount(inner.ravel(), minlength=len(buildings) + 1)[1:]
    rel = np.asarray(ndi.median(structure_rel, inner, idx))
    pairs = [(float(rel[i]), buildings[i].height_m, buildings[i].source)
             for i in range(len(buildings))
             if buildings[i].height_m and counts[i] >= min_px // 2 and rel[i] > 0.03]
    if len(pairs) < 4:
        return None

    r = np.array([p[0] for p in pairs])
    hm = np.array([p[1] for p in pairs])
    # Hold out every other building: fit on one half, report error on the other.
    fit, val = np.arange(len(r)) % 2 == 0, np.arange(len(r)) % 2 == 1
    k_fit = float(np.median(hm[fit] / r[fit]))
    pred_val = k_fit * r[val]
    err = pred_val - hm[val]
    k_all = float(np.median(hm / r))
    corr = float(np.corrcoef(r, hm)[0, 1]) if len(r) > 2 else float("nan")
    return {
        "height_ref_m": round(k_all, 2),
        "n_buildings": int(len(r)),
        "n_tagged_height": int(sum(1 for p in pairs if p[2] == "height")),
        "holdout": {
            "n": int(val.sum()),
            "mae_m": round(float(np.abs(err).mean()), 2),
            "rmse_m": round(float(np.sqrt((err ** 2).mean())), 2),
            "bias_m": round(float(err.mean()), 2),
            "median_abs_pct": round(float(np.median(np.abs(err) / hm[val]) * 100), 1),
        },
        "pearson_r": round(corr, 3),
        "pairs": [{"rel": round(a, 4), "osm_m": round(b, 1)} for a, b, _ in pairs[:200]],
    }
