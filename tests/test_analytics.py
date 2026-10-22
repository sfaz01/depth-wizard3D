"""Unit tests for src/analytics.py against the procedural river town with known truth."""

from datetime import datetime, timezone
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analytics as A
from src.synthetic import make_river_town


@pytest.fixture(scope="module")
def town():
    t = make_river_town(256, gsd_m=2.0)
    ground, labels, buildings = A.analyze_surface(t["dsm"], float(t["gsd_m"]))
    return t, ground, labels, buildings


def test_ground_estimate_removes_buildings(town):
    t, ground, _, _ = town
    roofs = t["labels"] > 0
    err = np.abs(ground - t["ground"])
    # Under roofs the estimate should be close to the true terrain, not the roof.
    assert np.median(err[roofs]) < 2.0
    assert np.median(err[~roofs]) < 1.0


def test_building_extraction_matches_truth(town):
    t, _, labels, buildings = town
    n_true = int(t["labels"].max())
    assert abs(len(buildings) - n_true) <= max(2, n_true // 10)
    # Tallest building first, and its height close to the tallest truth height.
    assert buildings[0].height_m == max(b.height_m for b in buildings)
    assert abs(buildings[0].height_m - float(t["heights"].max())) < 3.0
    # Label image ids are 1..N in the same order.
    assert labels.max() == len(buildings)
    for b in buildings[:5]:
        assert labels[int(round(b.row)), int(round(b.col))] == b.id
        assert b.footprint_m2 > 0 and b.volume_m3 > 0 and b.floors >= 1


def test_flood_is_monotonic_and_connected(town):
    t, ground, labels, buildings = town
    gsd = float(t["gsd_m"])
    prev = -1.0
    for level in (9.0, 11.0, 14.0, 18.0):
        rep = A.flood_report(t["dsm"], ground, gsd, level, labels, buildings)
        assert rep["flooded_area_m2"] >= prev
        prev = rep["flooded_area_m2"]
    # Water at 9 m only fills the river channel (which sits at <= 8 m).
    wet = A.flood_mask(t["dsm"], 9.0)
    assert wet[t["river"]].mean() > 0.95
    assert wet[~t["river"]].mean() < 0.05


def test_flood_connectivity_blocks_isolated_basin():
    dsm = np.full((50, 50), 10.0, np.float32)
    dsm[:, 24:26] = 30.0  # ridge
    dsm[10:20, 5:15] = 2.0  # west basin
    dsm[30:40, 35:45] = 1.0  # east basin (lowest -> source)
    wet = A.flood_mask(dsm, 5.0, "lowest")
    assert wet[35, 40] and not wet[15, 10]
    assert A.flood_mask(dsm, 5.0, "rain")[15, 10]
    assert A.flood_mask(dsm, 5.0, (15, 10))[15, 10]
    assert not A.flood_mask(dsm, 5.0, (15, 10))[35, 40]


def test_flood_report_counts_buildings(town):
    t, ground, labels, buildings = town
    rep = A.flood_report(t["dsm"], ground, float(t["gsd_m"]), 16.0, labels, buildings)
    assert rep["buildings_affected"] > 0
    assert rep["est_people_affected"] >= 0
    assert sum(rep["severity_counts"].values()) == rep["buildings_affected"]
    assert all(a["water_depth_m"] > 0 for a in rep["affected"])


def test_safe_zones_are_dry(town):
    t, ground, labels, buildings = town
    level = 14.0
    zones = A.safe_zones(t["dsm"], ground, float(t["gsd_m"]), level, labels, buildings)
    assert zones
    wet = A.flood_mask(t["dsm"], level)
    for z in zones:
        assert not wet[z["row"], z["col"]]
        assert z["freeboard_m"] > 0


def test_sun_position_reference_values():
    # Equator, March equinox, local solar noon -> sun almost overhead.
    az, el = A.sun_position(0.0, 0.0, datetime(2026, 3, 20, 12, 7, tzinfo=timezone.utc))
    assert el > 88.0
    # New Delhi, June solstice ~noon IST: high sun in the south-ish sky.
    az, el = A.sun_position(28.61, 77.21, datetime(2026, 6, 21, 6, 50, tzinfo=timezone.utc))
    assert 80 < el < 86
    # New Delhi, winter afternoon: sun in the south-west.
    az, el = A.sun_position(28.61, 77.21, datetime(2026, 12, 21, 10, 30, tzinfo=timezone.utc))
    assert 190 < az < 240 and 15 < el < 35


def test_shadow_falls_away_from_sun():
    dsm = np.zeros((60, 60), np.float32)
    dsm[28:32, 28:32] = 20.0  # tower
    sh = A.shadow_mask(dsm, 1.0, azimuth_deg=90.0, elevation_deg=45.0)  # sun in the east
    assert sh[30, 20]  # shadow cast to the west
    assert not sh[30, 45]
    assert not sh[10, 30]


def test_solar_potential_shaded_roof_gets_less():
    dsm = np.zeros((80, 80), np.float32)
    labels = np.zeros((80, 80), np.int32)
    dsm[40:50, 30:40] = 6.0
    labels[40:50, 30:40] = 1  # low roof
    dsm[52:60, 30:40] = 40.0  # tower to the south -> shades the low roof in the north hemisphere
    labels[52:60, 30:40] = 2
    b1 = A.Building(1, 45, 35, (40, 30, 50, 40), 100, 6, 6, 0, 6, 600, 2, 5)
    b2 = A.Building(2, 56, 35, (52, 30, 60, 40), 64, 40, 40, 0, 40, 2560, 12, 19)
    res = A.solar_potential(dsm, 1.0, 28.6, 77.2, labels, [b1, b2])
    per = {b["id"]: b for b in res["buildings"]}
    assert per[2]["roof_kwh_m2_yr"] > per[1]["roof_kwh_m2_yr"]
    assert 1000 < per[2]["roof_kwh_m2_yr"] < 2600  # plausible Indian rooftop yield
    assert res["total_kwh_yr"] > 0


def test_landslide_classes_sum_to_one(town):
    t, ground, _, _ = town
    res = A.landslide_susceptibility(ground, float(t["gsd_m"]))
    total = res["stable_lt15"] + res["caution_15_30"] + res["hazard_gt30"]
    assert abs(total - 1.0) < 1e-3


def test_detect_water_finds_river():
    from src.scene import detect_water, split_structures
    from src.synthetic import relative_from_dsm

    t = make_river_town(256, gsd_m=2.0)
    _, struct = split_structures(relative_from_dsm(t["dsm"]), 2.0)
    water = detect_water(t["rgb"], struct, 2.0, dem=t["ground"])
    river = t["river"]
    iou = (water & river).sum() / max(1, (water | river).sum())
    assert iou > 0.6, iou


def test_flood_from_permanent_water_ignores_isolated_pits():
    dsm = np.full((40, 40), 10.0, np.float32)
    dsm[:, 18:22] = 5.0            # river channel
    dsm[5:8, 5:8] = 0.0            # deep pit (DEM artefact) far from the river
    river = np.zeros_like(dsm, bool)
    river[:, 18:22] = True
    wet = A.flood_mask(dsm, 6.0, "lowest", water=river)
    assert wet[:, 18:22].all() and not wet[6, 6]
    rep = A.flood_report(dsm, dsm, 1.0, 6.0, water=river)
    assert rep["flooded_area_m2"] == 0  # the river itself is not "flooded"
