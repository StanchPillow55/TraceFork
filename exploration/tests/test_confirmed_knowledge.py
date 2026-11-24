"""M8: explicitly confirmed knowledge feeds knowledge-value ranking. Offline.

Only the user's latest "known" confirmation counts. Encountered or retrieved material is
never treated as known. Scores already recorded are never recomputed, and every new
score names the confirmations it used.
"""

from datetime import datetime, timezone

import pytest

from exploration.backend.knowledge_value import confirmed_known_claims, ranking_profile
from exploration.backend.orchestrator import Orchestrator
from exploration.tests.test_integrated_pipeline import PAGE_A, PROFILE, QUESTION, _FakeRetrieval
from shared.contracts import KnowledgeAssertionIn

KNOWN_TEXT = "NVIDIA has invested $2 billion in CoreWeave to expand GPU datacenter capacity"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Retrieval is the in-process fake; this only satisfies the configuration gate.
    monkeypatch.setenv("TELEM_API_KEY", "offline-test-placeholder-not-a-key")


def _row(i, state, basis="user_confirmation", subject="NVIDIA CoreWeave investment", text=KNOWN_TEXT):
    return {"id": f"assert_{i}", "subject": subject, "predicate": "related_to", "object": "", "text": text,
            "epistemic_state": state, "basis": basis}


def test_only_the_latest_user_confirmed_known_assertion_counts():
    assert [c.id for c in confirmed_known_claims([_row(1, "known")])] == ["assert_1"]
    assert confirmed_known_claims([_row(1, "encountered")]) == []
    assert confirmed_known_claims([_row(1, "known", basis="retrieved")]) == []
    assert confirmed_known_claims([_row(1, "known", basis="profile_declared")]) == []
    # A later correction of the same subject withdraws the confirmation.
    assert confirmed_known_claims([_row(1, "known"), _row(2, "uncertain")]) == []
    assert [c.id for c in confirmed_known_claims([_row(1, "uncertain"), _row(2, "known")])] == ["assert_2"]


def test_ranking_profile_is_a_copy_with_provenance():
    merged, used = ranking_profile(PROFILE, [_row(7, "known")])
    assert used == ["assert_7"]
    assert len(merged.known_claims) == len(PROFILE.known_claims) + 1
    assert all(claim.id != "assert_7" for claim in PROFILE.known_claims)  # stored profile untouched


def _first_step(tmp_path, name, assertions=()):
    orch = Orchestrator(tmp_path / f"{name}.sqlite", retrieval=_FakeRetrieval({"https://news.example-live/nvidia-coreweave": PAGE_A}, follow_up_url=None))
    inv = orch.create(QUESTION, mode="web", profile=PROFILE)
    for i, (state, basis) in enumerate(assertions):
        orch.database.insert_assertion(
            f"assert_{name}_{i}",
            KnowledgeAssertionIn(profile_id=PROFILE.id, investigation_id=inv.id, subject="NVIDIA CoreWeave investment",
                                 text=KNOWN_TEXT, epistemic_state=state, basis=basis),
            datetime.now(timezone.utc),
        )
    export = orch.step(inv.id)
    scored = [c for c in export.candidates if c.knowledge_value is not None]
    assert scored, "the fixture page is scored"
    return orch, inv.id, scored[0].knowledge_value


def test_confirmed_knowledge_lowers_novelty_and_is_recorded(tmp_path):
    _, _, control = _first_step(tmp_path, "control")
    orch, inv_id, confirmed = _first_step(tmp_path, "confirmed", [("known", "user_confirmation")])
    assert confirmed.N < control.N  # tokens the user knows are no longer novel to them
    assert confirmed.score < control.score
    assert confirmed.confirmed_known_assertion_ids == ["assert_confirmed_0"]
    assert control.confirmed_known_assertion_ids == []
    # The recorded ranking event carries the same provenance the UI shows.
    import json

    from exploration.backend.trace import build_trace

    trace = json.dumps(build_trace(orch.database, inv_id).model_dump(mode="json"))
    assert '"knowledge_value_confirmed": ["assert_confirmed_0"]' in trace
    stored = orch.get(inv_id).investigation.metadata["profile_snapshot"]
    assert stored == PROFILE.model_dump(mode="json")  # the stored profile is not rewritten


def test_encountered_material_is_not_treated_as_known(tmp_path):
    _, _, control = _first_step(tmp_path, "control")
    _, _, encountered = _first_step(tmp_path, "encountered", [("encountered", "retrieved"), ("encountered", "user_confirmation")])
    assert encountered.N == control.N and encountered.score == control.score
    assert encountered.confirmed_known_assertion_ids == []


def test_recorded_scores_are_not_recomputed_after_a_later_confirmation(tmp_path):
    orch, inv_id, before = _first_step(tmp_path, "history")
    orch.database.insert_assertion(
        "assert_later",
        KnowledgeAssertionIn(profile_id=PROFILE.id, investigation_id=inv_id, subject="NVIDIA CoreWeave investment",
                             text=KNOWN_TEXT, epistemic_state="known", basis="user_confirmation"),
        datetime.now(timezone.utc),
    )
    reloaded = [c for c in orch.get(inv_id).candidates if c.knowledge_value is not None][0].knowledge_value
    assert reloaded == before
