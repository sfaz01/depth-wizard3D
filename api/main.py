"""
main.py - DepthWizard API + static viewer on one port.

    uvicorn api.main:app --host 0.0.0.0 --port 8000

Heavy work (depth inference, tile download) runs on the background JobManager; the
browser polls /api/jobs/{id} for live progress. Analytics endpoints are synchronous and
fast because they operate on the cached web-resolution grids.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import sys
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import analytics as A
from src import copilot
from src import scene as S
from src.exporters import write_glb, write_stl
from src.jobs import JOB_ID_RE, JobManager
from src.pipeline import run_demo, run_map, run_upload
from src.tiles import BBox

VERSION = "2.0.0"
JOBS_DIR = Path(os.environ.get("DW_JOBS_DIR", PROJECT_ROOT / "data" / "web_jobs"))
MAX_UPLOAD_MB = float(os.environ.get("DW_MAX_UPLOAD_MB", "60"))
ALLOWED_UPLOADS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
ASSET_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(png|jpg|bin|json)$")

app = FastAPI(title="DepthWizard API", version=VERSION,
              description="Single-image 3D twins for disaster response and smart cities.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs = JobManager(JOBS_DIR)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def job_dir(job_id: str, require_done: bool = True) -> Path:
    if job_id == "latest":
        done = [p for p in JOBS_DIR.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
        if not done:
            raise HTTPException(404, "No processed scenes yet.")
        return max(done, key=lambda p: (p / "manifest.json").stat().st_mtime)
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(400, "Invalid job id.")
    d = JOBS_DIR / job_id
    if not d.is_dir() or (require_done and not (d / "manifest.json").exists()):
        raise HTTPException(404, f"Scene '{job_id}' not found or not finished.")
    return d


def safe_filename(name: str) -> str:
    base = Path(name).name  # strips any directory components, incl. "../"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(base).stem)[:60] or "upload"
    return stem + Path(base).suffix.lower()


def lan_addresses() -> List[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


# --------------------------------------------------------------------------------------
# scene creation
# --------------------------------------------------------------------------------------

class MapRequest(BaseModel):
    west: float
    south: float
    east: float
    north: float
    name: str = ""


@app.post("/api/scene/map")
def create_map_scene(req: MapRequest):
    bbox = BBox(req.west, req.south, req.east, req.north)
    try:
        bbox.validate()
    except ValueError as e:
        raise HTTPException(400, str(e))
    jid = jobs.submit(run_map(bbox, req.name[:80]), title=req.name or "Map scene")
    return {"job_id": jid, "status": jobs.status(jid)}


@app.post("/api/process")
def create_upload_scene(
    file: UploadFile = File(...),
    name: str = Form(""),
    gsd_m: Optional[float] = Form(None),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
):
    if not file.filename:
        raise HTTPException(400, "No filename provided.")
    fname = safe_filename(file.filename)
    if Path(fname).suffix not in ALLOWED_UPLOADS:
        raise HTTPException(400, f"Unsupported format. Use one of: {', '.join(sorted(ALLOWED_UPLOADS))}")
    if gsd_m is not None and not (0.02 <= gsd_m <= 30):
        raise HTTPException(400, "gsd_m must be between 0.02 and 30 metres/pixel.")

    from src.jobs import new_job_id

    jid = new_job_id()
    d = JOBS_DIR / jid
    d.mkdir(parents=True)
    dest = d / ("input_" + fname)
    limit = int(MAX_UPLOAD_MB * 1024 * 1024)
    written = 0
    with open(dest, "wb") as out:
        while chunk := file.file.read(1 << 20):
            written += len(chunk)
            if written > limit:
                out.close()
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"File larger than {MAX_UPLOAD_MB:.0f} MB.")
            out.write(chunk)
    if written == 0:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "Uploaded file is empty.")
    jobs.submit(run_upload(dest, name or Path(fname).stem, gsd_m, lat, lon), job_id=jid, title=name or fname)
    return {"job_id": jid, "status": jobs.status(jid)}


@app.post("/api/scene/demo")
def create_demo_scene():
    jid = jobs.run_sync(run_demo(), "job_demo", title="Demo")
    return {"job_id": jid, "status": jobs.status(jid)}


# --------------------------------------------------------------------------------------
# job status & results
# --------------------------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(limit: int = 30):
    out = []
    for d in JOBS_DIR.iterdir():
        if not d.is_dir() or not (d / "manifest.json").exists() or not JOB_ID_RE.match(d.name):
            continue
        m = S.read_json(d / "manifest.json", {})
        scene = m.get("scene") or {}
        out.append({
            "job_id": d.name,
            "name": scene.get("name") or d.name,
            "kind": scene.get("kind", "legacy"),
            "thumb": f"/api/assets/{d.name}/thumb.jpg" if (d / "thumb.jpg").exists() else f"/api/assets/{d.name}/texture.png",
            "updated": (d / "manifest.json").stat().st_mtime,
            "metric": bool(m.get("metric")),
        })
    out.sort(key=lambda j: -j["updated"])
    return out[:limit]


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    try:
        st = jobs.status(job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job id.")
    if st is None:
        raise HTTPException(404, "Unknown job.")
    return st


@app.get("/api/result/{job_id}")
def job_result(job_id: str):
    d = job_dir(job_id)
    m = S.read_json(d / "manifest.json")
    jid = d.name

    def asset(name: str) -> Optional[str]:
        return f"/api/assets/{jid}/{name}" if (d / name).exists() else None

    return {
        "job_id": jid,
        "manifest": m,
        "analysis": S.read_json(d / "analysis.json"),
        "buildings": S.read_json(d / "buildings.json", []),
        "assets": {
            "texture": asset("texture.png"),
            "heightmap": asset("heightmap.png"),
            "heightmap_bin": asset("heightmap.bin"),
            "heightmap_sharp_bin": asset("heightmap_sharp.bin") or asset("heightmap.bin"),
            "ground_bin": asset("ground.bin"),
            "labels_bin": asset("labels.bin"),
            "error_map": asset("error_map.png"),
            "thumb": asset("thumb.jpg"),
        },
    }


@app.get("/api/assets/{job_id}/{filename}")
def job_asset(job_id: str, filename: str):
    if not ASSET_RE.match(filename):
        raise HTTPException(400, "Invalid asset name.")
    path = job_dir(job_id, require_done=False) / filename
    if not path.is_file():
        raise HTTPException(404, "Asset not found.")
    media = {"png": "image/png", "jpg": "image/jpeg", "json": "application/json"}.get(
        filename.rsplit(".", 1)[-1], "application/octet-stream")
    return FileResponse(path, media_type=media)


# --------------------------------------------------------------------------------------
# scale calibration
# --------------------------------------------------------------------------------------

class ScaleRequest(BaseModel):
    height_ref_m: Optional[float] = Field(None, gt=0.5, lt=2000)
    terrain_relief_m: Optional[float] = Field(None, ge=0, lt=3000)
    gsd_m: Optional[float] = Field(None, gt=0.01, lt=50)


@app.post("/api/jobs/{job_id}/scale")
def rescale(job_id: str, req: ScaleRequest):
    d = job_dir(job_id)
    if (d / "dsm.npy").exists():
        raise HTTPException(400, "This scene has exact synthetic heights; scale is fixed.")
    source = S.read_json(d / "source.json", {})
    scale = S.read_json(d / "scale.json") or S.default_scale(source)
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "Nothing to change.")
    scale.update(changes, method="user", note="Calibrated by user.")
    S.write_json(d / "scale.json", scale)
    (d / "solar.json").unlink(missing_ok=True)
    S.build_assets(d)
    return job_result(d.name)


# --------------------------------------------------------------------------------------
# analytics
# --------------------------------------------------------------------------------------

class FloodRequest(BaseModel):
    level_m: float
    source: str = "lowest"  # lowest | edges | rain | point
    point: Optional[List[int]] = None  # [row, col] in web grid when source == "point"


def _flood_source(req: FloodRequest):
    if req.source == "point":
        if not req.point or len(req.point) != 2:
            raise HTTPException(400, "point=[row, col] required for source=point")
        return (int(req.point[0]), int(req.point[1]))
    if req.source not in ("lowest", "edges", "rain"):
        raise HTTPException(400, "source must be lowest, edges, rain or point")
    return req.source


@app.post("/api/jobs/{job_id}/flood")
def flood(job_id: str, req: FloodRequest):
    m, dsm, ground, labels, buildings = S.load_grids(job_dir(job_id))
    return A.flood_report(dsm, ground, float(m["gsd_m"]), req.level_m, labels, buildings,
                          source=_flood_source(req), max_listed=200)


@app.post("/api/jobs/{job_id}/safe_zones")
def safe_zones(job_id: str, req: FloodRequest):
    m, dsm, ground, labels, buildings = S.load_grids(job_dir(job_id))
    return {"level_m": req.level_m,
            "zones": A.safe_zones(dsm, ground, float(m["gsd_m"]), req.level_m, labels, buildings,
                                  source=_flood_source(req))}


@app.get("/api/jobs/{job_id}/solar")
def solar(job_id: str):
    return copilot.solar_for_job(job_dir(job_id))


class CopilotRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    state: dict = {}


@app.post("/api/jobs/{job_id}/copilot")
def ask_copilot(job_id: str, req: CopilotRequest):
    return copilot.answer(job_dir(job_id), req.message, req.state)


# --------------------------------------------------------------------------------------
# exports
# --------------------------------------------------------------------------------------

def _grids_and_rgb(job_id: str):
    from PIL import Image
    import numpy as np

    d = job_dir(job_id)
    m, dsm, *_ = S.load_grids(d)
    rgb = np.asarray(Image.open(d / "texture.png").convert("RGB"))
    return d, m, dsm, rgb


@app.get("/api/jobs/{job_id}/model.glb")
def export_glb(job_id: str, exaggeration: float = Query(1.5, ge=0.5, le=6),
               size: str = Query("table", pattern="^(table|real)$")):
    d, m, dsm, rgb = _grids_and_rgb(job_id)
    data = write_glb(dsm, rgb, float(m["gsd_m"]), exaggeration, size_m=0.6 if size == "table" else None)
    return Response(data, media_type="model/gltf-binary",
                    headers={"Content-Disposition": f'inline; filename="{d.name}.glb"'})


@app.get("/api/jobs/{job_id}/model.stl")
def export_stl(job_id: str, exaggeration: float = Query(2.0, ge=0.5, le=8),
               size_mm: float = Query(150, ge=30, le=400)):
    d, m, dsm, _ = _grids_and_rgb(job_id)
    data = write_stl(dsm, float(m["gsd_m"]), exaggeration, size_mm)
    return Response(data, media_type="model/stl",
                    headers={"Content-Disposition": f'attachment; filename="{d.name}.stl"'})


@app.get("/api/jobs/{job_id}/buildings.geojson")
def export_geojson(job_id: str):
    """Building points (and footprint bboxes) in lon/lat for GIS tools, when the scene is geolocated."""
    d = job_dir(job_id)
    m = S.read_json(d / "manifest.json")
    bbox = (m.get("scene") or {}).get("bbox")
    w, h = m["width"], m["height"]

    def to_lonlat(r: float, c: float):
        if not bbox:
            return [c * m["gsd_m"], -r * m["gsd_m"]]  # local metres
        import math

        lon = bbox["west"] + (bbox["east"] - bbox["west"]) * (c / w)
        # Linear in Mercator y, not latitude.
        def merc(lat):
            return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

        y = merc(bbox["north"]) + (merc(bbox["south"]) - merc(bbox["north"])) * (r / h)
        lat = math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)
        return [round(lon, 7), round(lat, 7)]

    feats = []
    for b in S.read_json(d / "buildings.json", []):
        r0, c0, r1, c1 = b["bbox"]
        ring = [to_lonlat(r0, c0), to_lonlat(r0, c1), to_lonlat(r1, c1), to_lonlat(r1, c0), to_lonlat(r0, c0)]
        props = {k: b[k] for k in ("id", "height_m", "footprint_m2", "floors", "volume_m3", "est_occupants")}
        feats.append({"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"type": "FeatureCollection", "features": feats,
            "properties": {"crs": "EPSG:4326" if bbox else "local-metres", "note": "Footprints are bounding boxes."}}


# --------------------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "version": VERSION,
        "model_size": os.environ.get("DW_MODEL_SIZE", "small"),
        "llm": {"base_url": copilot.LLM_BASE_URL, "model": copilot.LLM_MODEL, "available": copilot._llm_available()},
    }


@app.get("/api/server-info")
def server_info():
    port = int(os.environ.get("DW_PORT", "8000"))
    public = os.environ.get("DW_PUBLIC_URL", "").rstrip("/")
    urls = ([public] if public else []) + [f"http://{ip}:{port}" for ip in lan_addresses()]
    return {"urls": urls, "port": port}


def ensure_demo_scene() -> None:
    if not (JOBS_DIR / "job_demo" / "manifest.json").exists() or \
            S.read_json(JOBS_DIR / "job_demo" / "manifest.json", {}).get("version") != 2:
        print("[DepthWizard] Building offline demo scene...")
        jobs.run_sync(run_demo(), "job_demo", title="Demo")


ensure_demo_scene()

app.mount("/", StaticFiles(directory=str(PROJECT_ROOT / "web"), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("DW_PORT", "8000"))
    print("=" * 64)
    print(f"DepthWizard {VERSION}  ->  http://localhost:{port}")
    for ip in lan_addresses():
        print(f"  on your network (phones / AR): http://{ip}:{port}")
    print("=" * 64)
    uvicorn.run("api.main:app", host=os.environ.get("DW_HOST", "0.0.0.0"), port=port, reload=False)
