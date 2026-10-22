"""API smoke tests (offline: demo scene, no depth model, no network LLM)."""

import io
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["DW_JOBS_DIR"] = str(tmp_path_factory.mktemp("jobs"))
    os.environ["DW_LLM"] = "0"
    from fastapi.testclient import TestClient

    import api.main as main

    return TestClient(main.app)


def test_viewer_and_health(client):
    assert client.get("/").status_code == 200
    h = client.get("/api/health").json()
    assert h["version"].startswith("2.")


def test_demo_result_has_analytics(client):
    r = client.get("/api/result/job_demo").json()
    m = r["manifest"]
    assert m["metric"] and m["width"] > 0
    assert r["analysis"]["buildings"]["count"] > 50
    for key in ("heightmap_bin", "ground_bin", "labels_bin", "texture"):
        assert client.get(r["assets"][key]).status_code == 200
    assert client.get("/api/result/latest").status_code == 200
    assert any(j["job_id"] == "job_demo" for j in client.get("/api/jobs").json())


def test_flood_and_safe_zones(client):
    rep = client.post("/api/jobs/job_demo/flood", json={"level_m": 13.0}).json()
    assert rep["buildings_affected"] > 0 and rep["flooded_area_m2"] > 0
    pt = client.post("/api/jobs/job_demo/flood", json={"level_m": 13.0, "source": "point", "point": [5, 5]})
    assert pt.status_code == 200
    z = client.post("/api/jobs/job_demo/safe_zones", json={"level_m": 13.0}).json()
    assert z["zones"]


def test_copilot_and_exports(client):
    res = client.post("/api/jobs/job_demo/copilot", json={"message": "flood by 4 m"}).json()
    assert res["actions"][0]["type"] == "flood"
    glb = client.get("/api/jobs/job_demo/model.glb")
    assert glb.status_code == 200 and glb.content[:4] == b"glTF"
    stl = client.get("/api/jobs/job_demo/model.stl?size_mm=80")
    assert stl.status_code == 200 and len(stl.content) > 84
    gj = client.get("/api/jobs/job_demo/buildings.geojson").json()
    assert gj["features"]


def test_rejects_traversal_and_bad_ids(client):
    assert client.get("/api/result/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/api/assets/job_demo/..%2Fmanifest.json").status_code in (400, 404)
    assert client.get("/api/assets/job_demo/rel.npy").status_code == 400
    assert client.get("/api/jobs/not-a-job").status_code == 400


def test_upload_sanitises_filename_and_queues(client, monkeypatch):
    import api.main as main

    seen = {}

    def fake_run_upload(path, *a, **k):
        seen["path"] = path
        return lambda job_dir, progress: None

    monkeypatch.setattr(main, "run_upload", fake_run_upload)
    files = {"file": ("../../evil name.png", io.BytesIO(b"\x89PNG fake"), "image/png")}
    r = client.post("/api/process", files=files)
    assert r.status_code == 200
    jid = r.json()["job_id"]
    p = seen["path"]
    assert p.parent == main.JOBS_DIR / jid and p.name == "input_evil_name.png"
    for _ in range(50):
        if client.get(f"/api/jobs/{jid}").json()["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert client.get(f"/api/jobs/{jid}").json()["state"] == "done"


def test_rejects_bad_bbox(client):
    r = client.post("/api/scene/map", json={"west": 0, "south": 0, "east": 1, "north": 1})
    assert r.status_code == 400
