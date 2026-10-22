"""OSM parsing, rasterisation and hold-out calibration on a synthetic city."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.osm import OsmBuilding, calibrate_scale, parse_height, parse_overpass, rasterize
from src.tiles import BBox


def test_parse_height_variants():
    assert parse_height({"height": "45"}) == (45.0, "height")
    assert parse_height({"height": "100 ft"})[0] == 100 * 0.3048
    assert parse_height({"building:levels": "4"}) == (4 * 3.2, "levels")
    assert parse_height({"height": "tall"}) is None
    assert parse_height({}) is None


def test_parse_overpass_keeps_untagged_footprints():
    payload = {"elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes", "height": "20"},
         "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}]},
        {"type": "way", "id": 2, "tags": {"building": "yes"},
         "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 0}, {"lon": 1, "lat": 1}]},
    ]}
    out = parse_overpass(payload)
    assert [b.height_m for b in out] == [20.0, None]


def test_rasterize_and_calibrate_recovers_scale():
    bbox = BBox(77.2000, 28.6000, 77.2100, 28.6100)
    rng = np.random.default_rng(0)
    shape = (400, 400)
    struct = np.zeros(shape, np.float32)
    buildings = []
    true_k = 42.0
    for i in range(30):
        r0, c0 = rng.integers(10, 360, 2)
        rel = float(rng.uniform(0.1, 1.0))
        lon0 = bbox.west + (bbox.east - bbox.west) * c0 / shape[1]
        lon1 = bbox.west + (bbox.east - bbox.west) * (c0 + 20) / shape[1]
        lat0 = bbox.north - (bbox.north - bbox.south) * r0 / shape[0]
        lat1 = bbox.north - (bbox.north - bbox.south) * (r0 + 20) / shape[0]
        buildings.append(OsmBuilding(i, true_k * rel, "height", [(lon0, lat0), (lon1, lat0), (lon1, lat1), (lon0, lat1)]))
        struct[r0:r0 + 20, c0:c0 + 20] = rel
    labels = rasterize(buildings, bbox, shape)
    assert labels.max() > 0
    fit = calibrate_scale(struct, labels, buildings)
    assert fit is not None
    assert abs(fit["height_ref_m"] - true_k) / true_k < 0.1
