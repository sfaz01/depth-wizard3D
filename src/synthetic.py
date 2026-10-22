"""
synthetic.py - Procedural "river town" scene with known ground truth.

Used for two things:
1. Unit tests: buildings, flood levels and slopes are known exactly.
2. Offline demo: the viewer always has a scene to show, even with no internet or model.
"""

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import scipy.ndimage as ndi


def make_river_town(size: int = 256, gsd_m: float = 2.0, seed: int = 7) -> Dict[str, np.ndarray]:
    """
    Returns dict with:
        dsm (m), ground (m), rgb (uint8 HxWx3), labels (int32 ground-truth building ids),
        heights (float32 per building id, index 0 unused), gsd_m.

    Layout: a river runs north-south through a valley (ground ~10 m at the river, rising to
    ~30 m on the east hills); a grid of rectangular buildings sits on both banks.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    u = xx / size
    v = yy / size

    river_x = 0.38 + 0.06 * np.sin(v * 2 * np.pi * 1.2)
    dist = np.abs(u - river_x)
    ground = 10.0 + 22.0 * np.clip(dist - 0.04, 0, None) ** 1.2 * 3.0
    ground += 14.0 * np.clip(u - 0.7, 0, None) ** 2 * 10  # eastern hills
    ground += 1.5 * ndi.gaussian_filter(rng.standard_normal((size, size)).astype(np.float32), 8) * 6
    ground = ground.astype(np.float32)
    river = dist < 0.025
    ground[river] = np.minimum(ground[river], 8.0)

    dsm = ground.copy()
    labels = np.zeros((size, size), np.int32)
    heights = [0.0]
    block = max(8, size // 16)
    bid = 0
    for r0 in range(block // 2, size - block, block):
        for c0 in range(block // 2, size - block, block):
            cc = (c0 + block / 2) / size
            rr = (r0 + block / 2) / size
            rx = 0.38 + 0.06 * np.sin(rr * 2 * np.pi * 1.2)
            if abs(cc - rx) < 0.08 or rng.random() < 0.25:
                continue  # keep the river corridor and some parks open
            bh = float(rng.choice([6.0, 9.0, 12.0, 18.0, 30.0], p=[0.3, 0.3, 0.2, 0.15, 0.05]))
            h0 = int(block * rng.uniform(0.45, 0.7))
            w0 = int(block * rng.uniform(0.45, 0.7))
            rs, cs = r0 + 1, c0 + 1
            bid += 1
            footprint = (slice(rs, rs + h0), slice(cs, cs + w0))
            base = float(ground[footprint].mean())
            dsm[footprint] = base + bh
            labels[footprint] = bid
            heights.append(bh)

    # Colour: water blue, grass green on slopes, grey roofs, beige ground.
    rgb = np.zeros((size, size, 3), np.float32)
    rgb[...] = (172, 160, 130)
    grass = ndi.gaussian_filter((ground > 18).astype(np.float32), 2)
    rgb = rgb * (1 - grass[..., None]) + np.array([86, 130, 70]) * grass[..., None]
    rgb[river] = (52, 92, 140)
    roof_tone = 120 + 60 * ((labels * 37) % 7) / 6.0
    roofs = labels > 0
    rgb[roofs] = np.stack([roof_tone, roof_tone * 0.95, roof_tone * 0.9], -1)[roofs]
    rgb += rng.normal(0, 6, rgb.shape)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    return {
        "dsm": dsm.astype(np.float32),
        "ground": ground,
        "rgb": rgb,
        "labels": labels,
        "heights": np.asarray(heights, np.float32),
        "gsd_m": np.float32(gsd_m),
        "river": river,
    }


def relative_from_dsm(dsm: np.ndarray) -> np.ndarray:
    """Mimic what the depth model hands us: an un-scaled 0..1 version of the surface."""
    lo, hi = np.percentile(dsm, [2, 98])
    return np.clip((dsm - lo) / max(hi - lo, 1e-6), 0, 1).astype(np.float32)


def tilted(rel: np.ndarray, amount: float = 0.25) -> Tuple[np.ndarray, np.ndarray]:
    """Add the top-to-bottom perspective tilt monocular models hallucinate on nadir images."""
    ramp = np.linspace(0, amount, rel.shape[0], dtype=np.float32)[:, None]
    return rel + ramp, ramp
