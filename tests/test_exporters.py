"""GLB / STL writers: structural validity and watertightness."""

import json
import struct
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exporters import write_glb, write_stl
from src.synthetic import make_river_town


def _town():
    t = make_river_town(96, gsd_m=2.0)
    return t["dsm"], t["rgb"]


def test_glb_structure():
    dsm, rgb = _town()
    data = write_glb(dsm, rgb, 2.0, size_m=0.5, max_dim=64)
    magic, version, total = struct.unpack("<4sII", data[:12])
    assert magic == b"glTF" and version == 2 and total == len(data)
    jlen, jtype = struct.unpack("<I4s", data[12:20])
    assert jtype == b"JSON"
    gltf = json.loads(data[20:20 + jlen])
    pos = gltf["accessors"][0]
    assert pos["count"] == 64 * 64
    extent = np.array(pos["max"]) - np.array(pos["min"])
    assert abs(max(extent[0], extent[2]) - 0.5) < 1e-3  # tabletop scale honoured
    blen, btype = struct.unpack("<I4s", data[20 + jlen:28 + jlen])
    assert btype == b"BIN\x00" and blen == gltf["buffers"][0]["byteLength"]


def test_stl_is_watertight_and_outward():
    dsm, _ = _town()
    data = write_stl(dsm, 2.0, size_mm=100, max_dim=40)
    n = struct.unpack("<I", data[80:84])[0]
    assert len(data) == 84 + n * 50
    rec = np.frombuffer(data[84:], dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    tris = rec["v"].astype(np.float64)
    edges = Counter()
    for t in np.round(tris, 4):
        pts = [tuple(p) for p in t]
        for i in range(3):
            a, b = pts[i], pts[(i + 1) % 3]
            edges[(a, b)] += 1
    # Watertight + consistently oriented: every directed edge has exactly one reverse twin.
    assert all(edges.get((b, a), 0) == c for (a, b), c in edges.items())
    vol = np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0
    assert vol > 0
