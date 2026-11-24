"""The B0/B1/B2 evaluation is reproducible offline and its committed results match a fresh run."""

import json
from pathlib import Path

from exploration.backend.evaluation import run_evaluation

COMMITTED = Path(__file__).resolve().parents[2] / "docs" / "evaluation" / "b0_b1_b2_story_c.json"


def _strip(result):
    return {k: v for k, v in result.items() if k not in {"latency_ms", "investigation_id"}}


def test_fresh_run_matches_the_committed_results_except_latency():
    fresh = run_evaluation()
    committed = json.loads(COMMITTED.read_text())
    assert [_strip(r) for r in fresh["results"]] == [_strip(r) for r in committed["results"]]
    assert all(r["cost"]["network_calls"] == 0 and r["cost"]["llm_calls"] == 0 for r in fresh["results"])


def test_searchhax_detects_what_the_baselines_overclaim():
    results = {r["configuration"]: r for r in run_evaluation()["results"]}
    for name in ("B0", "B1"):
        assert results[name]["independence"]["independent_origins_claimed"] == results[name]["independence"]["documents"]
        assert results[name]["independence"]["contradictions_detected"] == 0
    b2 = results["B2"]
    assert b2["independence"]["shared_origin_detected"] is True
    assert b2["independence"]["independent_origins_claimed"] < b2["independence"]["documents"]
    assert b2["independence"]["contradictions_detected"] >= 1
    assert b2["grounding"]["exact_quote_rate"] == 1.0
    assert b2["decision_source"] == "convergence"


def test_branch_usefulness_is_labelled_as_a_recorded_live_result():
    block = json.loads(COMMITTED.read_text())["branch_usefulness"]
    assert block["source"].startswith("recorded live run")
    assert "n = 1" in block["note"]
