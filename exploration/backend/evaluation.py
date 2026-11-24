"""Reproducible B0 / B1 / B2 comparison on the offline Story C fixture corpus.

- B0  one-shot: read the corpus once; no follow-up, no provenance, no contradiction checks.
- B1  conventional iterative loop (offline proxy): the same planner-driven follow-up
      search, but every page counts as an independent source and conflicting amounts are
      not reconciled. It is a stand-in for "iterate on gaps without provenance reasoning".
      No LLM orchestrator is simulated, so this is not a measurement of any specific
      agent product.
- B2  SearchHax: provenance and origin clustering, contradiction reconciliation, and the
      in-process Focus convergence controller deciding the follow-up.

Every number comes from the run itself. The corpus is four documents (n = 1 story), so
the results show behavioural differences, not statistical effects. Latency is
wall-clock time on the local machine and is the only non-deterministic field.

Branch usefulness cannot be measured offline without inventing research, so it is
reported separately from a recorded live parent/child pair (see ``branch_usefulness``).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from statistics import mean
from typing import Any

from shared.contracts import ExplorationExport

from exploration.backend.focus_bridge import focus_post
from exploration.backend.orchestrator import Orchestrator
from exploration.backend.policy_space import record_policy_space

CONFIGURATIONS = {
    "B0": {"label": "one-shot", "reconcile": False, "follow_up": False, "classify": False, "controller": "none"},
    "B1": {"label": "conventional iterative (offline proxy)", "reconcile": False, "follow_up": True, "classify": False, "controller": "fallback"},
    "B2": {"label": "SearchHax", "reconcile": True, "follow_up": True, "classify": True, "controller": "focus"},
}


def _fallback(*_args, **_kwargs):
    raise ConnectionError("offline evaluation: labelled fallback controller")


def metrics(export: ExplorationExport, *, latency_ms: int) -> dict[str, Any]:
    versions = {version.id: version for version in export.document_versions}
    spans = export.evidence_spans
    exact = sum(1 for span in spans if (v := versions.get(span.version_id)) and v.content[span.start : span.end] == span.text)
    supported = [rel for rel in export.relationships if rel.status == "supported"]
    distinct_supported = {(rel.subject_name.casefold(), rel.predicate, rel.object_name.casefold()) for rel in supported}
    documents = len(export.documents)
    derivative = sorted({doc for cluster in export.origin_clusters for doc in cluster.derivative_document_ids})
    # Independent origins: one per detected origin cluster, plus every unclustered document.
    # Without classification (B0, B1) each document is implicitly its own origin.
    clustered = {doc for cluster in export.origin_clusters for doc in cluster.document_ids}
    independent_origins = len(export.origin_clusters) + sum(1 for doc in export.documents if doc.id not in clustered)
    policy = record_policy_space(export)
    usage = export.investigation.usage
    return {
        "grounding": {
            "evidence_spans": len(spans),
            "exact_quote_spans": exact,
            "exact_quote_rate": round(exact / len(spans), 3) if spans else None,
            "supported_relationships": len(supported),
            "distinct_supported_facts": len(distinct_supported),
            "contradicted_relationships": sum(1 for rel in export.relationships if rel.status == "contradicted"),
            "rejected_ungrounded_citations": usage.rejected_ungrounded_citations,
        },
        "independence": {
            "documents": documents,
            "independent_origins_claimed": independent_origins,
            "derivative_documents_detected": len(derivative),
            "shared_origin_detected": bool(derivative),
            "contradictions_detected": len(export.contradictions),
        },
        "novelty": {
            "policy_points": len(policy),
            "mean_policy_novelty": round(mean(row["novelty"] for row in policy), 3) if policy else None,
            "considered_candidates": sum(1 for row in policy if row["action"] == "consider"),
            "open_questions": len({q.text.casefold() for q in export.unresolved_questions}),
        },
        "cost": {
            "search_calls": usage.search_calls,
            "urls_fetched": usage.urls_fetched,
            "llm_calls": usage.llm_calls,
            "network_calls": usage.network_calls,
            "followups_executed": usage.followups_executed,
        },
        "latency_ms": latency_ms,
        "stop_reason": export.investigation.stop_reason,
        "decision_source": export.investigation.decision_source,
    }


def run_configuration(name: str, db_path: Path) -> dict[str, Any]:
    config = CONFIGURATIONS[name]
    post = focus_post if config["controller"] == "focus" else _fallback
    orchestrator = Orchestrator(db_path, post=post)
    started = time.perf_counter()
    export = orchestrator.run_story_c(
        reconcile=config["reconcile"], follow_up=config["follow_up"], classify=config["classify"], configuration=name if name != "B2" else "B3"
    )
    latency = int((time.perf_counter() - started) * 1000)
    orchestrator.database.close()
    return {"configuration": name, "label": config["label"], "investigation_id": export.investigation.id, **metrics(export, latency_ms=latency)}


def run_evaluation() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        results = [run_configuration(name, Path(tmp) / f"{name}.sqlite") for name in CONFIGURATIONS]
    return {
        "story": "C (offline fixture corpus: 4 documents, one shared wire story, one conflicting amount)",
        "sample_note": "One story, one run per configuration. Behavioural comparison, not a statistical result.",
        "configurations": {name: {k: v for k, v in c.items() if k != "label"} | {"label": c["label"]} for name, c in CONFIGURATIONS.items()},
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Offline B0/B1/B2 evaluation on Story C")
    parser.add_argument("--out", type=Path, default=None, help="Write JSON here (stdout when omitted).")
    parser.add_argument("--branch-usefulness", type=Path, default=None, help="Merge a recorded branch_usefulness JSON block.")
    args = parser.parse_args(argv)
    payload = run_evaluation()
    if args.branch_usefulness:
        payload["branch_usefulness"] = json.loads(args.branch_usefulness.read_text())
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
