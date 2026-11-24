"""Bounded investigation loop.

The backend asks the convergence controller which follow-up to run, then clamps
the answer to the hard search and fetch caps. If the controller is down, the
labeled fallback runs at most one non-duplicate query. This module does not
implement intent alignment, novelty, or redundancy policy.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from shared.contracts import (
    Document,
    DocumentVersion,
    EvidenceCandidate,
    ExplorationExport,
    Investigation,
    InvestigationBudget,
    ResearchProfile,
    SourceProvenance,
    UnresolvedQuestion,
)

from exploration.backend.attention import score_passage
from exploration.backend.budget import BudgetExceededError, BudgetGuard, clamp_budget
from exploration.backend.checkpoints import save_checkpoint
from exploration.backend.config import default_db_path, llm_api_key, telem_configured
from exploration.backend.convergence import build_request, evaluate_controller
from exploration.backend.database import Database
from exploration.backend.extraction import ExtractionBatch, extract_document, merge_batches
from exploration.backend.llm import OpenAICompatibleClient
from exploration.backend.graph import EvidenceGraph
from exploration.backend.ids import content_hash, new_id, stable_id
from exploration.backend.knowledge_value import ranking_profile, score_knowledge_value
from exploration.backend.planner import branch_query, generate_actions, initial_search_query
from exploration.backend.retrieval import RetrievalClient, normalize_url, page_failed
from exploration.backend.story_c import STORY_C_QUESTION, load_story_c
from exploration.backend.trace import extraction_payload, ingest_payload, lifecycle_of, ranking_rows, record_event
from exploration.backend.verification import classify_provenance, origin_clusters, reconcile_amount_conflicts

STORY_A_QUESTION = (
    "How do NVIDIA's investments, GPU infrastructure customers, and AI capacity expansion reinforce one another?"
)
INDEPENDENCE_QUESTION = (
    "independent primary confirmation of the Lumenarc Dynamics Series D amount "
    "that does not cite wire LF-PR-2026-031"
)


class Orchestrator:
    def __init__(self, db_path: str | Path | None = None, retrieval: RetrievalClient | None = None, post=None, convergence_url: str | None = None):
        self.database = Database(db_path or default_db_path())
        self.graph = EvidenceGraph(self.database)
        self.retrieval = retrieval or RetrievalClient()
        self.post = post
        self.convergence_url = convergence_url

    def create(
        self,
        question: str,
        budget: InvestigationBudget | None = None,
        *,
        mode: str = "web",
        profile: ResearchProfile | None = None,
        profile_source_investigation_id: str | None = None,
    ) -> Investigation:
        if not question.strip():
            raise ValueError("question must not be blank")
        now = _now()
        metadata: dict = {"mode": mode}
        if profile is not None:
            # Read-only profile snapshot. It conditions knowledge-value scoring
            # of live candidates; it is never stored as sourced web evidence.
            metadata["profile_snapshot"] = profile.model_dump(mode="json")
            if profile_source_investigation_id:
                # Where the snapshot was copied from (the source is not modified).
                metadata["profile_source_investigation_id"] = profile_source_investigation_id
        investigation = Investigation(
            id=new_id("inv"),
            question=question.strip(),
            budget=clamp_budget(budget),
            status="created",
            usage={"configuration": mode},
            created_at=now,
            updated_at=now,
            metadata=metadata,
        )
        self.database.insert_investigation(investigation)
        return investigation

    def stored_profile(self, investigation_id: str) -> ResearchProfile | None:
        """The profile recorded on an existing investigation (read only). KeyError if it does not exist."""
        return self._profile(self.get(investigation_id))

    def _profile(self, export: ExplorationExport) -> ResearchProfile | None:
        """Return the investigation's read-only profile, if one was attached."""
        if export.profile is not None:
            return export.profile
        raw = export.investigation.metadata.get("profile_snapshot")
        if not raw:
            return None
        return ResearchProfile.model_validate(raw)

    def _add_profile_questions(self, export: ExplorationExport) -> None:
        """Seed open questions from the profile's unresolved interests.

        This is the profile-conditioned signal focus and convergence reason
        over. It does not change the immutable investigation question, does not
        fabricate evidence, and is skipped when no profile is attached.
        """
        profile = self._profile(export)
        if profile is None:
            return
        export.profile = profile
        existing = {question.text.strip().lower() for question in export.unresolved_questions}
        for interest in profile.unresolved_interests:
            text = f"What grounded evidence connects the investigation to the open interest: {interest}?"
            if text.strip().lower() in existing:
                continue
            export.unresolved_questions.append(
                UnresolvedQuestion(
                    id=stable_id("q", export.investigation.id, "profile", interest),
                    text=text,
                    importance=0.8,
                    missing_evidence=1.0,
                    kind="profile_interest",
                )
            )

    def get(self, investigation_id: str) -> ExplorationExport:
        return self.graph.load(investigation_id)

    def resume(self, investigation_id: str) -> dict[str, int]:
        """Reload stored evidence. Unchanged cached or stored text is not fetched again."""
        export = self.graph.load(investigation_id)
        stats = {"reused": 0, "unchanged_versions": 0, "network_fetches": 0}
        documents = {document.id: document for document in export.documents}
        for version in export.document_versions:
            document = documents.get(version.document_id)
            if document is None:
                continue
            cached = self.retrieval.cached_fetch(document.canonical_url)
            if cached and cached.content_hash == version.content_hash and cached.content == version.content:
                stats["reused"] += 1
                continue
            if version.content_hash == content_hash(version.content):
                stats["unchanged_versions"] += 1
                continue
            stats["network_fetches"] += 1
            if not self.retrieval.dry_run:
                self.retrieval.fetch([document.canonical_url], session_id=export.investigation.session_id)
        return stats

    def run_story_c(
        self,
        *,
        reconcile: bool = True,
        follow_up: bool = True,
        classify: bool = True,
        configuration: str = "B3",
    ) -> ExplorationExport:
        started = _now_seconds()
        investigation = self.create(STORY_C_QUESTION, mode="story_c")
        investigation.usage.configuration = configuration
        batches: list[ExtractionBatch] = []
        documents: list[Document] = []
        versions: list[DocumentVersion] = []
        provenance: list[SourceProvenance] = []
        for fixture in load_story_c():
            canonical = normalize_url(fixture.url)
            digest = content_hash(fixture.content)
            document = Document(
                id=stable_id("doc", investigation.id, canonical),
                canonical_url=canonical,
                title=fixture.title,
                providers=["fixture"],
            )
            version = DocumentVersion(
                id=stable_id("ver", document.id, digest),
                document_id=document.id,
                content_hash=digest,
                fetched_at=_now(),
                content=fixture.content,
                publish_date=fixture.publish_date,
            )
            batch = extract_document(
                fixture.content,
                url=canonical,
                document_id=document.id,
                version_id=version.id,
                version_hash=digest,
                question=investigation.question,
                explicit_fixture=True,
                publish_date=fixture.publish_date,
            )
            batches.append(batch)
            if classify:
                item = classify_provenance(document.id, canonical, fixture.content, ["fixture"])
                document.origin_id = item.origin_id
            else:
                item = SourceProvenance(
                    id=stable_id("prov", document.id, "b0"),
                    document_id=document.id,
                    provider="fixture",
                    providers=["fixture"],
                    origin_kind="unknown",
                    notes="B0 stores the document without cross-document provenance classification.",
                )
            documents.append(document)
            versions.append(version)
            provenance.append(item)

        merged = merge_batches(batches)
        contradictions = []
        if classify and reconcile:
            contradictions = reconcile_amount_conflicts(merged.relationships, merged.claims, merged.links, provenance)
        clusters = origin_clusters(provenance) if classify else []
        questions: list[UnresolvedQuestion] = []
        if classify and any(cluster.derivative_document_ids for cluster in clusters):
            questions.append(
                UnresolvedQuestion(
                    id=stable_id("q", investigation.id, "independence"),
                    text=INDEPENDENCE_QUESTION,
                    importance=0.9,
                    missing_evidence=1.0,
                    kind="independence",
                )
            )
        candidates = [
            _candidate(investigation.question, document, version, questions)
            for document, version in zip(documents, versions, strict=True)
        ]
        actions = generate_actions(questions, []) if follow_up else []
        investigation.status = "running"
        investigation.live_extraction_blocked = merged.live_extraction_blocked
        investigation.blocker = merged.blocker
        investigation.usage.corpus_documents = len(documents)
        investigation.usage.rejected_ungrounded_citations = merged.rejected_ungrounded
        investigation.usage.providers_used = ["fixture"]
        investigation.metadata["extraction_origin"] = "deterministic_fixture"
        investigation.metadata["llm_relationships"] = 0
        investigation.updated_at = _now()
        export = ExplorationExport(
            investigation=investigation,
            documents=documents,
            document_versions=versions,
            evidence_spans=merged.spans,
            entities=merged.entities,
            claims=merged.claims,
            relationships=merged.relationships,
            evidence_links=merged.links,
            provenance=provenance,
            unresolved_questions=questions,
            candidates=candidates,
            actions=actions,
            contradictions=contradictions,
            origin_clusters=clusters,
        )
        self._persist(export)
        self._record_fixture_stages(export, summary_prefix="Story C fixture corpus")
        save_checkpoint(self.database, export, step=1, summary="Ingested the Story C fixture corpus with no network calls.")
        if not follow_up:
            investigation.status = "stopped"
            investigation.usage.latency_ms = _elapsed_ms(started)
            investigation.updated_at = _now()
            self._persist(export)
            save_checkpoint(self.database, export, step=2, summary="B0 single pass stopped without a follow-up or provenance graph.")
            return self.graph.load(investigation.id)

        decision = evaluate_controller(
            build_request(
                investigation_id=investigation.id,
                question=investigation.question,
                questions=questions,
                candidates=candidates,
                actions=actions,
                budget=investigation.budget,
                usage=investigation.usage,
            ),
            followups_executed=investigation.usage.followups_executed,
            url=self.convergence_url,
            post=self.post,
        )
        export.decision = decision
        investigation.decision_source = decision.decision_source
        investigation.decision_rationale = decision.rationale
        if decision.approved_action and not decision.stop:
            try:
                self._local_follow_up(export, decision)
            except BudgetExceededError:
                investigation.stop_reason = "budget_exhausted"
                decision.stop = True
                decision.stop_reason = "budget_exhausted"
        if investigation.stop_reason is None:
            if decision.stop and decision.stop_reason:
                investigation.stop_reason = decision.stop_reason
            elif questions and any(question.missing_evidence >= 0.5 for question in questions):
                investigation.stop_reason = "unresolved_uncertainty"
            elif not actions and not questions:
                investigation.stop_reason = "evidence_sufficient"
            else:
                investigation.stop_reason = decision.stop_reason or "no_useful_actions"
        investigation.status = "stopped"
        investigation.usage.latency_ms = _elapsed_ms(started)
        investigation.updated_at = _now()
        self._persist(export)
        self._record_convergence(export, decision)
        save_checkpoint(
            self.database,
            export,
            step=2,
            summary="Recorded the controller or fallback decision and any one local follow-up.",
            stop_reason=investigation.stop_reason,
        )
        return self.graph.load(investigation.id)

    def run_profile(self, directory: Path | None = None, *, bridges=None) -> ExplorationExport:
        """Rank a fixed local corpus with query attention and knowledge value. No network."""
        from exploration.backend.profile_run import execute_profile

        return execute_profile(self, directory=directory, bridges=bridges)

    def reload_profile(self, investigation_id: str) -> ExplorationExport:
        """Read the stored profile investigation. This does not search or fetch."""
        return self.graph.load(investigation_id)

    def step(self, investigation_id: str) -> ExplorationExport:
        """Run one research step and record the resulting lifecycle state."""
        try:
            result = self._step(investigation_id)
        except Exception as error:
            # Record the failure on the investigation; never substitute results.
            try:
                export = self.graph.load(investigation_id)
                export.investigation.metadata["lifecycle"] = "failed"
                export.investigation.metadata["last_error"] = f"{type(error).__name__}: {error}"[:500]
                export.investigation.updated_at = _now()
                self._persist(export)
            except Exception:
                pass
            raise
        state = lifecycle_of(result.investigation)
        if result.investigation.metadata.get("lifecycle") != state:
            result.investigation.metadata["lifecycle"] = state
            result.investigation.metadata.pop("last_error", None)
            self._persist(result)
        return result

    def _step(self, investigation_id: str) -> ExplorationExport:
        export = self.graph.load(investigation_id)
        investigation = export.investigation
        if investigation.status in {"stopped", "blocked"}:
            return export
        if investigation.metadata.get("mode") in {"story_c", "profile"}:
            return export
        if (
            investigation.metadata.get("intervention_type") == "challenge_evidence"
            and investigation.usage.search_calls == 0
        ):
            return self._challenge_step(export)
        if not export.documents and investigation.usage.search_calls == 0:
            return self._initial_web_step(export)
        return self._follow_up_web_step(export)

    def _initial_web_step(self, export: ExplorationExport) -> ExplorationExport:
        investigation = export.investigation
        if not telem_configured() and not self.retrieval.dry_run:
            investigation.status = "blocked"
            investigation.blocker = "TELEM_API_KEY missing"
            investigation.live_extraction_blocked = llm_api_key() is None
            investigation.updated_at = _now()
            self._persist(export)
            self._event(
                investigation.id,
                "blocked",
                {"blocker": investigation.blocker, "search_calls": 0, "verified_success": False},
                "Blocked before search. No results were invented.",
            )
            save_checkpoint(self.database, export, step=1, summary="Blocked before search: TELEM_API_KEY missing. No results were invented.")
            return self.graph.load(investigation.id)
        started = _now_seconds()
        guard = BudgetGuard(investigation.budget)
        query = initial_search_query(investigation.question)
        if not query.strip():
            # Never send an empty search; nothing is invented either.
            investigation.status = "stopped"
            investigation.stop_reason = "no_useful_actions"
            investigation.decision_source = "fallback"
            investigation.decision_rationale = "The question yields no searchable text. No search was run."
            investigation.updated_at = _now()
            self._persist(export)
            return self.graph.load(investigation.id)
        try:
            bundle = self.retrieval.search(
                query,
                session_id=investigation.session_id,
                guard=guard,
            )
        except BudgetExceededError:
            return self._stop_budget(export, guard)
        except Exception as error:  # provider failure after bounded retries
            return self._stop_retrieval_failure(
                export, guard, query, [f"search failed: {type(error).__name__}: {error}"[:300]], record_search=True
            )
        investigation.budget = guard.budget
        self._record_search(
            investigation,
            bundle,
            network=not bundle.dry_run,
            planned_from={"source": "research_question", "target_question_id": None, "action_id": None},
        )
        failures = self._ingest_bundle(export, bundle, guard)
        if not export.documents and (bundle.provider_errors or failures):
            return self._stop_retrieval_failure(
                export, guard, bundle.query, [*bundle.provider_errors, *failures], record_search=False
            )
        self._add_profile_questions(export)
        investigation.status = "running"
        investigation.updated_at = _now()
        self._persist(export)
        # Record the step latency before the checkpoint so both agree.
        investigation.usage.latency_ms = _elapsed_ms(started)
        save_checkpoint(self.database, export, step=1, summary="Initial Telem search and shortlisted fetch.")
        if investigation.live_extraction_blocked and not export.relationships:
            investigation.status = "stopped"
            investigation.stop_reason = "unresolved_uncertainty"
            investigation.decision_source = "fallback"
            investigation.decision_rationale = (
                investigation.blocker
                or "Extraction produced no grounded relationships. Fallback did not invent any or spend a follow-up search."
            )
            investigation.updated_at = _now()
            self._persist(export)
            self._event(
                investigation.id,
                "convergence",
                {
                    "stop": True,
                    "stop_reason": "unresolved_uncertainty",
                    "decision_source": "fallback",
                    "rationale": investigation.decision_rationale,
                    "alternatives": [],
                    "unresolved_questions": [question.text for question in export.unresolved_questions],
                    "unresolved": True,
                    "verified_success": False,
                },
                "Stopped unresolved because extraction is blocked. This is not a verified answer.",
            )
            save_checkpoint(self.database, export, step=2, summary="Stopped without a follow-up because extraction is blocked.", stop_reason="unresolved_uncertainty")
        else:
            self._persist(export)
        return self.graph.load(investigation.id)

    def _follow_up_web_step(self, export: ExplorationExport) -> ExplorationExport:
        investigation = export.investigation
        actions = generate_actions(export.unresolved_questions, self.database.search_queries(investigation.id))
        export.actions = actions
        decision = evaluate_controller(
            build_request(
                investigation_id=investigation.id,
                question=investigation.question,
                questions=export.unresolved_questions,
                candidates=export.candidates,
                actions=actions,
                budget=investigation.budget,
                usage=investigation.usage,
            ),
            followups_executed=investigation.usage.followups_executed,
            url=self.convergence_url,
            post=self.post,
        )
        export.decision = decision
        investigation.decision_source = decision.decision_source
        investigation.decision_rationale = decision.rationale
        guard = BudgetGuard(investigation.budget)
        already_searched = {query.strip().lower() for query in self.database.search_queries(investigation.id)}
        action = decision.approved_action
        duplicate_query = bool(action and action.query.strip().lower() in already_searched)
        if duplicate_query and action is not None:
            action.already_executed = True
            action.result_summary = "Skipped a query that was already searched."
        if action is not None and not action.query.strip():
            # A blank approved query is not a search; it does not spend budget.
            action.result_summary = "Skipped a blank approved query."
            duplicate_query = True
        searched = False
        if action and not decision.stop and not duplicate_query:
            searched = True
            try:
                bundle = self.retrieval.search(
                    action.query,
                    session_id=investigation.session_id,
                    guard=guard,
                )
            except BudgetExceededError:
                return self._stop_budget(export, guard)
            investigation.budget = guard.budget
            self._record_search(
                investigation,
                bundle,
                network=not bundle.dry_run,
                planned_from={
                    "source": "approved_action",
                    "action_id": action.id,
                    "target_question_id": action.target_question_id,
                    "decision_source": decision.decision_source,
                },
            )
            self._ingest_bundle(export, bundle, guard)
            investigation.usage.followups_executed += 1
            decision.approved_action.already_executed = True
            decision.approved_action.result_summary = f"Follow-up returned {len(bundle.documents)} deduplicated URLs."
        if decision.stop:
            # A controller stop ends the investigation even without a reason
            # code; the recorded reason is derived, not invented evidence.
            investigation.stop_reason = decision.stop_reason or (
                "unresolved_uncertainty" if export.unresolved_questions else "no_useful_actions"
            )
            investigation.status = "stopped"
        elif decision.decision_source == "fallback":
            investigation.stop_reason = (
                "unresolved_uncertainty" if export.unresolved_questions else "evidence_sufficient"
            )
            investigation.status = "stopped"
        elif investigation.budget.remaining_searches <= 0:
            investigation.stop_reason = "budget_exhausted"
            investigation.status = "stopped"
        elif not searched:
            # The controller approved nothing runnable and did not stop. Staying
            # "running" would repeat the same idle decision forever.
            investigation.stop_reason = "no_useful_actions"
            investigation.status = "stopped"
        else:
            investigation.status = "running"
        investigation.updated_at = _now()
        self._persist(export)
        self._record_convergence(export, decision)
        save_checkpoint(self.database, export, step=2, summary="Follow-up decision applied within the backend budget.", stop_reason=investigation.stop_reason)
        return self.graph.load(investigation.id)

    def _local_follow_up(self, export: ExplorationExport, decision) -> None:
        investigation = export.investigation
        guard = BudgetGuard(investigation.budget)
        guard.authorize_search()
        investigation.budget = guard.budget
        action = decision.approved_action
        independent: list[str] = []
        for version in export.document_versions:
            has_amount = re.search(r"\$\d+ million Series D", version.content) is not None
            cites_wire = "LF-PR-2026-031" in version.content
            if has_amount and not cites_wire:
                independent.append(version.document_id)
        summary = (
            "No document in the local corpus states a Series D amount without citing wire LF-PR-2026-031."
            if not independent
            else "Found local documents that state an amount without the wire id: " + ", ".join(independent)
        )
        action.already_executed = True
        action.result_summary = summary
        for existing in export.actions:
            if existing.id == action.id:
                existing.already_executed = True
                existing.result_summary = summary
        investigation.usage.search_calls += 1
        investigation.usage.followups_executed += 1
        self.database.insert_search(
            search_id=new_id("search"),
            investigation_id=investigation.id,
            query=action.query,
            session_id=None,
            interaction_id=None,
            providers=["local_corpus"],
            raw={"independent_document_ids": independent, "network": False},
            created_at=_now(),
            network=False,
        )
        self._event(
            investigation.id,
            "search",
            {
                "query": action.query,
                "providers": ["local_corpus"],
                "network": False,
                "result_urls": [],
                "results": [],
                "result_count": 0,
                "independent_document_ids": independent,
                "kind": "local_corpus_scan",
                "verified": False,
            },
            f"Local corpus scan: {action.query}",
        )

    def _record_search(self, investigation: Investigation, bundle, *, network: bool, planned_from: dict | None = None) -> None:
        investigation.usage.search_calls += 1
        investigation.usage.provider_calls += bundle.provider_call_count
        if network:
            investigation.usage.network_calls += 1
        if bundle.session_id:
            investigation.session_id = bundle.session_id
            investigation.usage.session_id = bundle.session_id
        if bundle.interaction_id and bundle.interaction_id not in investigation.usage.interaction_ids:
            investigation.usage.interaction_ids.append(bundle.interaction_id)
        for provider in [hit.provider for document in bundle.documents for hit in document.hits]:
            if provider not in investigation.usage.providers_used:
                investigation.usage.providers_used.append(provider)
        providers = sorted({hit.provider for hit in bundle.provider_hits})
        self.database.insert_search(
            search_id=new_id("search"),
            investigation_id=investigation.id,
            query=bundle.query,
            session_id=bundle.session_id,
            interaction_id=bundle.interaction_id,
            providers=providers,
            raw=bundle.raw,
            created_at=_now(),
            network=network,
        )
        self._event(
            investigation.id,
            "search",
            {
                "query": bundle.query,
                # Round = ordinal of this search within the investigation (1-based), recorded at search time.
                "round": investigation.usage.search_calls,
                **({"planned_from": planned_from} if planned_from is not None else {}),
                "providers": providers,
                "network": network,
                "dry_run": bundle.dry_run,
                "from_cache": bundle.from_cache,
                "session_id": bundle.session_id,
                "interaction_id": bundle.interaction_id,
                "result_count": len(bundle.documents),
                "result_urls": [document.canonical_url for document in bundle.documents],
                "results": [
                    {
                        "url": document.canonical_url,
                        "title": document.title,
                        "snippet": document.snippet,
                        "providers": list(document.providers),
                    }
                    for document in bundle.documents
                ],
                "verified": False,
            },
            f"Searched: {bundle.query}",
        )

    def _ingest_bundle(self, export: ExplorationExport, bundle, guard: BudgetGuard) -> list[str]:
        """Store fetched pages. Returns the fetch failures (empty when none)."""
        investigation = export.investigation
        failures: list[str] = []
        profile = self._profile(export)
        # Knowledge value ranks against the stored priors plus what the user has explicitly
        # confirmed as known so far. Scores are recorded once, here; they are never recomputed.
        confirmed_ids: list[str] = []
        if profile is not None:
            profile, confirmed_ids = ranking_profile(profile, self.database.list_assertions(profile.id))
        candidate_start = len(export.candidates)
        claim_start = len(export.claims)
        span_start = len(export.evidence_spans)
        link_start = len(export.evidence_links)
        relationship_start = len(export.relationships)
        provenance_start = len(export.provenance)
        ranked = []
        for document in bundle.documents:
            gaps = [question.text for question in export.unresolved_questions]
            attention = score_passage(investigation.question, document.snippet or document.title, gaps, document.publish_date)
            ranked.append((attention.score, document, attention))
        ranked.sort(key=lambda item: item[0], reverse=True)
        chosen = [item for item in ranked if item[1].canonical_url not in {doc.canonical_url for doc in export.documents}]
        chosen = chosen[: min(5, investigation.budget.remaining_fetched_urls)]
        pages = []
        if chosen:
            fetched_before = investigation.budget.urls_fetched
            try:
                pages = self.retrieval.fetch(
                    [item[1].canonical_url for item in chosen],
                    session_id=investigation.session_id,
                    guard=guard,
                )
            except BudgetExceededError:
                raise
            except Exception as error:  # the search is already recorded; record the fetch failure too
                pages = []
                failures.append(f"fetch failed: {type(error).__name__}: {error}"[:300])
            investigation.budget = guard.budget
            for page in pages:
                # The fetch is an interaction on the same session lineage.
                if page.interaction_id and not page.from_cache and page.interaction_id not in investigation.usage.interaction_ids:
                    investigation.usage.interaction_ids.append(page.interaction_id)
                if page_failed(page) and not page.content.strip():
                    failures.append(f"fetch failed for {page.canonical_url}: {page.error or page.status}"[:300])
            investigation.usage.fetch_calls += 1
            investigation.usage.urls_fetched = investigation.budget.urls_fetched
            investigation.usage.cache_hits += sum(1 for page in pages if page.from_cache and page.content.strip())
            if investigation.budget.urls_fetched > fetched_before:
                investigation.usage.network_calls += 1
            self._event(
                investigation.id,
                "retrieval",
                {
                    "pages": [
                        {
                            "url": page.canonical_url,
                            "title": page.title,
                            "provider": page.provider,
                            "from_cache": page.from_cache,
                            "status": page.status,
                            "content_present": bool(page.content.strip()),
                        }
                        for page in pages
                    ],
                    "fetch_call": True,
                    "verified": False,
                },
                f"Retrieved {len(pages)} page(s) for this search.",
            )
        # A page is found by the URL that was requested as well as by the
        # canonical URL the fetch reported (which may differ after redirects).
        page_by_url = {normalize_url(page.url): page for page in pages if page.url}
        page_by_url.update({page.canonical_url: page for page in pages if page.canonical_url not in page_by_url})
        batches: list[ExtractionBatch] = []
        llm_calls_here = 0
        for _score, merged, attention in chosen:
            page = page_by_url.get(merged.canonical_url)
            content = page.content if page else ""
            if not content.strip():
                continue
            publish_date = merged.publish_date or page.publish_date
            digest = content_hash(content)
            document = Document(
                id=stable_id("doc", investigation.id, merged.canonical_url),
                canonical_url=merged.canonical_url,
                title=merged.title or (page.title if page else ""),
                providers=list(merged.providers),
            )
            version = DocumentVersion(
                id=stable_id("ver", document.id, digest),
                document_id=document.id,
                content_hash=digest,
                fetched_at=_now(),
                content=content,
                publish_date=publish_date,
            )
            export.documents.append(document)
            export.document_versions.append(version)
            export.provenance.append(
                classify_provenance(document.id, merged.canonical_url, content, list(merged.providers))
                if is_fixture_url(merged.canonical_url)
                else SourceProvenance(
                    id=stable_id("prov", document.id, merged.canonical_url),
                    document_id=document.id,
                    provider=merged.providers[0] if merged.providers else None,
                    providers=list(merged.providers),
                    origin_kind="unknown",
                    notes="Provider provenance is retained. Source independence is unresolved until a citation or shared origin is found.",
                )
            )
            # Knowledge value is computed against the read-only profile when one
            # is attached. It is a separate signal from query attention, which
            # is unchanged above. No profile means no knowledge-value score.
            knowledge = (
                score_knowledge_value(content, profile) if profile is not None else None
            )
            if knowledge is not None and confirmed_ids:
                knowledge.confirmed_known_assertion_ids = list(confirmed_ids)
            export.candidates.append(
                EvidenceCandidate(
                    id=stable_id("cand", document.id),
                    document_id=document.id,
                    url=document.canonical_url,
                    title=document.title,
                    # The controller ranks what was fetched, not the search snippet.
                    snippet=" ".join(content.split())[:280],
                    providers=list(merged.providers),
                    attention=attention,
                    knowledge_value=knowledge,
                    publish_date=publish_date,
                )
            )
            llm_before = investigation.usage.llm_calls
            batches.append(self._extract_page(investigation, guard, document, version))
            llm_calls_here += investigation.usage.llm_calls - llm_before
        new_candidates = export.candidates[candidate_start:]
        if new_candidates:
            self._event(
                investigation.id,
                "ranking",
                {
                    "source": "search",
                    "candidates": ranking_rows(export, "current_run", new_candidates),
                    "verified": False,
                },
                f"Ranked {len(new_candidates)} candidate(s) from this search. Scores are not verification.",
            )
        if not batches:
            return failures
        merged_batch = merge_batches(batches)
        export.entities.extend(merged_batch.entities)
        export.claims.extend(merged_batch.claims)
        export.relationships.extend(merged_batch.relationships)
        export.evidence_spans.extend(merged_batch.spans)
        export.evidence_links.extend(merged_batch.links)
        export.unresolved_questions.extend(merged_batch.questions)
        investigation.usage.rejected_ungrounded_citations += merged_batch.rejected_ungrounded
        # Cross-document checks run on the web path too: documents citing one
        # wire are one origin, and disagreeing amounts are recorded as conflicts.
        export.origin_clusters = origin_clusters(export.provenance)
        known = {item.id for item in export.contradictions}
        for item in reconcile_amount_conflicts(
            export.relationships, export.claims, export.evidence_links, export.provenance
        ):
            if item.id not in known:
                export.contradictions.append(item)
                known.add(item.id)
        if merged_batch.live_extraction_blocked and not merged_batch.relationships:
            investigation.live_extraction_blocked = True
            investigation.blocker = merged_batch.blocker
        if merged_batch.llm_tokens is not None:
            investigation.usage.llm_tokens = (investigation.usage.llm_tokens or 0) + merged_batch.llm_tokens
        payload = extraction_payload(
            claims=export.claims[claim_start:],
            spans=export.evidence_spans[span_start:],
            links=export.evidence_links[link_start:],
            relationships=export.relationships[relationship_start:],
            provenance=export.provenance[provenance_start:],
            llm_call=llm_calls_here > 0,
            blocker=merged_batch.blocker,
        )
        if payload["passages"] or payload["blocker"]:
            summary = "Extracted grounded passages." if payload["passages"] else "Extraction recorded no grounded passages."
            self._event(investigation.id, "extraction", payload, summary)
        return failures

    def _extract_page(self, investigation: Investigation, guard: BudgetGuard, document: Document, version: DocumentVersion) -> ExtractionBatch:
        disclosed = extract_document(
            version.content,
            url=document.canonical_url,
            document_id=document.id,
            version_id=version.id,
            version_hash=version.content_hash,
            question=investigation.question,
            publish_date=version.publish_date,
            llm=OpenAICompatibleClient(api_key=""),
        )
        if not llm_api_key():
            return disclosed
        try:
            guard.authorize_llm()
        except BudgetExceededError:
            return disclosed
        investigation.budget = guard.budget
        investigation.usage.llm_calls += 1
        return extract_document(
            version.content,
            url=document.canonical_url,
            document_id=document.id,
            version_id=version.id,
            version_hash=version.content_hash,
            question=investigation.question,
            publish_date=version.publish_date,
        )

    def _stop_retrieval_failure(
        self,
        export: ExplorationExport,
        guard: BudgetGuard,
        query: str,
        errors: list[str],
        *,
        record_search: bool,
    ) -> ExplorationExport:
        """Stop with the provider/fetch failure recorded. Nothing is invented."""
        investigation = export.investigation
        investigation.budget = guard.budget
        if record_search:
            # The search was authorized and attempted; history and usage agree.
            investigation.usage.search_calls += 1
            investigation.usage.network_calls += 1
            self.database.insert_search(
                search_id=new_id("search"),
                investigation_id=investigation.id,
                query=query,
                session_id=investigation.session_id,
                interaction_id=None,
                providers=[],
                raw={"error": errors},
                created_at=_now(),
                network=True,
            )
        detail = "; ".join(errors) or "retrieval returned no usable page"
        investigation.status = "stopped"
        investigation.stop_reason = "unresolved_uncertainty"
        investigation.decision_source = "fallback"
        investigation.decision_rationale = f"Retrieval failed ({detail}). No results were invented."
        investigation.updated_at = _now()
        self._persist(export)
        self._event(
            investigation.id,
            "convergence",
            {
                "stop": True,
                "stop_reason": "unresolved_uncertainty",
                "decision_source": "fallback",
                "rationale": investigation.decision_rationale,
                "alternatives": [],
                "unresolved": True,
                "verified_success": False,
            },
            "Stopped because retrieval failed. This is not a verified answer.",
        )
        save_checkpoint(
            self.database,
            export,
            step=max(1, investigation.usage.search_calls),
            summary="Stopped after a retrieval failure. No results were invented.",
            stop_reason="unresolved_uncertainty",
        )
        return self.graph.load(investigation.id)

    def _stop_budget(self, export: ExplorationExport, guard: BudgetGuard) -> ExplorationExport:
        export.investigation.budget = guard.budget
        export.investigation.status = "stopped"
        export.investigation.stop_reason = "budget_exhausted"
        export.investigation.decision_rationale = "Backend refused another search because the hard cap is 2 searches."
        export.investigation.updated_at = _now()
        self._persist(export)
        self._event(
            export.investigation.id,
            "convergence",
            {
                "stop": True,
                "stop_reason": "budget_exhausted",
                "decision_source": export.investigation.decision_source or "fallback",
                "rationale": export.investigation.decision_rationale,
                "alternatives": [],
                "unresolved": False,
                "verified_success": False,
            },
            "Stopped because the search budget was exhausted. This is not a verified answer.",
        )
        save_checkpoint(self.database, export, step=export.investigation.usage.search_calls + 1, summary="Refused a search past the hard cap.", stop_reason="budget_exhausted")
        return self.graph.load(export.investigation.id)

    def _challenge_step(self, export: ExplorationExport) -> ExplorationExport:
        investigation = export.investigation
        objective = (investigation.metadata.get("objective") or "").strip()
        query, planned_from = branch_query(
            root_question=investigation.metadata.get("root_question") or investigation.question,
            objective=objective,
            attached_claims=list(investigation.metadata.get("attached_claims") or []),
            intervention_type="challenge_evidence",
        )
        if query is None:
            # Legacy challenge rows created before grounding validation: refuse to search
            # for an ungrounded prompt instead of sending it to retrieval verbatim.
            investigation.status = "stopped"
            investigation.stop_reason = "no_useful_actions"
            investigation.decision_source = "fallback"
            investigation.decision_rationale = (
                f"Challenge objective {objective!r} names nothing to test and no claim was attached. "
                "No search was run."
            )
            investigation.updated_at = _now()
            self._persist(export)
            self._event(
                investigation.id,
                "convergence",
                {
                    "stop": True,
                    "stop_reason": "no_useful_actions",
                    "decision_source": "fallback",
                    "rationale": investigation.decision_rationale,
                    "planned_from": planned_from,
                    "search_calls": 0,
                    "verified_success": False,
                },
                "Challenge refused: nothing grounded to search for. No counterevidence was invented.",
            )
            return self.graph.load(investigation.id)
        if not telem_configured() and not self.retrieval.dry_run:
            investigation.status = "blocked"
            investigation.blocker = "TELEM_API_KEY missing"
            investigation.updated_at = _now()
            self._persist(export)
            self._event(
                investigation.id,
                "blocked",
                {"blocker": investigation.blocker, "objective": objective, "search_calls": 0, "verified_success": False},
                "Challenge search was not run. No counterevidence was invented.",
            )
            save_checkpoint(
                self.database,
                export,
                step=2,
                summary="Challenge stopped before search because retrieval is not configured.",
            )
            return self.graph.load(investigation.id)
        guard = BudgetGuard(investigation.budget)
        try:
            bundle = self.retrieval.search(query, session_id=investigation.session_id, guard=guard)
        except BudgetExceededError:
            return self._stop_budget(export, guard)
        investigation.budget = guard.budget
        self._record_search(investigation, bundle, network=not bundle.dry_run, planned_from=planned_from)
        self._ingest_bundle(export, bundle, guard)
        investigation.usage.followups_executed += 1
        actions = generate_actions(export.unresolved_questions, self.database.search_queries(investigation.id))
        export.actions = actions
        decision = evaluate_controller(
            build_request(
                investigation_id=investigation.id,
                question=investigation.question,
                questions=export.unresolved_questions,
                candidates=export.candidates,
                actions=actions,
                budget=investigation.budget,
                usage=investigation.usage,
            ),
            followups_executed=investigation.usage.followups_executed,
            url=self.convergence_url,
            post=self.post,
        )
        export.decision = decision
        investigation.decision_source = decision.decision_source
        investigation.decision_rationale = decision.rationale
        if decision.stop and decision.stop_reason:
            investigation.stop_reason = decision.stop_reason
        elif decision.decision_source == "fallback":
            investigation.stop_reason = "unresolved_uncertainty" if export.unresolved_questions else "no_useful_actions"
        investigation.status = "stopped" if investigation.stop_reason else "running"
        investigation.updated_at = _now()
        self._persist(export)
        self._record_convergence(export, decision)
        save_checkpoint(
            self.database,
            export,
            step=2,
            summary="Counterevidence search recorded on this branch. The parent assertion was not changed.",
            stop_reason=investigation.stop_reason,
        )
        return self.graph.load(investigation.id)

    def _record_fixture_stages(self, export: ExplorationExport, *, summary_prefix: str) -> None:
        self._event(
            export.investigation.id,
            "ingest",
            ingest_payload(export, "fixture_corpus"),
            f"Ingested {summary_prefix}. No web search was performed.",
        )
        if export.candidates:
            self._event(
                export.investigation.id,
                "ranking",
                {
                    "source": "fixture_corpus",
                    "candidates": ranking_rows(export, "fixture_corpus"),
                    "verified": False,
                },
                "Ranked the fixed corpus. Attention and knowledge value are not verification.",
            )
        payload = extraction_payload(
            claims=export.claims,
            spans=export.evidence_spans,
            links=export.evidence_links,
            relationships=export.relationships,
            provenance=export.provenance,
            llm_call=export.investigation.usage.llm_calls > 0,
            blocker=export.investigation.blocker,
        )
        if payload["passages"] or payload["blocker"]:
            self._event(
                export.investigation.id,
                "extraction",
                payload,
                "Extracted passages from the fixed corpus." if payload["passages"] else "No grounded passages were extracted.",
            )

    def _record_convergence(self, export: ExplorationExport, decision) -> None:
        unresolved = export.investigation.stop_reason == "unresolved_uncertainty"
        self._event(
            export.investigation.id,
            "convergence",
            {
                "stop": bool(decision.stop or export.investigation.status == "stopped"),
                "stop_reason": export.investigation.stop_reason,
                "decision_source": decision.decision_source,
                "rationale": decision.rationale,
                "alternatives": [
                    {
                        "id": action.id,
                        "query": action.query,
                        "reason": action.reason,
                        "utility": action.utility,
                        "already_executed": action.already_executed,
                    }
                    for action in export.actions
                ],
                "approved_query": decision.approved_action.query if decision.approved_action is not None else None,
                "unresolved_questions": [question.text for question in export.unresolved_questions],
                "unresolved": unresolved,
                "verified_success": False,
            },
            "Unresolved stop. This is not a verified answer."
            if unresolved
            else f"Recorded a {decision.decision_source} decision ({export.investigation.stop_reason or 'continue'}). Not a verified answer.",
        )

    def _event(self, investigation_id: str, stage: str, payload: dict, summary: str) -> None:
        record_event(self.database, investigation_id, stage, payload, summary=summary)

    def _persist(self, export: ExplorationExport) -> None:
        self.graph.save(export)


def _candidate(question: str, document: Document, version: DocumentVersion, questions: list[UnresolvedQuestion]) -> EvidenceCandidate:
    attention = score_passage(
        question,
        version.content[:1500],
        [item.text for item in questions],
        version.publish_date,
    )
    return EvidenceCandidate(
        id=stable_id("cand", document.id),
        document_id=document.id,
        url=document.canonical_url,
        title=document.title,
        snippet=version.content[:280].replace("\n", " "),
        providers=list(document.providers),
        attention=attention,
        publish_date=version.publish_date,
    )


def is_fixture_url(url: str) -> bool:
    return ".example/" in url or url.endswith(".example")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_seconds() -> float:
    return datetime.now(timezone.utc).timestamp()


def _elapsed_ms(started: float) -> int:
    return max(0, int((datetime.now(timezone.utc).timestamp() - started) * 1000))
