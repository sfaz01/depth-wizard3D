"""
exporters.py - Dependency-free GLB (glTF 2.0 binary) and STL writers for a heightfield.

GLB  -> phones: <model-viewer> renders it and hands it to Android Scene Viewer / iOS Quick
        Look for AR, so judges can put the city on the table.
STL  -> 3D printers: a closed, watertight solid (top surface, four walls, flat base).
"""

from __future__ import annotations

import io
import json
import struct
from typing import Optional, Tuple

import numpy as np
from PIL import Image


def _resample(dsm: np.ndarray, max_dim: int) -> Tuple[np.ndarray, float]:
    h, w = dsm.shape
    f = min(1.0, max_dim / max(h, w))
    if f >= 1.0:
        return dsm.astype(np.float32), 1.0
    nw, nh = max(2, int(round(w * f))), max(2, int(round(h * f)))
    img = Image.fromarray(dsm.astype(np.float32)).resize((nw, nh), Image.Resampling.BILINEAR)
    return np.asarray(img, np.float32), w / nw


def _grid(dsm: np.ndarray, gsd_m: float, exaggeration: float, size_m: Optional[float]):
    """Vertex positions (x east, y up, z south), centred, optionally scaled to size_m."""
    h, w = dsm.shape
    xs = (np.arange(w) - (w - 1) / 2) * gsd_m
    zs = (np.arange(h) - (h - 1) / 2) * gsd_m
    X, Z = np.meshgrid(xs, zs)
    Y = (dsm - dsm.min()) * exaggeration
    k = 1.0
    if size_m:
        k = size_m / max(np.ptp(xs), np.ptp(zs), 1e-6)
    return np.stack([X * k, Y * k, Z * k], -1).astype(np.float32), k


def _grid_indices(h: int, w: int) -> np.ndarray:
    r, c = np.meshgrid(np.arange(h - 1), np.arange(w - 1), indexing="ij")
    a = r * w + c
    b = a + 1
    d = a + w
    e = d + 1
    # Counter-clockwise when seen from above (+y) with z pointing south.
    return np.stack([a, d, b, b, d, e], -1).reshape(-1, 3).astype(np.uint32)


def write_glb(dsm: np.ndarray, rgb: np.ndarray, gsd_m: float, exaggeration: float = 1.5,
              size_m: Optional[float] = 0.6, max_dim: int = 256) -> bytes:
    """Textured terrain mesh. size_m=0.6 gives a 60 cm tabletop model; None = real scale."""
    dsm_s, _ = _resample(np.asarray(dsm, np.float32), max_dim)
    h, w = dsm_s.shape
    pos, _ = _grid(dsm_s, gsd_m * dsm.shape[1] / w, exaggeration, size_m)
    pos = pos.reshape(-1, 3)
    u, v = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    uv = np.stack([u, v], -1).reshape(-1, 2).astype(np.float32)
    idx = _grid_indices(h, w).ravel()

    tex = Image.fromarray(np.asarray(rgb, np.uint8)).convert("RGB")
    tex.thumbnail((1024, 1024))
    buf = io.BytesIO()
    tex.save(buf, format="JPEG", quality=88)
    jpg = buf.getvalue()

    chunks = []
    offset = 0

    def add(data: bytes) -> Tuple[int, int]:
        nonlocal offset
        start = offset
        chunks.append(data)
        pad = (-len(data)) % 4
        chunks.append(b"\x00" * pad)
        offset += len(data) + pad
        return start, len(data)

    p_off, p_len = add(pos.tobytes())
    t_off, t_len = add(uv.tobytes())
    i_off, i_len = add(idx.astype(np.uint32).tobytes())
    im_off, im_len = add(jpg)
    binary = b"".join(chunks)

    gltf = {
        "asset": {"version": "2.0", "generator": "DepthWizard"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "terrain"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0.0,
                                                "roughnessFactor": 0.9}, "doubleSided": True}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": 3, "mimeType": "image/jpeg"}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": p_off, "byteLength": p_len, "target": 34962},
            {"buffer": 0, "byteOffset": t_off, "byteLength": t_len, "target": 34962},
            {"buffer": 0, "byteOffset": i_off, "byteLength": i_len, "target": 34963},
            {"buffer": 0, "byteOffset": im_off, "byteLength": im_len},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
             "min": pos.min(0).tolist(), "max": pos.max(0).tolist()},
            {"bufferView": 1, "componentType": 5126, "count": len(uv), "type": "VEC2"},
            {"bufferView": 2, "componentType": 5125, "count": int(idx.size), "type": "SCALAR"},
        ],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    out = io.BytesIO()
    out.write(struct.pack("<4sII", b"glTF", 2, total))
    out.write(struct.pack("<I4s", len(js), b"JSON"))
    out.write(js)
    out.write(struct.pack("<I4s", len(binary), b"BIN\x00"))
    out.write(binary)
    return out.getvalue()


def write_stl(dsm: np.ndarray, gsd_m: float, exaggeration: float = 2.0, size_mm: float = 150.0,
              base_mm: float = 5.0, max_dim: int = 300) -> bytes:
    """Watertight binary STL in millimetres, longest side = size_mm, with a solid base."""
    dsm_s, _ = _resample(np.asarray(dsm, np.float32), max_dim)
    h, w = dsm_s.shape
    pos, _ = _grid(dsm_s, gsd_m * dsm.shape[1] / w, exaggeration, size_mm)
    # STL convention: z up. Map (x east, y up, z south) -> (x, -z, y + base).
    top = np.stack([pos[..., 0], -pos[..., 2], pos[..., 1] + base_mm], -1)
    bottom_z = 0.0

    tris = []
    v = top.reshape(-1, 3)
    for a, b, c in _grid_indices(h, w).reshape(-1, 3):
        tris.append((v[a], v[b], v[c]))

    def wall(edge: np.ndarray):
        for i in range(len(edge) - 1):
            p, q = edge[i], edge[i + 1]
            pb, qb = p.copy(), q.copy()
            pb[2] = qb[2] = bottom_z
            tris.append((p, q, qb))
            tris.append((p, qb, pb))

    # Perimeter walked so that walls face outward.
    perimeter = np.concatenate([top[0, :], top[1:, -1], top[-1, -2::-1], top[-2:0:-1, 0]])
    wall(np.concatenate([perimeter, perimeter[:1]]))

    centre = np.array([0.0, 0.0, bottom_z], np.float32)
    ring = np.concatenate([perimeter, perimeter[:1]]).copy()
    ring[:, 2] = bottom_z
    for i in range(len(ring) - 1):
        tris.append((centre, ring[i], ring[i + 1]))

    arr = np.asarray(tris, np.float32)  # (N, 3, 3)
    n = np.cross(arr[:, 1] - arr[:, 0], arr[:, 2] - arr[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(arr), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["n"], rec["v"] = n, arr
    header = b"DepthWizard terrain".ljust(80, b" ")
    return header + struct.pack("<I", len(arr)) + rec.tobytes()
