"""The browser flood fill and sun maths must match the Python analytics exactly."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import analytics as A
from src.synthetic import make_river_town

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_flood_and_sun_parity(tmp_path):
    t = make_river_town(128, gsd_m=2.0)
    dsm = t["dsm"].astype(np.float32)
    water = t["river"].astype(np.uint8)
    dsm.tofile(tmp_path / "dsm.bin")
    water.tofile(tmp_path / "water.bin")
    cases = [(lv, src) for lv in (9.0, 12.0, 16.0) for src in ("lowest", "edges", "rain", [20, 100])]
    expected = {}
    for lv, src in cases:
        py_src = tuple(src) if isinstance(src, list) else src
        key = f"{lv}|{json.dumps(src, separators=(',', ':'))}"
        expected[f"{key}|w"] = int(A.flood_mask(dsm, lv, py_src, t["river"]).sum())
        expected[f"{key}|-"] = int(A.flood_mask(dsm, lv, py_src).sum())
    az, el = A.sun_position(19.07, 72.87, datetime(2026, 4, 3, 9, 15, tzinfo=timezone.utc))
    script = tmp_path / "check.mjs"
    script.write_text(f"""
import fs from 'fs';
import {{ floodMask, lowestThreshold }} from '{(ROOT / 'web/js/flood.js').as_posix()}';
import {{ sunPosition }} from '{(ROOT / 'web/js/sun.js').as_posix()}';
const f32 = (p) => {{ const b = fs.readFileSync(p); return new Float32Array(b.buffer, b.byteOffset, b.length / 4); }};
const dsm = f32('{(tmp_path / 'dsm.bin').as_posix()}');
const water = new Uint8Array(fs.readFileSync('{(tmp_path / 'water.bin').as_posix()}'));
const lt = lowestThreshold(dsm);
const out = {{}};
for (const [lv, src] of {json.dumps(cases)}) {{
  for (const [tag, w] of [['w', water], ['-', null]]) {{
    const m = floodMask(dsm, {dsm.shape[1]}, {dsm.shape[0]}, lv, src, lt, w);
    out[`${{lv.toFixed(1)}}|${{JSON.stringify(src)}}|${{tag}}`] = m.reduce((a, b) => a + b, 0);
  }}
}}
out.sun = sunPosition(19.07, 72.87, new Date(Date.UTC(2026, 3, 3, 9, 15)));
console.log(JSON.stringify(out));
""")
    res = json.loads(subprocess.run([NODE, str(script)], capture_output=True, text=True, check=True).stdout)
    for key, val in expected.items():
        assert res[key] == val, (key, res[key], val)
    assert res["sun"]["azimuth"] == pytest.approx(az, abs=1e-6)
    assert res["sun"]["elevation"] == pytest.approx(el, abs=1e-6)
