"""
dw.py - Build DepthWizard scenes from the command line.

Pre-bake scenes before a demo so nothing depends on venue Wi-Fi:

    python dw.py presets                       # every place in web/presets.json
    python dw.py map --lat 26.192 --lon 91.747 --size 1200 --name "Guwahati"
    python dw.py image drone.jpg --gsd 0.3 --name "Campus"
    python dw.py list

Scenes land in data/web_jobs/ and show up under "Recent scenes" in the app.
"""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.jobs import JobManager  # noqa: E402
from src.pipeline import run_map, run_upload  # noqa: E402
from src.scene import read_json  # noqa: E402
from src.tiles import BBox  # noqa: E402

JOBS_DIR = Path(os.environ.get("DW_JOBS_DIR", ROOT / "data" / "web_jobs"))


def square(lat: float, lon: float, size_m: float) -> BBox:
    half = size_m / 2
    dlat = half / 111320
    dlon = half / (111320 * math.cos(math.radians(lat)))
    return BBox(lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def build(jm: JobManager, runner, title: str) -> None:
    t = time.time()
    print(f"-> {title}")
    jid = jm.run_sync(runner, title=title)
    st = jm.status(jid)
    if st["state"] != "done":
        print(f"   FAILED: {st.get('error')}")
        return
    a = read_json(JOBS_DIR / jid / "analysis.json", {})
    sc = read_json(JOBS_DIR / jid / "scale.json", {})
    print(f"   {jid}: {a['buildings']['count']} buildings, scale={sc.get('method')}, "
          f"water={a.get('water_fraction', 0) * 100:.0f}%, {time.time() - t:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="build a scene for a map area")
    m.add_argument("--lat", type=float, required=True)
    m.add_argument("--lon", type=float, required=True)
    m.add_argument("--size", type=float, default=1000, help="square side in metres (50-4000)")
    m.add_argument("--name", default="")
    i = sub.add_parser("image", help="build a scene from an image file")
    i.add_argument("path", type=Path)
    i.add_argument("--gsd", type=float, default=None, help="metres per pixel of the image")
    i.add_argument("--name", default="")
    i.add_argument("--lat", type=float)
    i.add_argument("--lon", type=float)
    sub.add_parser("presets", help="build every place in web/presets.json")
    sub.add_parser("list", help="list built scenes")
    args = ap.parse_args()

    jm = JobManager(JOBS_DIR)
    if args.cmd == "map":
        build(jm, run_map(square(args.lat, args.lon, args.size), args.name), args.name or f"{args.lat},{args.lon}")
    elif args.cmd == "image":
        build(jm, run_upload(args.path.resolve(), args.name, args.gsd, args.lat, args.lon), args.name or args.path.name)
    elif args.cmd == "presets":
        for p in json.loads((ROOT / "web" / "presets.json").read_text()):
            build(jm, run_map(square(p["lat"], p["lon"], p["size"]), p["name"]), p["name"])
    elif args.cmd == "list":
        for d in sorted(JOBS_DIR.iterdir()):
            m = read_json(d / "manifest.json") if d.is_dir() else None
            if m and m.get("version") == 2:
                print(f"{d.name:28s} {(m.get('scene') or {}).get('name', '')}")


if __name__ == "__main__":
    main()
