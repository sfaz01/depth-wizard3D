"""Copilot: offline rule parsing and fact-grounded execution on the demo town."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DW_LLM"] = "0"  # never call a network LLM from tests

from src import copilot as C
from src.jobs import JobManager
from src.pipeline import run_demo


@pytest.fixture(scope="module")
def demo_job(tmp_path_factory):
    jobs = tmp_path_factory.mktemp("jobs")
    jm = JobManager(jobs)
    jid = jm.run_sync(run_demo(), "job_demo")
    return jobs / jid


@pytest.mark.parametrize("msg,expected", [
    ("flood the town by 3 m", {"type": "flood", "rise_m": 3.0}),
    ("raise the water level to 14 m", {"type": "flood", "level_m": 14.0}),
    ("storm surge of 2m", {"type": "flood", "rise_m": 2.0, "source": "edges"}),
    ("show buildings taller than 20 m", {"type": "highlight_buildings", "min_height_m": 20.0}),
    ("where can people evacuate?", {"type": "safe_zones"}),
    ("shadows at 5 pm in december", {"type": "sun", "hour": 17.0, "month": 12}),
    ("how much rooftop solar can we install", {"type": "solar"}),
    ("highlight landslide risk", {"type": "view", "mode": "slope"}),
])
def test_rule_parser(msg, expected):
    acts = C.parse_rules(msg)
    match = [a for a in acts if a["type"] == expected["type"]]
    assert match, acts
    for k, v in expected.items():
        assert match[0].get(k) == v, (msg, match[0])


def test_answer_flood_is_grounded(demo_job):
    res = C.answer(demo_job, "flood by 5 m")
    assert res["engine"] == "rules"
    flood = [a for a in res["actions"] if a["type"] == "flood"][0]
    assert flood["level_m"] == pytest.approx(13.0, abs=0.01)  # demo river bed is at 8 m
    assert "reached" in res["reply"]
    assert len(flood["affected_ids"]) > 0


def test_answer_combo_and_solar(demo_job):
    res = C.answer(demo_job, "flood 4m and show me safe evacuation points")
    types = [a["type"] for a in res["actions"]]
    assert types[:2] == ["flood", "safe_zones"]
    res = C.answer(demo_job, "rooftop solar potential?")
    solar = [a for a in res["actions"] if a["type"] == "solar"][0]
    assert solar["buildings"] and "MWh/yr" in res["reply"]
    assert (demo_job / "solar.json").exists()
