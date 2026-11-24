"""Investigation event log, branch manager, and profile knowledge map.

Nodes come from recorded events. A missing event is left missing. Attention
and knowledge value stay ranking signals and never verify a source.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.contracts import (
    ArtifactNote,
    BranchConstraints,
    BranchRecord,
    Checkpoint,
    Claim,
    CreateBranchRequest,
    Document,
    DocumentVersion,
    EvidenceCandidate,
    EvidenceLink,
    EvidenceSpan,
    ExplorationExport,
    Investigation,
    InvestigationBudget,
    InvestigationTrace,
    KnowledgeAssertionIn,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    Relationship,
    ResearchProfile,
    SourceProvenance,
    TraceEdge,
    TraceEvent,
    TraceNode,
    UnresolvedQuestion,
)

from exploration.backend.database import Database
from exploration.backend.ids import new_id, stable_id
from exploration.backend.planner import branch_query, is_grounded_objective

_ACTIVITY_NOTES = {
    "current_run": "Produced by a search, fetch, or ranking event recorded on this investigation.",
    "fixture_corpus": "Fixed corpus artifact. This run did not web-search or fetch it.",
    "reused_or_historical": "Present with no search, fetch, or LLM call recorded on this run. Inherited or historical, not current-run retrieval.",
    "unknown": "This artifact's activity was not recorded. It is unknown, not filled in.",
}


def record_event(
    database: Database,
    investigation_id: str,
    stage: str,
    payload: dict[str, Any],
    *,
    summary: str,
    checkpoint_id: str | None = None,
) -> str:
    event_id = new_id("evt")
    database.insert_event(
        event_id=event_id,
        investigation_id=investigation_id,
        stage=stage,
        summary=summary,
        payload=payload,
        created_at=datetime.now(timezone.utc),
        checkpoint_id=checkpoint_id,
    )
    return event_id


def ranking_rows(export: ExplorationExport, activity: str, candidates: list[EvidenceCandidate] | None = None) -> list[dict[str, Any]]:
    provenance = {item.document_id: item for item in export.provenance}
    rows: list[dict[str, Any]] = []
    for candidate in candidates if candidates is not None else export.candidates:
        knowledge = candidate.knowledge_value
        trigger = knowledge.bridge_trigger if knowledge is not None else None
        origin = provenance.get(candidate.document_id or "")
        rows.append(
            {
                "id": candidate.id,
                "document_id": candidate.document_id,
                "title": candidate.title,
                "url": candidate.url,
                "providers": list(candidate.providers),
                "snippet": candidate.snippet,
                "attention_score": candidate.attention.score,
                "knowledge_value_score": knowledge.score if knowledge is not None else None,
                # Which of the user's confirmations were part of the known set when scored.
                "knowledge_value_confirmed": list(knowledge.confirmed_known_assertion_ids) if knowledge is not None else [],
                "bridge_trigger": trigger,
                "bridge_status": "recorded" if trigger else "unknown",
                "origin_kind": origin.origin_kind if origin is not None else "unknown",
                "activity": activity,
                "epistemic_status": "unknown",
                "verification_basis": "none",
                "verified": False,
            }
        )
    return rows


def ingest_payload(export: ExplorationExport, source: str) -> dict[str, Any]:
    provenance = {item.document_id: item for item in export.provenance}
    documents = []
    for document in export.documents:
        origin = provenance.get(document.id)
        documents.append(
            {
                "id": document.id,
                "title": document.title,
                "url": document.canonical_url,
                "providers": list(document.providers),
                "origin_kind": origin.origin_kind if origin is not None else "unknown",
                "origin_id": origin.origin_id if origin is not None else document.origin_id,
            }
        )
    return {
        "source": source,
        "documents": documents,
        "search_calls": 0,
        "fetch_calls": 0,
        "llm_calls": 0,
        "verified": False,
    }


def extraction_payload(
    *,
    claims: list[Claim],
    spans: list[EvidenceSpan],
    links: list[EvidenceLink],
    relationships: list[Relationship],
    provenance: list[SourceProvenance],
    llm_call: bool,
    blocker: str | None = None,
) -> dict[str, Any]:
    span_by_id = {span.id: span for span in spans}
    provenance_by_doc = {item.document_id: item for item in provenance}
    passages = []
    for claim in claims:
        span = span_by_id.get(claim.span_id or "")
        related = [item for item in relationships if item.claim_id == claim.id]
        claim_links = [item for item in links if item.claim_id == claim.id]
        status = claim.status
        if any(item.status == "contradicted" for item in related) or status == "contradicted":
            epistemic = "contradicted"
            basis = "relationship_status" if related else "claim_status"
        elif status == "supported" or any(item.status == "supported" for item in related):
            epistemic = "supported"
            basis = "claim_status" if status == "supported" else "relationship_status"
        else:
            epistemic = "unknown"
            basis = "claim_status"
        origin = provenance_by_doc.get(claim.document_id or "")
        passages.append(
            {
                "claim_id": claim.id,
                "claim_text": claim.text,
                "status": status,
                "epistemic_status": epistemic,
                "verification_basis": basis,
                "verified": False,
                "passage": span.text if span is not None else None,
                "span_id": span.id if span is not None else None,
                "quote_valid": bool(span.quote_valid) if span is not None else False,
                "document_id": claim.document_id,
                "origin_kind": origin.origin_kind if origin is not None else "unknown",
                "origin_id": origin.origin_id if origin is not None else None,
                "provider": origin.provider if origin is not None else None,
                "stances": [item.stance for item in claim_links],
                "semantic_status": [item.semantic_status for item in claim_links],
                "used_by_relationship_ids": [item.id for item in related],
                "relationships": [
                    {
                        "id": item.id,
                        "predicate": item.predicate,
                        "subject": item.subject_name,
                        "object": item.object_name,
                        "status": item.status,
                    }
                    for item in related
                ],
            }
        )
    return {
        "passages": passages,
        "llm_call": llm_call,
        "blocker": blocker,
        "verified_success": False,
    }


def build_trace(database: Database, investigation_id: str) -> InvestigationTrace:
    export = database.load_export(investigation_id)
    events = database.list_events(investigation_id)
    branch = _incoming_branch(database, investigation_id)
    nodes, edges = _nodes_from_events(events)
    notes = _artifact_notes(export, events)
    search_events = sum(1 for event in events if event.stage == "search")
    fetch_events = sum(1 for event in events if event.stage == "retrieval")
    llm_events = sum(1 for event in events if event.payload.get("llm_call"))
    reasons: list[str] = []
    if not events:
        reasons.append(
            "No investigation events were recorded. Nothing is drawn, because a trace is not reconstructed from the final output."
        )
    usage = export.investigation.usage
    if usage.search_calls > search_events:
        reasons.append(
            "Usage counts searches that have no recorded search event. Those searches are not drawn."
        )
    if usage.llm_calls > llm_events and llm_events == 0 and usage.llm_calls:
        reasons.append(
            "Usage counts LLM calls that have no recorded extraction event. Those calls are not drawn."
        )
    metadata = export.investigation.metadata
    return InvestigationTrace(
        investigation_id=export.investigation.id,
        question=export.investigation.question,
        incomplete=bool(reasons),
        incomplete_reason=" ".join(reasons) or None,
        events=events,
        nodes=[] if not events else nodes,
        edges=[] if not events else edges,
        artifact_notes=notes,
        search_event_count=search_events,
        fetch_event_count=fetch_events,
        llm_event_count=llm_events,
        usage_search_calls=usage.search_calls,
        usage_fetch_calls=usage.fetch_calls,
        usage_llm_calls=usage.llm_calls,
        parent_investigation_id=metadata.get("parent_investigation_id") or (branch.parent_investigation_id if branch else None),
        branch_id=metadata.get("branch_id") or (branch.id if branch else None),
        intervention_type=metadata.get("intervention_type") or (branch.intervention_type if branch else None),
        from_checkpoint_id=metadata.get("branch_from_checkpoint_id") or (branch.from_checkpoint_id if branch else None),
        branch_objective=metadata.get("objective") if (metadata.get("branch_id") or branch) else None,
        attached_claims=list(metadata.get("attached_claims") or []),
        attached_sources=list(metadata.get("attached_sources") or []),
        root_question=resolve_root_question(database, export),
        lifecycle=lifecycle_of(export.investigation, is_branch=bool(metadata.get("branch_id") or branch)),
    )


def create_branch(orchestrator, parent_id: str, request: CreateBranchRequest) -> BranchRecord:
    """Open a child investigation from a checkpoint. The parent row is not written."""
    parent = orchestrator.get(parent_id)
    checkpoint = next((item for item in parent.checkpoints if item.id == request.from_checkpoint_id), None)
    if checkpoint is None:
        raise KeyError(request.from_checkpoint_id)
    snapshot = checkpoint.state_json.get("snapshot") or {}
    snap_claims = {item.get("id"): item for item in snapshot.get("claims") or [] if isinstance(item, dict)}
    snap_docs = {item.get("id"): item for item in snapshot.get("documents") or [] if isinstance(item, dict)}
    unknown_claims = [cid for cid in request.attached_claim_ids if cid not in snap_claims]
    unknown_sources = [did for did in request.attached_source_ids if did not in snap_docs]
    if unknown_claims or unknown_sources:
        raise ValueError(
            f"attachments not recorded at checkpoint {checkpoint.id}: "
            f"claims={unknown_claims} sources={unknown_sources}"
        )
    attached_claims = [
        {"id": cid, "text": snap_claims[cid].get("text", ""), "status": snap_claims[cid].get("status")}
        for cid in dict.fromkeys(request.attached_claim_ids)
    ]
    attached_sources = [
        {"id": did, "title": snap_docs[did].get("title", ""), "url": snap_docs[did].get("canonical_url", "")}
        for did in dict.fromkeys(request.attached_source_ids)
    ]
    parent_events = [event.id for event in orchestrator.database.list_events(parent_id)]
    parent_questions = orchestrator.database.search_queries(parent_id)
    objective = request.objective.strip()
    if (
        request.intervention_type == "challenge_evidence"
        and not attached_claims
        and not is_grounded_objective(objective)
    ):
        raise ValueError(
            "A challenge needs a selected claim or an objective that names what to test. "
            f"{objective!r} is not specific enough to search for on its own."
        )
    # The child keeps the ROOT research question for every intervention. The objective is
    # stored separately (branches.objective, metadata.objective) and never replaces it.
    root_question = resolve_root_question(orchestrator, parent)
    question = root_question
    constraints = request.constraints or BranchConstraints()
    parent_budget = parent.investigation.budget
    budget = InvestigationBudget(
        max_searches=constraints.max_searches if constraints.max_searches is not None else parent_budget.max_searches,
        max_fetched_urls=constraints.max_fetched_urls if constraints.max_fetched_urls is not None else parent_budget.max_fetched_urls,
        max_llm_calls=constraints.max_llm_calls if constraints.max_llm_calls is not None else parent_budget.max_llm_calls,
    )
    profile = parent.profile
    child = orchestrator.create(question, budget, mode="branch", profile=profile)
    now = datetime.now(timezone.utc)
    branch = BranchRecord(
        id=new_id("branch"),
        parent_investigation_id=parent_id,
        child_investigation_id=child.id,
        from_checkpoint_id=checkpoint.id,
        intervention_type=request.intervention_type,
        objective=objective,
        created_at=now,
        constraints=constraints,
    )
    orchestrator.database.insert_branch(branch)
    child.metadata.update(
        {
            "parent_investigation_id": parent_id,
            "branch_id": branch.id,
            "branch_from_checkpoint_id": checkpoint.id,
            "intervention_type": request.intervention_type,
            "objective": objective,
            "objective_grounded": is_grounded_objective(objective),
            "root_question": root_question,
            "parent_question": parent.investigation.question,
            "lifecycle": "saved",
            "decision_is_inherited": False,
            "attached_claims": attached_claims,
            "attached_sources": attached_sources,
        }
    )
    from exploration.backend.checkpoints import save_checkpoint

    export, inherited_decision = _materialize_child(child, parent, checkpoint)
    # Saved, not started: no search has run on this branch yet.
    child.status = "created"
    branch_focus = _branch_focus_question(child.id, objective, attached_claims, request.intervention_type)
    if branch_focus is not None:
        export.unresolved_questions = [branch_focus, *export.unresolved_questions]
    if export.profile is not None:
        child.metadata["profile_snapshot"] = export.profile.model_dump(mode="json")
    export.investigation = child
    orchestrator._persist(export)
    save_checkpoint(
        orchestrator.database,
        export,
        step=1,
        summary=(
            f"Inherited checkpoint {checkpoint.id} via {request.intervention_type}. "
            "The parent investigation was not modified."
        ),
        stop_reason=None,
        extra={
            "inherited": True,
            "parent_investigation_id": parent_id,
            "from_checkpoint_id": checkpoint.id,
            "intervention_type": request.intervention_type,
            "objective": objective,
            "inherited_decision": inherited_decision,
            "inherited_stop_reason": checkpoint.stop_reason,
            "verified_success": False,
        },
    )
    if orchestrator.database.list_events(parent_id) and [event.id for event in orchestrator.database.list_events(parent_id)] != parent_events:
        raise RuntimeError("branch creation changed the parent event log")
    if orchestrator.database.search_queries(parent_id) != parent_questions:
        raise RuntimeError("branch creation changed the parent search history")
    return branch


def resolve_root_question(source, export: ExplorationExport) -> str:
    """The original research question at the top of a lineage.

    New branches store ``metadata.root_question``. Legacy branches (created before 1.1.0)
    overwrote ``question`` with their objective, so walk parent links to the root instead.
    """
    load = source.load_export if isinstance(source, Database) else source.get
    current = export
    for _ in range(64):
        metadata = current.investigation.metadata or {}
        if metadata.get("root_question"):
            return str(metadata["root_question"])
        parent_id = metadata.get("parent_investigation_id")
        if not parent_id:
            return current.investigation.question
        try:
            current = load(parent_id)
        except Exception:  # missing parent row: the lineage is broken, use what we have
            return current.investigation.question
    return current.investigation.question


def lifecycle_of(investigation, *, is_branch: bool | None = None) -> str:
    """saved | queued | researching | stopped | completed | failed. Separate from stop_reason.

    Derived from the stored record so legacy rows (which stored saved branches as
    ``running``) are reported truthfully.
    """
    metadata = investigation.metadata or {}
    explicit = metadata.get("lifecycle")
    if explicit in {"failed", "queued"} and investigation.status not in {"stopped"}:
        return explicit
    if is_branch is None:
        is_branch = bool(metadata.get("branch_id") or metadata.get("parent_investigation_id"))
    if investigation.status == "blocked":
        return "failed"
    if investigation.status == "stopped":
        return "completed" if investigation.stop_reason == "evidence_sufficient" else "stopped"
    if investigation.status == "created":
        return "saved"
    if investigation.usage.search_calls == 0 and is_branch:
        return "saved"
    return "researching"


def _branch_focus_question(child_id, objective, attached_claims, intervention_type):
    """An open question that steers the branch's follow-up planning, when one is grounded."""
    if intervention_type == "continue":
        return None
    query, _planned = branch_query(
        root_question="",
        objective=objective,
        attached_claims=attached_claims,
        intervention_type=intervention_type,
    )
    if not query:
        return None
    return UnresolvedQuestion(
        id=stable_id("uq", child_id, "branch_objective"),
        text=query,
        importance=0.9,
        missing_evidence=1.0,
        kind="branch_objective",
    )


def resume_branch(orchestrator, branch_id: str) -> ExplorationExport:
    branch = orchestrator.database.get_branch(branch_id)
    if branch is None:
        raise KeyError(branch_id)
    return orchestrator.step(branch.child_investigation_id)


def knowledge_graph(
    database: Database,
    *,
    investigation_id: str | None = None,
    profile_id: str | None = None,
) -> KnowledgeGraph:
    export = None
    profile = None
    question = None
    if investigation_id:
        export = database.load_export(investigation_id)
        question = export.investigation.question
        profile = export.profile
        if profile is None and export.investigation.metadata.get("profile_snapshot"):
            profile = ResearchProfile.model_validate(export.investigation.metadata["profile_snapshot"])
        profile_id = profile.id if profile is not None else profile_id
    assertions = database.list_assertions(profile_id) if profile_id else []
    notes = [
        "Knowledge value is a ranking signal. A high score is not learning, gap resolution, or source truth.",
        "Encountered material was retrieved or presented. It is not marked known.",
        "Supported investigation claims stay on the investigation. They are not copied here as user knowledge.",
    ]
    nodes: list[KnowledgeNode] = []
    edges: list[KnowledgeEdge] = []
    if profile is None and not assertions:
        notes.append("No stored profile and no confirmed assertions. Nothing is inferred.")
        return KnowledgeGraph(
            profile_id=profile_id,
            investigation_id=investigation_id,
            question=question,
            notes=notes,
        )
    if profile is not None:
        for interest in profile.interests:
            nodes.append(
                KnowledgeNode(
                    id=stable_id("kn", profile.id, "interest", interest),
                    label=interest,
                    epistemic_state="interested",
                    basis="profile_declared",
                    confirmed=True,
                    inferred=False,
                    note="Declared on the research profile.",
                    investigation_id=investigation_id,
                )
            )
        for claim in profile.known_claims:
            node_id = stable_id("kn", profile.id, "prior", claim.id)
            nodes.append(
                KnowledgeNode(
                    id=node_id,
                    label=claim.text or f"{claim.subject} {claim.predicate} {claim.object}",
                    epistemic_state="known",
                    basis="profile_prior",
                    confirmed=False,
                    inferred=False,
                    note="Profile prior. Not sourced web evidence and not confirmed in this session.",
                    investigation_id=investigation_id,
                )
            )
            edges.append(
                KnowledgeEdge(
                    id=stable_id("ke", node_id, claim.predicate),
                    source=stable_id("kn", profile.id, "interest", claim.subject) if any(
                        interest.casefold() == claim.subject.casefold() for interest in profile.interests
                    ) else node_id,
                    target=node_id,
                    label=f"{claim.subject} {claim.predicate} {claim.object}",
                    basis="profile_prior",
                )
            )
        for interest in profile.unresolved_interests:
            nodes.append(
                KnowledgeNode(
                    id=stable_id("kn", profile.id, "frontier", interest),
                    label=interest,
                    epistemic_state="frontier",
                    basis="profile_unresolved",
                    confirmed=False,
                    inferred=True,
                    note="Listed on the profile as unresolved. Not a computed frontier and not a resolved gap.",
                    investigation_id=investigation_id,
                )
            )
    if export is not None:
        for document in export.documents:
            nodes.append(
                KnowledgeNode(
                    id=stable_id("kn", export.investigation.id, "encountered", document.id),
                    label=document.title or document.canonical_url,
                    epistemic_state="encountered",
                    basis="presented_in_investigation",
                    confirmed=False,
                    inferred=False,
                    note="Presented in this investigation. Encountered is not known.",
                    investigation_id=export.investigation.id,
                )
            )
        for item in export.contradictions:
            nodes.append(
                KnowledgeNode(
                    id=stable_id("kn", export.investigation.id, "uncertain", item.id),
                    label=f"{item.subject_name} {item.predicate}: {item.supported_object} vs {item.conflicting_object}",
                    epistemic_state="uncertain",
                    basis="contradiction",
                    confirmed=False,
                    inferred=False,
                    note=item.detail,
                    investigation_id=export.investigation.id,
                )
            )
    confirmed_keys = set()
    # A later user assertion about the same (subject, predicate, object) is a correction of the
    # earlier one: keep only the latest per key. All rows stay stored (audit trail).
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for assertion in assertions:
        key = (
            assertion["subject"].casefold(),
            assertion["predicate"].casefold(),
            assertion["object"].casefold(),
        )
        latest.pop(key, None)
        latest[key] = assertion
    superseded = len(assertions) - len(latest)
    if superseded:
        notes.append(f"{superseded} earlier assertion(s) were superseded by a later correction and are not shown.")
    assertions = list(latest.values())
    for assertion in assertions:
        state = assertion["epistemic_state"]
        confirmed = assertion["basis"] == "user_confirmation"
        node_id = assertion["id"]
        key = (
            assertion["subject"].casefold(),
            assertion["predicate"].casefold(),
            assertion["object"].casefold(),
        )
        if confirmed and state == "known":
            confirmed_keys.add(key)
        nodes.append(
            KnowledgeNode(
                id=node_id,
                label=assertion["text"] or f"{assertion['subject']} {assertion['predicate']} {assertion['object']}",
                epistemic_state=state,
                basis=assertion["basis"],
                confirmed=confirmed,
                inferred=assertion["basis"] == "inferred",
                note="Explicit assertion recorded by the user." if confirmed else "Stored assertion.",
                investigation_id=assertion.get("investigation_id"),
            )
        )
        if assertion["object"] and assertion["subject"].casefold() != assertion["object"].casefold():
            edges.append(
                KnowledgeEdge(
                    id=stable_id("ke", node_id, "assert"),
                    source=node_id,
                    target=node_id,
                    label=f"{assertion['subject']} {assertion['predicate']} {assertion['object']}",
                    basis=assertion["basis"],
                )
            )
    node_ids = {node.id for node in nodes}
    edges = [edge for edge in edges if edge.source != edge.target and edge.source in node_ids and edge.target in node_ids]
    if profile is not None and confirmed_keys:
        for claim in profile.known_claims:
            key = (claim.subject.casefold(), claim.predicate.casefold(), claim.object.casefold())
            if key not in confirmed_keys:
                continue
            for node in nodes:
                if node.basis == "profile_prior" and claim.text and claim.text in node.label:
                    node.confirmed = True
                    node.note = "Profile prior confirmed by an explicit user assertion."
    return KnowledgeGraph(
        profile_id=profile_id,
        investigation_id=investigation_id,
        question=question,
        nodes=nodes,
        edges=edges,
        notes=notes,
    )


def add_assertion(database: Database, body: KnowledgeAssertionIn) -> KnowledgeGraph:
    database.insert_assertion(new_id("assert"), body, datetime.now(timezone.utc))
    return knowledge_graph(database, investigation_id=body.investigation_id, profile_id=body.profile_id)


def _incoming_branch(database: Database, investigation_id: str) -> BranchRecord | None:
    for branch in database.list_branches(investigation_id):
        if branch.child_investigation_id == investigation_id:
            return branch
    return None


def _nodes_from_events(events: list[TraceEvent]) -> tuple[list[TraceNode], list[TraceEdge]]:
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    previous: str | None = None
    for event in events:
        if event.stage == "extraction":
            anchor = _node(
                event,
                node_id=event.id,
                node_type="extraction",
                label="Extraction",
                epistemic="unknown",
                basis="none",
            )
            nodes.append(anchor)
            previous = _link(edges, previous, anchor.id)
            for index, passage in enumerate(event.payload.get("passages") or []):
                evidence = _node(
                    event,
                    node_id=f"{event.id}:p{index}",
                    node_type="evidence",
                    label=_short(passage.get("claim_text") or "Evidence passage", 90),
                    epistemic=passage.get("epistemic_status") or "unknown",
                    basis=passage.get("verification_basis") or "claim_status",
                    payload=passage,
                )
                nodes.append(evidence)
                edges.append(
                    TraceEdge(
                        id=f"e_{anchor.id}_{evidence.id}",
                        source=anchor.id,
                        target=evidence.id,
                        kind="produced",
                    )
                )
            continue
        node_type, label, epistemic, basis, payload = _stage_node(event)
        node = _node(
            event,
            node_id=event.id,
            node_type=node_type,
            label=label,
            epistemic=epistemic,
            basis=basis,
            payload=payload,
        )
        nodes.append(node)
        previous = _link(edges, previous, node.id)
    return nodes, edges


def _stage_node(event: TraceEvent) -> tuple[str, str, str, str, dict[str, Any]]:
    payload = dict(event.payload)
    payload["verified_success"] = False
    if event.stage == "search":
        query = str(payload.get("query") or event.summary)
        return "search", _short(query, 90), "unknown", "none", payload
    if event.stage == "retrieval":
        pages = payload.get("pages") or []
        cached = sum(1 for page in pages if page.get("from_cache"))
        label = f"Retrieved {len(pages)} page(s)"
        if cached:
            label = f"Retrieved {len(pages)} page(s), {cached} from cache"
        return "retrieval", label, "unknown", "none", payload
    if event.stage == "ranking":
        count = len(payload.get("candidates") or [])
        return "ranking", f"Ranked {count} candidate(s)", "unknown", "none", payload
    if event.stage == "convergence":
        unresolved = bool(payload.get("unresolved") or payload.get("stop_reason") == "unresolved_uncertainty")
        payload["unresolved"] = unresolved
        payload["verified_success"] = False
        if unresolved:
            return "convergence", "Unresolved stop", "unresolved", "none", payload
        reason = payload.get("stop_reason") or "continue"
        return "convergence", f"Decision: {reason}", "unknown", "none", payload
    if event.stage == "checkpoint":
        unresolved = bool(payload.get("unresolved") or payload.get("stop_reason") == "unresolved_uncertainty")
        payload["unresolved"] = unresolved
        payload["verified_success"] = False
        label = "Checkpoint"
        if payload.get("inherited"):
            label = "Inherited checkpoint"
        elif unresolved:
            label = "Checkpoint · unresolved"
        return "checkpoint", label, "unresolved" if unresolved else "unknown", "none", payload
    if event.stage == "ingest":
        source = payload.get("source") or "ingest"
        label = "Fixture corpus" if source == "fixture_corpus" else "Ingest"
        return "ingest", label, "unknown", "none", payload
    if event.stage == "blocked":
        return "blocked", _short(str(payload.get("blocker") or event.summary), 90), "unresolved", "none", payload
    return event.stage, _short(event.summary, 90), "unknown", "none", payload


def _node(
    event: TraceEvent,
    *,
    node_id: str,
    node_type: str,
    label: str,
    epistemic: str,
    basis: str,
    payload: dict[str, Any] | None = None,
) -> TraceNode:
    if epistemic not in {"supported", "contradicted", "unknown", "unresolved"}:
        epistemic = "unknown"
    if basis not in {"relationship_status", "claim_status", "none"}:
        basis = "none"
    return TraceNode(
        id=node_id,
        type=node_type,
        label=label,
        stage=event.stage,
        event_id=event.id,
        epistemic_status=epistemic,  # type: ignore[arg-type]
        verification_basis=basis,  # type: ignore[arg-type]
        payload=payload if payload is not None else dict(event.payload),
    )


def _link(edges: list[TraceEdge], previous: str | None, target: str) -> str:
    if previous is not None:
        edges.append(TraceEdge(id=f"e_{previous}_{target}", source=previous, target=target, kind="sequence"))
    return target


def _artifact_notes(export: ExplorationExport, events: list[TraceEvent]) -> list[ArtifactNote]:
    stamped: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.stage != "ranking":
            continue
        for row in event.payload.get("candidates") or []:
            if row.get("id"):
                stamped[row["id"]] = row
    provenance = {item.document_id: item for item in export.provenance}
    notes: list[ArtifactNote] = []
    for candidate in export.candidates:
        row = stamped.get(candidate.id)
        knowledge = candidate.knowledge_value
        if row:
            activity = row.get("activity") or "unknown"
            origin = row.get("origin_kind") or "unknown"
            trigger = row.get("bridge_trigger")
            bridge_status = row.get("bridge_status") or ("unknown" if not trigger else "recorded")
            attention = row.get("attention_score")
            knowledge_score = row.get("knowledge_value_score")
        else:
            origin_row = provenance.get(candidate.document_id or "")
            origin = origin_row.origin_kind if origin_row is not None else "unknown"
            trigger = knowledge.bridge_trigger if knowledge is not None else None
            bridge_status = "unknown" if not trigger else "recorded"
            attention = candidate.attention.score
            knowledge_score = knowledge.score if knowledge is not None else None
            activity = "reused_or_historical"
        if activity not in {"current_run", "reused_or_historical", "fixture_corpus", "unknown"}:
            activity = "unknown"
        notes.append(
            ArtifactNote(
                kind="candidate",
                id=candidate.id,
                title=candidate.title,
                url=candidate.url,
                activity=activity,  # type: ignore[arg-type]
                origin_kind=origin or "unknown",
                bridge_status=bridge_status,
                bridge_trigger=trigger,
                attention_score=attention,
                knowledge_value_score=knowledge_score,
                epistemic_status="unknown",
                verified=False,
                note=_ACTIVITY_NOTES.get(activity, _ACTIVITY_NOTES["unknown"]),
            )
        )
    return notes


def _materialize_child(
    child: Investigation,
    parent: ExplorationExport,
    checkpoint: Checkpoint,
) -> tuple[ExplorationExport, dict[str, Any] | None]:
    snapshot = checkpoint.state_json.get("snapshot")
    inherited_decision = None
    if isinstance(checkpoint.state_json.get("decision"), dict):
        inherited_decision = checkpoint.state_json["decision"]
    if isinstance(snapshot, dict) and snapshot.get("decision"):
        inherited_decision = snapshot["decision"]
    if not isinstance(snapshot, dict):
        child.metadata["checkpoint_snapshot_incomplete"] = True
        export = _partial_inheritance(child, parent, checkpoint)
        export.investigation = child
        return export, inherited_decision
    child.metadata["checkpoint_snapshot_incomplete"] = False
    export = _export_from_snapshot(child, snapshot)
    export.investigation = child
    return export, inherited_decision


def _partial_inheritance(child: Investigation, parent: ExplorationExport, checkpoint: Checkpoint) -> ExplorationExport:
    """Copy only immutable texts named by an old checkpoint. Do not invent claims."""
    wanted = {
        (item.get("document_id"), item.get("content_hash"))
        for item in checkpoint.state_json.get("version_hashes") or []
        if isinstance(item, dict)
    }
    versions = [
        version
        for version in parent.document_versions
        if (version.document_id, version.content_hash) in wanted
    ]
    doc_ids = {version.document_id for version in versions}
    documents = [document for document in parent.documents if document.id in doc_ids]
    questions = [
        UnresolvedQuestion.model_validate(item)
        for item in checkpoint.state_json.get("unresolved_questions") or []
        if isinstance(item, dict)
    ]
    profile = parent.profile
    return _retarget(
        child,
        documents=documents,
        versions=versions,
        spans=[],
        claims=[],
        relationships=[],
        links=[],
        provenance=[item for item in parent.provenance if item.document_id in doc_ids],
        questions=questions,
        candidates=[],
        contradictions=[],
        clusters=[],
        entities=[],
        profile=profile,
    )


def _export_from_snapshot(child: Investigation, snapshot: dict[str, Any]) -> ExplorationExport:
    profile = ResearchProfile.model_validate(snapshot["profile"]) if snapshot.get("profile") else None
    return _retarget(
        child,
        documents=[Document.model_validate(item) for item in snapshot.get("documents") or []],
        versions=[DocumentVersion.model_validate(item) for item in snapshot.get("document_versions") or []],
        spans=[EvidenceSpan.model_validate(item) for item in snapshot.get("evidence_spans") or []],
        claims=[Claim.model_validate(item) for item in snapshot.get("claims") or []],
        relationships=[Relationship.model_validate(item) for item in snapshot.get("relationships") or []],
        links=[EvidenceLink.model_validate(item) for item in snapshot.get("evidence_links") or []],
        provenance=[SourceProvenance.model_validate(item) for item in snapshot.get("provenance") or []],
        questions=[UnresolvedQuestion.model_validate(item) for item in snapshot.get("unresolved_questions") or []],
        candidates=[EvidenceCandidate.model_validate(item) for item in snapshot.get("candidates") or []],
        contradictions=[_validate_model("contradiction", item) for item in snapshot.get("contradictions") or []],
        clusters=[_validate_model("cluster", item) for item in snapshot.get("origin_clusters") or []],
        entities=[_validate_model("entity", item) for item in snapshot.get("entities") or []],
        profile=profile,
    )


def _validate_model(kind: str, item: dict[str, Any]):
    from shared.contracts import Contradiction, Entity, OriginCluster

    if kind == "contradiction":
        return Contradiction.model_validate(item)
    if kind == "cluster":
        return OriginCluster.model_validate(item)
    return Entity.model_validate(item)


def _retarget(child, **parts) -> ExplorationExport:
    doc_map = {document.id: stable_id("doc", child.id, document.id) for document in parts["documents"]}
    ver_map = {version.id: stable_id("ver", child.id, version.id) for version in parts["versions"]}

    def doc(value: str | None) -> str | None:
        if value is None:
            return None
        return doc_map.get(value, value)

    documents = []
    for document in parts["documents"]:
        documents.append(
            document.model_copy(
                update={
                    "id": doc_map[document.id],
                    "origin_id": document.origin_id,
                }
            )
        )
    versions = []
    for version in parts["versions"]:
        versions.append(
            version.model_copy(
                update={
                    "id": ver_map[version.id],
                    "document_id": doc_map.get(version.document_id, version.document_id),
                }
            )
        )
    spans = []
    for span in parts["spans"]:
        spans.append(
            span.model_copy(
                update={
                    "version_id": ver_map.get(span.version_id, span.version_id),
                    "document_id": doc_map.get(span.document_id, span.document_id),
                }
            )
        )
    claims = []
    for claim in parts["claims"]:
        claims.append(claim.model_copy(update={"document_id": doc(claim.document_id)}))
    relationships = []
    for item in parts["relationships"]:
        relationships.append(
            item.model_copy(
                update={
                    "document_id": doc(item.document_id),
                    "source_document_ids": [doc_map.get(value, value) for value in item.source_document_ids],
                }
            )
        )
    provenance = []
    for item in parts["provenance"]:
        provenance.append(item.model_copy(update={"document_id": doc_map.get(item.document_id, item.document_id)}))
    candidates = []
    for item in parts["candidates"]:
        candidates.append(item.model_copy(update={"document_id": doc(item.document_id)}))
    contradictions = []
    for item in parts["contradictions"]:
        contradictions.append(
            item.model_copy(
                update={
                    "original_document_id": doc_map.get(item.original_document_id, item.original_document_id),
                    "conflicting_document_id": doc_map.get(item.conflicting_document_id, item.conflicting_document_id),
                }
            )
        )
    clusters = []
    for item in parts["clusters"]:
        clusters.append(
            item.model_copy(
                update={
                    "document_ids": [doc_map.get(value, value) for value in item.document_ids],
                    "original_document_ids": [doc_map.get(value, value) for value in item.original_document_ids],
                    "derivative_document_ids": [doc_map.get(value, value) for value in item.derivative_document_ids],
                }
            )
        )
    child.status = "running"
    child.stop_reason = None
    child.decision_source = None
    child.decision_rationale = ""
    return ExplorationExport(
        investigation=child,
        documents=documents,
        document_versions=versions,
        evidence_spans=spans,
        entities=list(parts["entities"]),
        claims=claims,
        relationships=relationships,
        evidence_links=list(parts["links"]),
        provenance=provenance,
        unresolved_questions=list(parts["questions"]),
        candidates=candidates,
        contradictions=contradictions,
        origin_clusters=clusters,
        profile=parts["profile"],
    )


def _short(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"
