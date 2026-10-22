"""
analytics.py - Decision-support analytics on a metric Digital Surface Model (DSM).

Everything here works on plain numpy grids so it can be unit-tested without the depth
model, and so the browser, the REST API and the copilot all read the same numbers.

Conventions used throughout:
    dsm     : (H, W) float32 surface elevation in metres (roofs, canopy, ground).
    ground  : (H, W) float32 bare-earth elevation in metres (estimated from the DSM).
    gsd_m   : ground sample distance, metres per pixel.
    labels  : (H, W) int32 building id per pixel (0 = not a building).
    Row 0 is the north edge of the scene, column 0 the west edge.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import scipy.ndimage as ndi

# Rough planning constants. They are deliberately exposed so the UI can show them next to
# any derived number - an estimate is only honest if its assumptions are visible.
FLOOR_HEIGHT_M = 3.2
FLOOR_AREA_PER_PERSON_M2 = 40.0
PV_USABLE_ROOF_FRACTION = 0.6
PV_EFFICIENCY = 0.20
PV_PERFORMANCE_RATIO = 0.80
CLEAR_SKY_TO_ACTUAL = 0.75  # long-run cloud / haze derating of clear-sky irradiance
HOUSEHOLD_KWH_PER_YEAR = 1200.0


# --------------------------------------------------------------------------------------
# Ground / structure separation
# --------------------------------------------------------------------------------------

def estimate_ground(dsm: np.ndarray, gsd_m: float, max_footprint_m: float = 80.0) -> np.ndarray:
    """
    Estimate the bare-earth surface under a DSM.

    A grey-scale morphological opening with a window wider than the largest building
    removes everything that sticks up (roofs, canopy) and keeps the terrain. A light
    Gaussian blur then removes the blocky artefacts of the square window.
    """
    arr = np.asarray(dsm, dtype=np.float32)
    win = _window_px(arr, gsd_m, max_footprint_m)
    # Pad once up front: letting erosion and dilation each extend the border on their own
    # pulls the ground down along the scene edges by up to win/2 pixels of slope.
    padded = np.pad(arr, win, mode="edge")
    opened = ndi.grey_opening(padded, size=(win, win))
    smoothed = ndi.gaussian_filter(opened, sigma=max(1.0, win / 6.0))[win:-win, win:-win]
    # Smoothing can lift the ground above the surface in narrow valleys; never allow that.
    return np.minimum(smoothed, arr).astype(np.float32)


def _window_px(arr: np.ndarray, gsd_m: float, size_m: float) -> int:
    return int(np.clip(round(size_m / max(gsd_m, 1e-3)), 3, max(3, min(arr.shape) // 2)))


def refine_ground(dsm: np.ndarray, structure_mask: np.ndarray, gsd_m: float,
                  max_footprint_m: float = 80.0) -> np.ndarray:
    """
    Re-estimate the ground by interpolating only from cells known not to be structures.

    The opening in estimate_ground() shaves curved hillsides; once buildings are known,
    a normalised convolution over the remaining cells recovers that terrain while still
    bridging under every footprint.
    """
    arr = np.asarray(dsm, dtype=np.float32)
    keep = ~ndi.binary_dilation(structure_mask, iterations=1)
    if keep.sum() < 0.05 * keep.size:
        return estimate_ground(arr, gsd_m, max_footprint_m)
    sigma = max(1.0, _window_px(arr, gsd_m, max_footprint_m) / 5.0)
    num = ndi.gaussian_filter(np.where(keep, arr, 0.0), sigma, mode="nearest")
    den = ndi.gaussian_filter(keep.astype(np.float32), sigma, mode="nearest")
    interp = num / np.maximum(den, 1e-6)
    ground = np.where(keep, arr, interp)
    ground = ndi.gaussian_filter(ground, 1.0, mode="nearest")
    return np.minimum(ground, arr).astype(np.float32)


def analyze_surface(dsm: np.ndarray, gsd_m: float, min_height_m: float = 2.5,
                    min_area_m2: float = 30.0) -> Tuple[np.ndarray, np.ndarray, List["Building"]]:
    """Ground, building labels and building stats in one call (two-pass refinement)."""
    g0 = estimate_ground(dsm, gsd_m)
    # Everything elevated in the first pass - buildings *and* trees / soft blobs that fail the
    # wall test - is excluded when re-interpolating the ground.
    elevated = (np.asarray(dsm, np.float32) - g0) > min_height_m
    ground = refine_ground(dsm, elevated, gsd_m)
    labels, buildings = extract_buildings(dsm, ground, gsd_m, min_height_m, min_area_m2)
    return ground, labels, buildings


@dataclass
class Building:
    id: int
    row: float
    col: float
    bbox: Tuple[int, int, int, int]  # (row0, col0, row1, col1), end-exclusive
    footprint_m2: float
    height_m: float  # max height above ground
    mean_height_m: float
    base_elev_m: float
    roof_elev_m: float
    volume_m3: float
    floors: int
    est_occupants: int
    osm_id: Optional[int] = None
    ref_height_m: Optional[float] = None  # independent height from OpenStreetMap tags


def extract_buildings(
    dsm: np.ndarray,
    ground: np.ndarray,
    gsd_m: float,
    min_height_m: float = 2.5,
    min_area_m2: float = 30.0,
    max_buildings: int = 3000,
    min_wall_slope: float = 0.35,
    split_spacing_m: float = 14.0,
) -> Tuple[np.ndarray, List[Building]]:
    """
    Segment elevated structures from the normalised DSM (nDSM = DSM - ground).

    1. Threshold the nDSM and drop components without walls (median outline slope below
       min_wall_slope - curved-hillside artefacts and soft canopy blobs).
    2. Monocular depth blurs neighbouring roofs together, so split each blob with a
       marker-controlled watershed seeded at roof peaks at least split_spacing_m apart.

    Returns a label image (0 = background, 1..N = building id, tallest first) and stats.
    """
    ndsm = np.asarray(dsm, dtype=np.float32) - np.asarray(ground, dtype=np.float32)
    mask = ndsm > min_height_m
    mask = ndi.binary_opening(mask, structure=np.ones((3, 3), bool))
    comp, n = ndi.label(mask, structure=np.ones((3, 3), bool))
    if n == 0:
        return np.zeros(ndsm.shape, np.int32), []

    idx = np.arange(1, n + 1)
    gy, gx = np.gradient(np.asarray(dsm, np.float32), gsd_m)
    grad = np.hypot(gx, gy)
    boundary = mask & ~ndi.binary_erosion(mask, structure=np.ones((3, 3), bool))
    wall = np.nan_to_num(np.asarray(ndi.median(np.where(boundary, grad, 0.0),
                                               np.where(boundary, comp, 0), idx), np.float64))
    good = np.zeros(n + 1, bool)
    good[1:] = wall > min_wall_slope
    accepted = good[comp]
    if not accepted.any():
        return np.zeros(ndsm.shape, np.int32), []

    smooth = ndi.gaussian_filter(ndsm, 1.0)
    size = max(3, int(round(split_spacing_m / gsd_m)))
    peaks = accepted & (smooth == ndi.maximum_filter(smooth, size=size)) & (smooth > min_height_m + 0.5)
    markers, n_peaks = ndi.label(peaks, structure=np.ones((3, 3), bool))
    # Blobs without a qualifying peak keep their own component as a single marker.
    orphan = accepted & ~np.isin(comp, np.unique(comp[peaks]))
    if orphan.any():
        ol, no = ndi.label(orphan, structure=np.ones((3, 3), bool))
        markers = np.where(ol > 0, ol + n_peaks, markers)
        n_peaks += no
    bg = n_peaks + 1
    markers = markers.astype(np.int32)
    markers[~ndi.binary_dilation(accepted, iterations=1)] = bg
    span = float(smooth.max() - smooth.min()) or 1.0
    cost = np.clip((smooth.max() - smooth) / span * 65535, 0, 65535).astype(np.uint16)
    ws = ndi.watershed_ift(cost, markers)
    ws[(ws == bg) | ~accepted] = 0
    return buildings_from_labels(dsm, ground, gsd_m, ws, min_area_m2, max_buildings)


def buildings_from_labels(
    dsm: np.ndarray,
    ground: np.ndarray,
    gsd_m: float,
    raw_labels: np.ndarray,
    min_area_m2: float = 30.0,
    max_buildings: int = 3000,
    min_height_m: float = 0.0,
) -> Tuple[np.ndarray, List[Building]]:
    """
    Per-building statistics for any footprint label image (our segmentation or OSM
    footprints). Relabels 1..N by height, tallest first.
    """
    ndsm = np.asarray(dsm, dtype=np.float32) - np.asarray(ground, dtype=np.float32)
    raw_labels = np.asarray(raw_labels, np.int64)
    n = int(raw_labels.max())
    if n == 0:
        return np.zeros(ndsm.shape, np.int32), []
    idx = np.arange(1, n + 1)
    px_area = gsd_m * gsd_m
    counts = np.bincount(raw_labels.ravel(), minlength=n + 1)[1:]
    pos_h = np.clip(ndsm, 0, None)
    # Robust building height: 90th percentile of the footprint, not a single noisy pixel.
    p90_raw = np.asarray(ndi.labeled_comprehension(pos_h, raw_labels, idx, lambda v: np.percentile(v, 90),
                                                   float, 0.0))
    keep = (counts * px_area >= min_area_m2) & (p90_raw > min_height_m)
    if not np.any(keep):
        return np.zeros(ndsm.shape, np.int32), []
    order = [int(i) for i in idx[keep][np.argsort(-p90_raw[keep], kind="stable")]][:max_buildings]

    remap = np.zeros(n + 1, np.int32)
    remap[order] = np.arange(1, len(order) + 1, dtype=np.int32)
    labels = remap[raw_labels]

    m = len(order)
    lidx = np.arange(1, m + 1)
    counts = np.bincount(labels.ravel(), minlength=m + 1)[1:]
    sum_h = np.bincount(labels.ravel(), weights=pos_h.ravel(), minlength=m + 1)[1:]
    p90 = p90_raw[np.asarray(order) - 1]
    base = np.asarray(ndi.mean(ground, labels, lidx))
    roof = base + p90
    centers = ndi.center_of_mass(labels > 0, labels, lidx)
    slices = ndi.find_objects(labels)

    buildings: List[Building] = []
    for i in range(m):
        area = float(counts[i] * px_area)
        mean_h = float(sum_h[i] / max(counts[i], 1))
        floors = max(1, int(round(float(p90[i]) / FLOOR_HEIGHT_M)))
        sl = slices[i]
        buildings.append(
            Building(
                id=i + 1,
                row=round(float(centers[i][0]), 2),
                col=round(float(centers[i][1]), 2),
                bbox=(sl[0].start, sl[1].start, sl[0].stop, sl[1].stop),
                footprint_m2=round(area, 1),
                height_m=round(float(p90[i]), 2),
                mean_height_m=round(mean_h, 2),
                base_elev_m=round(float(base[i]), 2),
                roof_elev_m=round(float(roof[i]), 2),
                volume_m3=round(float(sum_h[i] * px_area), 1),
                floors=floors,
                est_occupants=int(round(area * floors / FLOOR_AREA_PER_PERSON_M2)),
            )
        )
    return labels.astype(np.int32), buildings


def buildings_summary(buildings: Sequence[Building]) -> Dict[str, float]:
    if not buildings:
        return {"count": 0, "total_footprint_m2": 0.0, "tallest_m": 0.0, "mean_height_m": 0.0,
                "total_volume_m3": 0.0, "est_occupants": 0}
    heights = np.array([b.height_m for b in buildings])
    return {
        "count": len(buildings),
        "total_footprint_m2": round(float(sum(b.footprint_m2 for b in buildings)), 1),
        "tallest_m": round(float(heights.max()), 1),
        "mean_height_m": round(float(heights.mean()), 1),
        "total_volume_m3": round(float(sum(b.volume_m3 for b in buildings)), 1),
        "est_occupants": int(sum(b.est_occupants for b in buildings)),
    }


# --------------------------------------------------------------------------------------
# Flooding
# --------------------------------------------------------------------------------------

FloodSource = Union[str, Tuple[int, int], np.ndarray]


def flood_mask(dsm: np.ndarray, level_m: float, source: FloodSource = "lowest",
               water: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Cells that are under water when the water surface stands at `level_m`.

    Unlike a naive "everything below the level" threshold, water only reaches a cell if
    it is hydraulically connected to the source, so a basin behind a ridge stays dry.

    source:
        "lowest" - water enters from the permanent water body (river / sea) when `water`
                   is given, otherwise from the lowest part of the scene.
        "edges"  - water enters from any scene border (storm surge, regional flood).
        "rain"   - every cell below the level fills (ponding; no connectivity rule).
        (row, col) - a clicked point, e.g. a river bank or a breached embankment.
        bool array - explicit seed cells.
    """
    arr = np.asarray(dsm, dtype=np.float32)
    below = arr < level_m
    if isinstance(source, str) and source == "rain":
        return below
    if not np.any(below):
        return below

    if isinstance(source, np.ndarray):
        seeds = source.astype(bool)
    elif isinstance(source, str) and source == "edges":
        seeds = np.zeros_like(below)
        seeds[0, :] = seeds[-1, :] = True
        seeds[:, 0] = seeds[:, -1] = True
    elif isinstance(source, str) and source == "lowest":
        if water is not None and water.any():
            seeds = water.astype(bool)
        else:
            seeds = arr <= np.percentile(arr, 0.5)
    elif isinstance(source, (tuple, list)) and len(source) == 2:
        seeds = np.zeros_like(below)
        r = int(np.clip(source[0], 0, arr.shape[0] - 1))
        c = int(np.clip(source[1], 0, arr.shape[1] - 1))
        seeds[r, c] = True
    else:
        raise ValueError(f"Unknown flood source: {source!r}")

    labels, _ = ndi.label(below)  # 4-connectivity: water does not leak through diagonal gaps
    wet_ids = np.unique(labels[seeds & below])
    wet_ids = wet_ids[wet_ids > 0]
    if wet_ids.size == 0:
        return np.zeros_like(below)
    return np.isin(labels, wet_ids)


def _severity(depth_m: float) -> str:
    if depth_m < 0.3:
        return "minor"
    if depth_m < 1.0:
        return "moderate"
    if depth_m < 2.5:
        return "severe"
    return "critical"


def flood_report(
    dsm: np.ndarray,
    ground: np.ndarray,
    gsd_m: float,
    level_m: float,
    labels: Optional[np.ndarray] = None,
    buildings: Optional[Sequence[Building]] = None,
    source: FloodSource = "lowest",
    max_listed: int = 25,
    water: Optional[np.ndarray] = None,
) -> Dict:
    """Full flood impact report for one water level (permanent water is not counted as flooded)."""
    wet = flood_mask(dsm, level_m, source, water)
    if water is not None:
        wet &= ~water.astype(bool)
    px_area = gsd_m * gsd_m
    depth = np.where(wet, level_m - np.asarray(dsm, np.float32), 0.0)
    n_wet = int(wet.sum())

    report: Dict = {
        "level_m": round(float(level_m), 2),
        "source": source if isinstance(source, str) else "point",
        "flooded_area_m2": round(n_wet * px_area, 1),
        "flooded_fraction": round(n_wet / wet.size, 4),
        "mean_depth_m": round(float(depth[wet].mean()) if n_wet else 0.0, 2),
        "max_depth_m": round(float(depth.max()) if n_wet else 0.0, 2),
        "water_volume_m3": round(float(depth.sum() * px_area), 1),
        "buildings_affected": 0,
        "est_people_affected": 0,
        "severity_counts": {"minor": 0, "moderate": 0, "severe": 0, "critical": 0},
        "affected": [],
    }

    if labels is None or not buildings:
        return report

    # A building is affected when water touches its footprint ring.
    ring_source = ndi.binary_dilation(wet, structure=np.ones((3, 3), bool), iterations=2)
    touched = np.unique(labels[ring_source & (labels > 0)])
    by_id = {b.id: b for b in buildings}
    affected = []
    for bid in touched:
        b = by_id.get(int(bid))
        if b is None:
            continue
        water_depth = float(level_m - b.base_elev_m)
        if water_depth <= 0.05:
            continue
        sev = _severity(water_depth)
        floors_under = min(b.floors, int(math.ceil(water_depth / FLOOR_HEIGHT_M)))
        people = int(round(b.est_occupants * floors_under / max(b.floors, 1)))
        affected.append({
            "id": b.id,
            "row": b.row,
            "col": b.col,
            "water_depth_m": round(water_depth, 2),
            "severity": sev,
            "floors_underwater": floors_under,
            "est_people": people,
            "submerged": water_depth >= b.height_m,
        })
        report["severity_counts"][sev] += 1

    affected.sort(key=lambda a: -a["water_depth_m"])
    report["buildings_affected"] = len(affected)
    report["est_people_affected"] = int(sum(a["est_people"] for a in affected))
    report["affected"] = affected[:max_listed]
    report["affected_ids"] = [a["id"] for a in affected]
    report["affected_severity"] = {str(a["id"]): a["severity"] for a in affected}
    return report


def safe_zones(
    dsm: np.ndarray,
    ground: np.ndarray,
    gsd_m: float,
    level_m: float,
    labels: Optional[np.ndarray] = None,
    buildings: Optional[Sequence[Building]] = None,
    source: FloodSource = "lowest",
    n: int = 5,
    min_area_m2: float = 400.0,
    water: Optional[np.ndarray] = None,
) -> List[Dict]:
    """
    Candidate assembly points for evacuation: the largest dry open-ground patches with the
    most freeboard above the water, plus the tallest unaffected-roof buildings as vertical
    refuges.
    """
    wet = flood_mask(dsm, level_m, source, water)
    if water is not None:
        wet |= water.astype(bool)
    open_ground = ~wet
    if labels is not None:
        open_ground &= labels == 0
    open_ground = ndi.binary_erosion(open_ground, iterations=2)
    zl, nz = ndi.label(open_ground)
    zones: List[Dict] = []
    px_area = gsd_m * gsd_m
    if nz:
        idx = np.arange(1, nz + 1)
        areas = np.bincount(zl.ravel(), minlength=nz + 1)[1:] * px_area
        elev = np.asarray(ndi.mean(ground, zl, idx))
        centers = ndi.center_of_mass(open_ground, zl, idx)
        for i in np.argsort(-areas):
            if areas[i] < min_area_m2:
                break
            freeboard = float(elev[i] - level_m)
            if freeboard < 0.5:
                continue
            # Snap the marker onto an actual member cell (centroids of L-shapes can be outside).
            rr, cc = np.nonzero(zl == idx[i])
            k = int(np.argmin((rr - centers[i][0]) ** 2 + (cc - centers[i][1]) ** 2))
            zones.append({
                "kind": "open_ground",
                "row": int(rr[k]),
                "col": int(cc[k]),
                "area_m2": round(float(areas[i]), 1),
                "elev_m": round(float(elev[i]), 2),
                "freeboard_m": round(freeboard, 2),
            })
            if len(zones) >= n:
                break

    if buildings:
        refuges = [b for b in buildings if b.roof_elev_m - level_m > FLOOR_HEIGHT_M * 2]
        refuges.sort(key=lambda b: -(b.footprint_m2 * min(b.floors, 10)))
        for b in refuges[:3]:
            zones.append({
                "kind": "vertical_refuge",
                "building_id": b.id,
                "row": int(round(b.row)),
                "col": int(round(b.col)),
                "area_m2": b.footprint_m2,
                "elev_m": b.roof_elev_m,
                "freeboard_m": round(b.roof_elev_m - level_m, 2),
            })
    return zones


# --------------------------------------------------------------------------------------
# Terrain hazard
# --------------------------------------------------------------------------------------

def slope_degrees(surface: np.ndarray, gsd_m: float) -> np.ndarray:
    gy, gx = np.gradient(np.asarray(surface, np.float32), gsd_m)
    return np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)


def landslide_susceptibility(ground: np.ndarray, gsd_m: float) -> Dict:
    """Share of open terrain in the standard slope-hazard classes (on bare earth, not roofs)."""
    s = slope_degrees(ground, gsd_m)
    n = s.size
    return {
        "stable_lt15": round(float((s < 15).sum() / n), 4),
        "caution_15_30": round(float(((s >= 15) & (s < 30)).sum() / n), 4),
        "hazard_gt30": round(float((s >= 30).sum() / n), 4),
        "max_slope_deg": round(float(s.max()), 1),
    }


# --------------------------------------------------------------------------------------
# Sun & solar potential
# --------------------------------------------------------------------------------------

def sun_position(lat: float, lon: float, when: datetime) -> Tuple[float, float]:
    """
    Solar azimuth (degrees clockwise from north) and elevation (degrees above horizon).

    NOAA general solar position equations; accurate to well under a degree, which is far
    below the uncertainty of a monocular DSM.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    g = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)

    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))

    tst = hour * 60.0 + eqtime + 4.0 * lon  # true solar time, minutes
    ha = math.radians(tst / 4.0 - 180.0)
    phi = math.radians(lat)

    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zen = math.acos(cos_zen)
    elevation = 90.0 - math.degrees(zen)

    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi)))
    azimuth = (az + 180.0) % 360.0
    return azimuth, elevation


def shadow_mask(dsm: np.ndarray, gsd_m: float, azimuth_deg: float, elevation_deg: float,
                max_distance_m: Optional[float] = None) -> np.ndarray:
    """
    True where a cell is in cast shadow for the given sun direction (ray-marching the DSM).
    """
    arr = np.asarray(dsm, np.float32)
    if elevation_deg <= 0.5:
        return np.ones(arr.shape, bool)
    h, w = arr.shape
    tan_el = math.tan(math.radians(elevation_deg))
    relief = float(arr.max() - arr.min())
    reach = relief / tan_el if max_distance_m is None else max_distance_m
    steps = int(min(max(h, w), math.ceil(reach / gsd_m)))
    if steps < 1:
        return np.zeros(arr.shape, bool)

    az = math.radians(azimuth_deg)
    # Direction towards the sun in pixel space: north is -row, east is +col.
    dcol, drow = math.sin(az), -math.cos(az)

    shadow = np.zeros(arr.shape, bool)
    pad = steps + 1
    padded = np.pad(arr, pad, mode="constant", constant_values=-np.inf)
    for s in range(1, steps + 1):
        ro = int(round(drow * s))
        co = int(round(dcol * s))
        window = padded[pad + ro:pad + ro + h, pad + co:pad + co + w]
        shadow |= window > arr + s * gsd_m * tan_el
    return shadow


def _clear_sky_horizontal(elevation_deg: float) -> float:
    """Global horizontal irradiance (W/m2) under clear skies (Meinel air-mass model)."""
    if elevation_deg <= 0:
        return 0.0
    sin_el = math.sin(math.radians(elevation_deg))
    air_mass = 1.0 / max(sin_el, 0.05)
    dni = 1353.0 * 0.7 ** (air_mass ** 0.678)
    return dni * sin_el * 1.1  # +10% diffuse


def solar_potential(
    dsm: np.ndarray,
    gsd_m: float,
    lat: float,
    lon: float,
    labels: Optional[np.ndarray] = None,
    buildings: Optional[Sequence[Building]] = None,
    year: int = 2026,
    hours: Sequence[float] = tuple(range(6, 19)),
    utc_offset_h: Optional[float] = None,
) -> Dict:
    """
    Annual rooftop solar estimate with real cast shadows from the reconstructed city.

    The year is represented by the 21st of every second month at hourly steps, which keeps
    the ray-marched shadow count small (~80 sun positions) while capturing the seasonal arc.
    """
    if utc_offset_h is None:
        utc_offset_h = round(lon / 15.0)
    tz = timezone(timedelta(hours=utc_offset_h))
    arr = np.asarray(dsm, np.float32)
    insolation = np.zeros(arr.shape, np.float64)  # Wh/m2 summed over sampled hours
    months = (1, 3, 5, 7, 9, 11)
    for month in months:
        for local_h in hours:
            when = datetime(year, month, 21, int(local_h), int(round((local_h % 1) * 60)), tzinfo=tz)
            az, el = sun_position(lat, lon, when)
            ghi = _clear_sky_horizontal(el)
            if ghi <= 0:
                continue
            lit = ~shadow_mask(arr, gsd_m, az, el)
            insolation += lit * ghi
    # Each sample hour represents ~(365 / len(months)) days.
    annual_kwh_m2 = insolation * (365.0 / len(months)) / 1000.0 * CLEAR_SKY_TO_ACTUAL

    result: Dict = {
        "assumptions": {
            "usable_roof_fraction": PV_USABLE_ROOF_FRACTION,
            "panel_efficiency": PV_EFFICIENCY,
            "performance_ratio": PV_PERFORMANCE_RATIO,
            "cloud_derating": CLEAR_SKY_TO_ACTUAL,
            "household_kwh_per_year": HOUSEHOLD_KWH_PER_YEAR,
        },
        "scene_mean_kwh_m2_yr": round(float(annual_kwh_m2.mean()), 1),
        "buildings": [],
        "total_kwh_yr": 0.0,
        "households_powered": 0,
    }
    if labels is None or not buildings:
        return result

    m = len(buildings)
    sums = np.bincount(labels.ravel(), weights=annual_kwh_m2.ravel(), minlength=m + 1)[1:]
    counts = np.bincount(labels.ravel(), minlength=m + 1)[1:]
    px_area = gsd_m * gsd_m
    per_building = []
    for b in buildings:
        n_px = counts[b.id - 1]
        if n_px == 0:
            continue
        roof_kwh_m2 = float(sums[b.id - 1] / n_px)
        pv_kwh = n_px * px_area * PV_USABLE_ROOF_FRACTION * roof_kwh_m2 * PV_EFFICIENCY * PV_PERFORMANCE_RATIO
        per_building.append({
            "id": b.id,
            "roof_kwh_m2_yr": round(roof_kwh_m2, 1),
            "pv_kwh_yr": round(pv_kwh, 0),
            "pv_kwp": round(n_px * px_area * PV_USABLE_ROOF_FRACTION * PV_EFFICIENCY, 1),
        })
    per_building.sort(key=lambda x: -x["pv_kwh_yr"])
    total = float(sum(x["pv_kwh_yr"] for x in per_building))
    result["buildings"] = per_building
    result["total_kwh_yr"] = round(total, 0)
    result["households_powered"] = int(total // HOUSEHOLD_KWH_PER_YEAR)
    return result


def building_to_dict(b: Building) -> Dict:
    d = asdict(b)
    d["bbox"] = list(b.bbox)
    return d


def building_from_dict(d: Dict) -> Building:
    d = dict(d)
    d["bbox"] = tuple(d["bbox"])
    return Building(**d)
