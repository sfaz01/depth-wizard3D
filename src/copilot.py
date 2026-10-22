"""
copilot.py - Natural-language control of the 3D twin.

Design rule: the language model only *chooses actions*. Every number in a reply comes from
src/analytics.py, so a small free model cannot hallucinate flood depths or head counts.

Backends (all free):
    1. Any OpenAI-compatible chat endpoint. Default is a local Ollama server
       (http://localhost:11434/v1, model qwen2.5:3b - fits next to the depth model on a 6 GB
       GPU). Groq / OpenRouter free tiers also work via DW_LLM_BASE_URL + DW_LLM_API_KEY.
    2. A deterministic rule parser that always works offline, used when no LLM answers.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src import analytics as A
from src.scene import load_grids, read_json, write_json

LLM_BASE_URL = os.environ.get("DW_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("DW_LLM_MODEL", "qwen2.5:3b")
LLM_API_KEY = os.environ.get("DW_LLM_API_KEY", "")
LLM_TIMEOUT = float(os.environ.get("DW_LLM_TIMEOUT", "25"))

ACTION_DOC = """Available actions (JSON objects with a "type" field):
- {"type":"flood","rise_m":<number>}      water rises this many metres above the lowest ground (river level)
- {"type":"flood","level_m":<number>}     water surface at an absolute elevation in metres
  optional "source": "lowest" (river, default) | "edges" (regional/storm surge) | "rain" (ponding everywhere)
- {"type":"flood_clear"}                  remove the water
- {"type":"safe_zones"}                   find evacuation assembly points for the current water level
- {"type":"highlight_buildings","min_height_m":<n>,"max_height_m":<n>,"top_n":<n>}  any subset of fields
- {"type":"fly_to","target":"tallest"|<building id>}
- {"type":"sun","hour":<0-24>,"month":<1-12>}   move the sun / shadows
- {"type":"solar"}                        rooftop solar potential for the scene
- {"type":"view","mode":"rgb"|"slope"|"heights"|"wire"}
- {"type":"report"}                       summary of the scene
"""

SYSTEM_PROMPT = (
    "You control a 3D digital twin of a city built from one satellite image, used for disaster "
    "response and urban planning. Convert the user's request into actions.\n"
    + ACTION_DOC
    + 'Reply ONLY with JSON: {"actions":[...]} . Use several actions if needed. '
    "If nothing fits, return {\"actions\":[{\"type\":\"report\"}]}."
)

_llm_state = {"checked": 0.0, "ok": False}


# --------------------------------------------------------------------------------------
# Intent parsing
# --------------------------------------------------------------------------------------

_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_rules(message: str) -> List[Dict]:
    m = message.lower()
    actions: List[Dict] = []

    def num_after(pattern: str) -> Optional[float]:
        r = re.search(pattern, m)
        return float(r.group(1)) if r else None

    if re.search(r"\b(clear|remove|drain|no)\b.*\b(water|flood)", m):
        actions.append({"type": "flood_clear"})
    elif re.search(r"flood|water|rain|surge|inundat|submerge", m):
        a: Dict = {"type": "flood"}
        lvl = num_after(_NUM + r"\s*m(?:etres|eters)?\s*(?:asl|above sea|elevation)") or \
            num_after(r"(?:level|elevation)\s*(?:of|to|=|at)?\s*" + _NUM)
        rise = num_after(_NUM + r"\s*(?:m\b|metre|meter)")
        if lvl is not None:
            a["level_m"] = lvl
        elif rise is not None:
            a["rise_m"] = rise
        if re.search(r"surge|coast|sea|regional|everywhere around", m):
            a["source"] = "edges"
        elif re.search(r"\brain|cloudburst|pond|waterlog", m):
            a["source"] = "rain"
        actions.append(a)

    if re.search(r"evacuat|safe|shelter|escape|refuge|assembly", m):
        actions.append({"type": "safe_zones"})

    if re.search(r"solar|panel|rooftop energy|pv\b|renewable", m):
        actions.append({"type": "solar"})

    hl: Dict = {"type": "highlight_buildings"}
    v = num_after(r"(?:over|above|taller than|more than|higher than|>)\s*" + _NUM)
    if v is not None and re.search(r"build|tower|struct|tall|high", m):
        hl["min_height_m"] = v
    v = num_after(r"(?:under|below|shorter than|less than|<)\s*" + _NUM)
    if v is not None and re.search(r"build|struct|short|low", m):
        hl["max_height_m"] = v
    v = num_after(r"(?:top|tallest)\s*" + _NUM)
    if v is not None:
        hl["top_n"] = int(v)
    if len(hl) > 1:
        actions.append(hl)

    if re.search(r"\b(fly|go|zoom|take me|show me)\b.*\btallest\b", m) or re.search(r"tallest building", m) and "top" not in m:
        actions.append({"type": "fly_to", "target": "tallest"})

    t = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", m)
    if t or re.search(r"sunrise|sunset|morning|evening|noon|shadow", m):
        hour = 12.0
        if t:
            hour = int(t.group(1)) % 12 + (12 if t.group(3) == "pm" else 0) + (int(t.group(2) or 0) / 60)
        elif "sunrise" in m or "morning" in m:
            hour = 7.0
        elif "sunset" in m or "evening" in m:
            hour = 17.5
        sun: Dict = {"type": "sun", "hour": hour}
        for i, name in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]):
            if re.search(r"\b" + name, m):
                sun["month"] = i + 1
        if "winter" in m:
            sun["month"] = 12
        if "summer" in m:
            sun["month"] = 6
        actions.append(sun)

    if re.search(r"slope|landslide|steep", m):
        actions.append({"type": "view", "mode": "slope"})
    elif re.search(r"height map|heights|colou?r by height", m):
        actions.append({"type": "view", "mode": "heights"})
    elif re.search(r"wireframe|mesh", m):
        actions.append({"type": "view", "mode": "wire"})

    if not actions or re.search(r"summary|report|overview|how many|stats|statistics|describe", m):
        actions.append({"type": "report"})
    return actions


def _llm_available() -> bool:
    if os.environ.get("DW_LLM", "1") == "0":
        return False
    now = time.time()
    if now - _llm_state["checked"] < 60:
        return _llm_state["ok"]
    import requests

    try:
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
        r = requests.get(f"{LLM_BASE_URL}/models", headers=headers, timeout=1.5)
        _llm_state["ok"] = r.ok
    except Exception:
        _llm_state["ok"] = False
    _llm_state["checked"] = now
    return _llm_state["ok"]


def parse_llm(message: str, context: Dict) -> Optional[List[Dict]]:
    if not _llm_available():
        return None
    import requests

    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    body = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Scene context: {json.dumps(context)}\nRequest: {message}"},
        ],
    }
    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions", json=body, headers=headers, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        start, end = text.find("{"), text.rfind("}")
        acts = json.loads(text[start:end + 1]).get("actions", [])
        valid = [a for a in acts if isinstance(a, dict) and a.get("type") in VALID_TYPES]
        return valid or None
    except Exception as e:
        print(f"[copilot] LLM parse failed, using rules: {e}")
        return None


VALID_TYPES = {"flood", "flood_clear", "safe_zones", "highlight_buildings", "fly_to", "sun", "solar",
               "view", "report"}


# --------------------------------------------------------------------------------------
# Execution (all numbers come from analytics)
# --------------------------------------------------------------------------------------

def _fmt_area(m2: float) -> str:
    return f"{m2 / 10000:.2f} ha" if m2 >= 10000 else f"{m2:,.0f} m²"


def resolve_level(action: Dict, ground_min: float, current_level: Optional[float]) -> float:
    if "level_m" in action:
        return float(action["level_m"])
    if "rise_m" in action:
        return ground_min + float(action["rise_m"])
    return current_level if current_level is not None else ground_min + 2.0


def solar_for_job(job_dir: Path) -> Dict:
    cache = job_dir / "solar.json"
    cached = read_json(cache)
    if cached:
        return cached
    m, dsm, _, labels, buildings = load_grids(job_dir)
    scene = m.get("scene") or {}
    lat = scene.get("lat") if scene.get("lat") is not None else 20.0
    lon = scene.get("lon") if scene.get("lon") is not None else 78.0
    # Solar ray-marching is the costliest analytic; run it at <=256 px.
    step = max(1, int(round(max(dsm.shape) / 256)))
    res = A.solar_potential(dsm[::step, ::step], float(m["gsd_m"]) * step, lat, lon,
                            labels[::step, ::step], buildings)
    # Areas were counted on the subsampled grid at the coarser gsd, so totals stay comparable.
    res["lat"], res["lon"] = lat, lon
    write_json(cache, res)
    return res


def execute(job_dir: Path, actions: List[Dict], state: Optional[Dict] = None) -> Tuple[str, List[Dict]]:
    state = state or {}
    m, dsm, ground, labels, buildings = load_grids(job_dir)
    gsd = float(m["gsd_m"])
    gmin = float(read_json(job_dir / "analysis.json", {}).get("ground_min_m", float(ground.min())))
    level = state.get("flood_level_m")
    source = state.get("flood_source", "lowest")
    lines: List[str] = []
    out: List[Dict] = []

    for a in actions:
        t = a.get("type")
        if t == "flood":
            level = resolve_level(a, gmin, level)
            source = a.get("source", source)
            rep = A.flood_report(dsm, ground, gsd, level, labels, buildings, source=source)
            sev = rep["severity_counts"]
            lines.append(
                f"Water at {level:.1f} m ({level - gmin:+.1f} m above the river bed) floods "
                f"{_fmt_area(rep['flooded_area_m2'])} ({rep['flooded_fraction'] * 100:.1f}% of the scene), "
                f"max depth {rep['max_depth_m']:.1f} m. {rep['buildings_affected']} buildings reached "
                f"(≈{rep['est_people_affected']:,} people) — {sev['critical']} critical, {sev['severe']} severe, "
                f"{sev['moderate']} moderate."
            )
            out.append({"type": "flood", "level_m": round(level, 2), "source": source,
                        "affected_ids": rep.get("affected_ids", [])})
        elif t == "flood_clear":
            level = None
            lines.append("Water cleared.")
            out.append({"type": "flood_clear"})
        elif t == "safe_zones":
            lv = level if level is not None else gmin + 2.0
            zones = A.safe_zones(dsm, ground, gsd, lv, labels, buildings, source=source)
            if zones:
                best = zones[0]
                lines.append(
                    f"{len(zones)} evacuation points for water at {lv:.1f} m. Best open ground: "
                    f"{_fmt_area(best['area_m2'])} with {best['freeboard_m']:.1f} m freeboard."
                )
            else:
                lines.append(f"No dry assembly area large enough at {lv:.1f} m — use vertical evacuation.")
            out.append({"type": "safe_zones", "level_m": round(lv, 2), "zones": zones})
        elif t == "highlight_buildings":
            sel = buildings
            if a.get("min_height_m") is not None:
                sel = [b for b in sel if b.height_m >= float(a["min_height_m"])]
            if a.get("max_height_m") is not None:
                sel = [b for b in sel if b.height_m <= float(a["max_height_m"])]
            if a.get("top_n"):
                sel = sorted(sel, key=lambda b: -b.height_m)[: int(a["top_n"])]
            occ = sum(b.est_occupants for b in sel)
            crit = []
            if a.get("min_height_m") is not None:
                crit.append(f"≥ {float(a['min_height_m']):g} m")
            if a.get("max_height_m") is not None:
                crit.append(f"≤ {float(a['max_height_m']):g} m")
            if a.get("top_n"):
                crit.append(f"top {int(a['top_n'])}")
            lines.append(f"{len(sel)} buildings {' '.join(crit) or 'selected'} (≈{occ:,} occupants).")
            out.append({"type": "highlight_buildings", "ids": [b.id for b in sel]})
        elif t == "fly_to":
            target = a.get("target", "tallest")
            b = buildings[0] if (target == "tallest" and buildings) else next(
                (x for x in buildings if str(x.id) == str(target)), None)
            if b:
                lines.append(f"Building #{b.id}: {b.height_m:.0f} m tall, ~{b.floors} floors, "
                             f"{b.footprint_m2:,.0f} m² footprint.")
                out.append({"type": "fly_to", "building_id": b.id, "row": b.row, "col": b.col})
            else:
                lines.append("No such building.")
        elif t == "sun":
            out.append({"type": "sun", "hour": float(a.get("hour", 12)), "month": int(a.get("month", 6))})
            lines.append(f"Sun set to {_fmt_hour(float(a.get('hour', 12)))} in month {int(a.get('month', 6))}.")
        elif t == "solar":
            res = solar_for_job(job_dir)
            top = res["buildings"][:1]
            extra = f" Best roof: #{top[0]['id']} ({top[0]['pv_kwh_yr']:,.0f} kWh/yr)." if top else ""
            lines.append(
                f"Rooftop solar potential ≈ {res['total_kwh_yr'] / 1000:,.0f} MWh/yr, enough for "
                f"≈{res['households_powered']:,} households (shadow-aware, {res['scene_mean_kwh_m2_yr']:.0f} "
                f"kWh/m²/yr mean insolation).{extra}"
            )
            out.append({"type": "solar", "buildings": res["buildings"]})
        elif t == "view":
            out.append({"type": "view", "mode": a.get("mode", "rgb")})
            lines.append(f"Switched to {a.get('mode', 'rgb')} view.")
        elif t == "report":
            an = read_json(job_dir / "analysis.json", {})
            bs = an.get("buildings", {})
            tr = an.get("terrain", {})
            lines.append(
                f"{bs.get('count', 0)} buildings, tallest {bs.get('tallest_m', 0):.0f} m, mean "
                f"{bs.get('mean_height_m', 0):.0f} m, ≈{bs.get('est_occupants', 0):,} occupants. Ground spans "
                f"{an.get('ground_min_m', 0):.0f}–{an.get('ground_max_m', 0):.0f} m; "
                f"{tr.get('hazard_gt30', 0) * 100:.1f}% of terrain is steeper than 30°."
            )
            out.append({"type": "report"})
    return " ".join(lines), out


def _fmt_hour(h: float) -> str:
    hh, mm = int(h), int(round((h % 1) * 60))
    return f"{hh:02d}:{mm:02d}"


def answer(job_dir: Path, message: str, state: Optional[Dict] = None) -> Dict:
    an = read_json(job_dir / "analysis.json", {})
    context = {"buildings": an.get("buildings", {}), "ground_min_m": an.get("ground_min_m"),
               "ground_max_m": an.get("ground_max_m"), "current_flood_level_m": (state or {}).get("flood_level_m")}
    actions = parse_llm(message, context)
    engine = f"llm:{LLM_MODEL}" if actions else "rules"
    if not actions:
        actions = parse_rules(message)
    reply, ui_actions = execute(job_dir, actions, state)
    return {"reply": reply, "actions": ui_actions, "engine": engine, "parsed": actions}
