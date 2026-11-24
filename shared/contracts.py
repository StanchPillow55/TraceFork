"""Pydantic v2 contract shared by exploration and other SearchHax apps.

``CONTRACT_VERSION`` is 1.2.0 (1.1.0 adds root_question and lifecycle; 1.2.0 adds CreateInvestigationRequest.profile_from_investigation_id; 1.3.0 adds
KnowledgeValue.confirmed_known_assertion_ids; all additive). Add fields in a backward-compatible way (new
optional fields, ``extra="ignore"``) instead of renaming or removing models.

``ConvergenceDecision.decision_source`` is recorded by exploration after a
controller call. ``convergence`` means the HTTP endpoint returned a usable
decision. ``fallback`` means exploration applied its own deterministic policy
because the endpoint was down or the payload could not be used. A fallback
decision is not a convergence decision.

Attention scores are retrieval priority, not factual confidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.3.0"

VerificationStatus = Literal["supported", "contradicted", "insufficient", "hypothesis"]
StopReason = Literal[
    "evidence_sufficient",
    "low_expected_value",
    "no_useful_actions",
    "budget_exhausted",
    "unresolved_uncertainty",
]
DecisionSource = Literal["convergence", "fallback"]
EvidenceStance = Literal["supports", "contradicts", "mentions", "insufficient"]
OriginKind = Literal["original", "derivative", "unknown"]
InvestigationStatus = Literal["created", "running", "stopped", "blocked"]
# Research lifecycle (1.1.0). Derived from status, stop_reason and usage; stored in
# Investigation.metadata["lifecycle"]. "saved" = a branch that has not run any research.
Lifecycle = Literal["saved", "queued", "researching", "stopped", "completed", "failed"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AttentionComponents(_Model):
    """Passage priority for retrieval. This is not factual confidence and not a truth score."""

    R: float = Field(ge=0, le=1, description="Query-token relevance in [0,1]. Not factual confidence.")
    G: float = Field(ge=0, le=1, description="Overlap with unresolved gaps in [0,1]. Not factual confidence.")
    D: float = Field(ge=0, le=1, description="Domain-fit or bridge-term score in [0,1]. Not factual confidence.")
    F: float = Field(ge=0, le=1, description="Recency from publish_date in [0,1]. Not factual confidence.")
    score: float = Field(
        ge=0,
        le=1,
        description="A(q,p)=0.4*R+0.3*G+0.2*D+0.1*F, clamped to [0,1]. Not factual confidence.",
    )


class KnowledgeValue(_Model):
    """Additive knowledge-value heuristic. Not query attention, not a probability, and not factual confidence."""

    R: float = Field(ge=0, le=1, description="Overlap with profile interests and goals in [0,1]. Not factual confidence.")
    N: float = Field(ge=0, le=1, description="Share of passage tokens absent from known claims in [0,1]. Not factual confidence.")
    B: float = Field(
        ge=0,
        le=1,
        description=(
            "1 only when a profile interest and a DOMAIN_BRIDGES bridge term both occur "
            "and the bridge term is outside known claims; otherwise 0. Not factual confidence."
        ),
    )
    I: float = Field(ge=0, le=1, description="Overlap with the unresolved profile interest in [0,1]. Not factual confidence.")
    C: float = Field(
        ge=0,
        le=1,
        description=(
            "1 only when the passage contradicts a known claim with the same subject and predicate "
            "and a different object; otherwise 0. Not factual confidence."
        ),
    )
    score: float = Field(
        ge=0,
        le=1,
        description="U=0.25*R+0.25*N+0.20*B+0.20*I+0.10*C, clamped to [0,1]. A heuristic, not a probability and not factual confidence.",
    )
    bridge_trigger: str | None = None
    bridge_terms: list[str] = Field(default_factory=list)
    confirmed_known_assertion_ids: list[str] = Field(
        default_factory=list,
        description=(
            "User assertions (basis user_confirmation, state known) that were part of the known set when this "
            "score was recorded (1.3.0, additive). Empty for scores recorded before 1.3.0 or without confirmations."
        ),
    )


class KnownClaim(_Model):
    """A prior the profile already holds. This is not sourced web evidence."""

    id: str
    subject: str
    predicate: str
    object: str
    text: str = ""


class ResearchProfile(_Model):
    """Static read-only research profile. Not an account and not web evidence."""

    id: str
    interests: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    known_claims: list[KnownClaim] = Field(default_factory=list)
    unresolved_interests: list[str] = Field(default_factory=list)
    notes: str = ""


class InvestigationBudget(_Model):
    """Search budget. The exploration backend clamps enforcement to 2 searches, 5 fetched URLs, and 10 LLM calls."""

    max_searches: int = 2
    max_fetched_urls: int = 5
    max_llm_calls: int = 10
    searches_used: int = 0
    urls_fetched: int = 0
    llm_calls_used: int = 0
    min_utility: float = 0.15
    remaining_searches: int = 2
    remaining_fetched_urls: int = 5
    remaining_llm_calls: int = 10

    @model_validator(mode="after")
    def sync_remaining(self) -> InvestigationBudget:
        self.remaining_searches = max(0, self.max_searches - self.searches_used)
        self.remaining_fetched_urls = max(0, self.max_fetched_urls - self.urls_fetched)
        self.remaining_llm_calls = max(0, self.max_llm_calls - self.llm_calls_used)
        return self


class UsageMetrics(_Model):
    """Measured usage. Null token and cost fields mean the API did not report them."""

    session_id: str | None = None
    interaction_ids: list[str] = Field(default_factory=list)
    search_calls: int = 0
    provider_calls: int = 0
    urls_fetched: int = 0
    fetch_calls: int = 0
    llm_calls: int = 0
    llm_tokens: int | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    providers_used: list[str] = Field(default_factory=list)
    network_calls: int = 0
    cache_hits: int = 0
    corpus_documents: int = 0
    followups_executed: int = 0
    rejected_ungrounded_citations: int = 0
    dry_run: bool = False
    configuration: str = ""


class SourceProvenance(_Model):
    id: str
    document_id: str
    provider: str | None = None
    providers: list[str] = Field(default_factory=list)
    origin_url: str | None = None
    origin_id: str | None = None
    origin_kind: OriginKind = "unknown"
    cited_origin_ids: list[str] = Field(default_factory=list)
    shared_origin_group: str | None = None
    notes: str = ""


class Document(_Model):
    id: str
    canonical_url: str
    title: str
    origin_id: str | None = None
    providers: list[str] = Field(default_factory=list)


class DocumentVersion(_Model):
    """Immutable fetched text. A new hash is a new version; an existing hash is never rewritten."""

    id: str
    document_id: str
    content_hash: str
    fetched_at: datetime
    content: str
    publish_date: str | None = None


class EvidenceSpan(_Model):
    id: str
    version_id: str
    document_id: str
    text: str
    start: int = Field(description="Inclusive character offset into the stored document version.")
    end: int = Field(description="Exclusive character offset into the stored document version.")
    content_hash: str
    attention: AttentionComponents | None = Field(
        default=None,
        description="Retrieval priority for this passage. Not factual confidence.",
    )
    quote_valid: bool = False


class Entity(_Model):
    id: str
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)


class Claim(_Model):
    id: str
    text: str
    scope: str = ""
    status: VerificationStatus
    document_id: str | None = None
    span_id: str | None = None


class EvidenceLink(_Model):
    """A quotation can be valid and still fail to semantically support a claim."""

    id: str
    claim_id: str
    relationship_id: str | None = None
    evidence_span_id: str
    stance: EvidenceStance
    quote_valid: bool
    semantic_status: VerificationStatus


class Relationship(_Model):
    id: str
    subject_id: str
    predicate: str
    object_id: str
    claim_id: str | None = None
    kind: str = "factual"
    status: VerificationStatus
    document_id: str | None = None
    subject_name: str = ""
    object_name: str = ""
    evidence_link_ids: list[str] = Field(default_factory=list)
    source_document_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class UnresolvedQuestion(_Model):
    id: str
    text: str
    importance: float = Field(ge=0, le=1, default=0.5)
    missing_evidence: float = Field(ge=0, le=1, default=1.0)
    related_entity_ids: list[str] = Field(default_factory=list)
    kind: str = "gap"


class InvestigationAction(_Model):
    id: str
    query: str
    reason: str
    target_question_id: str | None = None
    utility: float = 0.0
    already_executed: bool = False
    result_summary: str = ""


class EvidenceCandidate(_Model):
    id: str
    document_id: str | None = None
    url: str
    title: str
    snippet: str
    providers: list[str] = Field(default_factory=list)
    attention: AttentionComponents
    knowledge_value: KnowledgeValue | None = Field(
        default=None,
        description="Optional knowledge-value score. Distinct from attention, which stays the query-retrieval score.",
    )
    publish_date: str | None = None


class ConvergenceDecision(_Model):
    """Decision sent to and received from the convergence controller.

    Exploration overwrites ``decision_source`` after the call. Do not treat a
    fallback rationale as a controller verdict.
    """

    investigation_id: str | None = None
    approved_action: InvestigationAction | None = None
    stop: bool = False
    stop_reason: StopReason | None = None
    rationale: str
    decision_source: DecisionSource = Field(
        description="Recorded by exploration: convergence if the endpoint was used, otherwise fallback."
    )
    include_document_ids: list[str] = Field(default_factory=list)
    archive_document_ids: list[str] = Field(default_factory=list)


class Checkpoint(_Model):
    id: str
    investigation_id: str
    step: int
    state_json: dict[str, Any] = Field(default_factory=dict)
    stop_reason: StopReason | None = None
    created_at: datetime
    summary: str = ""


class Investigation(_Model):
    id: str
    question: str = Field(description="Immutable original research question.")
    budget: InvestigationBudget
    status: InvestigationStatus = "created"
    stop_reason: StopReason | None = None
    decision_source: DecisionSource | None = None
    decision_rationale: str = ""
    session_id: str | None = None
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    created_at: datetime
    updated_at: datetime
    live_extraction_blocked: bool = False
    blocker: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Contradiction(_Model):
    id: str
    subject_name: str
    predicate: str
    supported_object: str
    conflicting_object: str
    original_document_id: str
    conflicting_document_id: str
    conflicting_relationship_id: str
    detail: str


class OriginCluster(_Model):
    origin_id: str
    document_ids: list[str]
    original_document_ids: list[str]
    derivative_document_ids: list[str]


class ConvergenceEvaluateRequest(_Model):
    """Body exploration POSTs to the convergence controller."""

    contract_version: str = CONTRACT_VERSION
    investigation_id: str
    original_question: str
    open_questions: list[UnresolvedQuestion] = Field(default_factory=list)
    candidates: list[EvidenceCandidate] = Field(default_factory=list)
    proposed_actions: list[InvestigationAction] = Field(default_factory=list)
    budget: InvestigationBudget
    usage: UsageMetrics = Field(default_factory=UsageMetrics)


class CreateInvestigationRequest(_Model):
    question: str = Field(min_length=1)
    budget: InvestigationBudget | None = None
    profile: ResearchProfile | None = Field(
        default=None,
        description="Optional read-only research profile. When present, live candidates are also scored with knowledge value.",
    )
    profile_from_investigation_id: str | None = Field(
        default=None,
        description=(
            "Optional (1.2.0, additive): copy the research profile stored on this existing investigation. "
            "The server reads the stored profile; it is never supplied or edited by the client. "
            "Cannot be combined with profile."
        ),
    )


class BaselineRequest(_Model):
    question: str | None = None
    story: Literal["A", "C"] = "C"
    configuration: Literal["B0", "B3"] = "B0"


class BaselineReport(_Model):
    story: Literal["A", "C"]
    configuration: Literal["B0", "B3"]
    ran: bool
    blocker: str | None = None
    question: str
    sample_size: int = 1
    sample_note: str = "One investigation, not a statistical claim."
    search_calls: int = 0
    provider_calls: int = 0
    urls_fetched: int = 0
    llm_calls: int = 0
    llm_tokens: int | None = None
    supported_relationships: int = 0
    contradicted_relationships: int = 0
    insufficient_relationships: int = 0
    hypothesis_relationships: int = 0
    correct_citations: int = 0
    rejected_ungrounded_citations: int = 0
    contradiction_detections: int = 0
    shared_origin_detected: bool | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    network_calls: int = 0
    notes: str = ""


class AnswerCitation(_Model):
    """One source the generated answer is allowed to cite."""

    marker: str  # e.g. S1
    document_id: str | None = None
    title: str = ""
    url: str = ""
    quote: str = ""
    status: str = ""  # supported or considered


class GroundedAnswer(_Model):
    """A cited prose answer synthesized from an investigation's evidence.

    The answer explains evidence the investigation already grounded. It does
    not add facts. Every inline marker maps to one AnswerCitation. This is a
    synthesis for the reader, not a new verification step.
    """

    investigation_id: str
    question: str
    markdown: str
    citations: list[AnswerCitation] = Field(default_factory=list)
    cited_markers: list[str] = Field(default_factory=list)
    model: str = ""
    llm_tokens: int | None = None
    blocked: bool = False
    blocker: str | None = None


class ExplorationExport(_Model):
    """Full investigation snapshot another app can read from JSON Schema alone."""

    contract_version: str = CONTRACT_VERSION
    investigation: Investigation
    documents: list[Document] = Field(default_factory=list)
    document_versions: list[DocumentVersion] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    evidence_links: list[EvidenceLink] = Field(default_factory=list)
    provenance: list[SourceProvenance] = Field(default_factory=list)
    unresolved_questions: list[UnresolvedQuestion] = Field(default_factory=list)
    candidates: list[EvidenceCandidate] = Field(default_factory=list)
    actions: list[InvestigationAction] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    origin_clusters: list[OriginCluster] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    decision: ConvergenceDecision | None = None
    answer: GroundedAnswer | None = Field(
        default=None,
        description="Optional synthesized cited answer. Present only after the answer endpoint runs.",
    )
    profile: ResearchProfile | None = Field(
        default=None,
        description="Optional read-only profile snapshot. Absent on investigations that have no profile.",
    )


TraceStage = Literal[
    "search",
    "retrieval",
    "ranking",
    "extraction",
    "convergence",
    "checkpoint",
    "ingest",
    "blocked",
]
EpistemicStatus = Literal["supported", "contradicted", "unknown", "unresolved"]
ArtifactActivity = Literal["current_run", "reused_or_historical", "fixture_corpus", "unknown"]
InterventionType = Literal["fork", "challenge_evidence", "continue"]
KnowledgeState = Literal["interested", "known", "encountered", "uncertain", "frontier"]


class TraceEvent(_Model):
    """One recorded investigation stage. Absent events are not reconstructed."""

    id: str
    investigation_id: str
    sequence: int
    stage: TraceStage
    created_at: datetime
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: str | None = None


class TraceNode(_Model):
    id: str
    type: str
    label: str
    stage: str
    event_id: str
    epistemic_status: EpistemicStatus = "unknown"
    verification_basis: Literal["relationship_status", "claim_status", "none"] = "none"
    payload: dict[str, Any] = Field(default_factory=dict)


class TraceEdge(_Model):
    id: str
    source: str
    target: str
    kind: Literal["sequence", "produced", "inherited"] = "sequence"


class ArtifactNote(_Model):
    """How a stored candidate relates to this run's recorded activity.

    Attention and knowledge value are copied through as ranking signals.
    They do not set epistemic status and they do not verify the source.
    """

    kind: str
    id: str
    title: str = ""
    url: str = ""
    activity: ArtifactActivity
    origin_kind: str = "unknown"
    bridge_status: str = "unknown"
    bridge_trigger: str | None = None
    attention_score: float | None = None
    knowledge_value_score: float | None = None
    epistemic_status: EpistemicStatus = "unknown"
    verified: bool = False
    note: str = ""


class InvestigationTrace(_Model):
    investigation_id: str
    question: str
    incomplete: bool
    incomplete_reason: str | None = None
    events: list[TraceEvent] = Field(default_factory=list)
    nodes: list[TraceNode] = Field(default_factory=list)
    edges: list[TraceEdge] = Field(default_factory=list)
    artifact_notes: list[ArtifactNote] = Field(default_factory=list)
    search_event_count: int = 0
    fetch_event_count: int = 0
    llm_event_count: int = 0
    usage_search_calls: int = 0
    usage_fetch_calls: int = 0
    usage_llm_calls: int = 0
    parent_investigation_id: str | None = None
    branch_id: str | None = None
    intervention_type: str | None = None
    from_checkpoint_id: str | None = None
    branch_objective: str | None = None
    attached_claims: list[dict[str, Any]] = Field(default_factory=list, description="Parent claims recorded with the branch (id, text, status). Additive.")
    attached_sources: list[dict[str, Any]] = Field(default_factory=list, description="Parent documents recorded with the branch (id, title, url). Additive.")
    root_question: str | None = Field(default=None, description="Original research question at the top of the lineage. Additive in 1.1.0.")
    lifecycle: Lifecycle | None = Field(default=None, description="saved | queued | researching | stopped | completed | failed. Separate from status and stop_reason. Additive in 1.1.0.")


class InvestigationSummary(_Model):
    """Read-only listing row. Additive in 1.0.x: summarises a stored investigation, adds no facts."""

    id: str
    question: str
    status: InvestigationStatus
    stop_reason: StopReason | None = None
    created_at: datetime
    updated_at: datetime
    parent_investigation_id: str | None = None
    branch_id: str | None = None
    intervention_type: str | None = None
    from_checkpoint_id: str | None = None
    event_count: int = 0
    checkpoint_count: int = 0
    root_question: str | None = None
    lifecycle: Lifecycle | None = None


class InvestigationList(_Model):
    items: list[InvestigationSummary] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class BranchConstraints(_Model):
    max_searches: int | None = None
    max_fetched_urls: int | None = None
    max_llm_calls: int | None = None
    intent_weight: float | None = None
    novelty_weight: float | None = None
    unresolved_weight: float | None = None
    knowledge_value_weight: float | None = None


class CreateBranchRequest(_Model):
    from_checkpoint_id: str = Field(min_length=1)
    objective: str = ""
    intervention_type: InterventionType
    constraints: BranchConstraints | None = None
    attached_claim_ids: list[str] = Field(
        default_factory=list,
        description="Optional parent claim ids the branch is about. Must exist at the chosen checkpoint. Additive in 1.0.x.",
    )
    attached_source_ids: list[str] = Field(
        default_factory=list,
        description="Optional parent document ids referenced by the branch. Must exist at the chosen checkpoint. Additive in 1.0.x.",
    )


class BranchRecord(_Model):
    id: str
    parent_investigation_id: str
    child_investigation_id: str
    from_checkpoint_id: str
    intervention_type: InterventionType
    objective: str = ""
    created_at: datetime
    constraints: BranchConstraints = Field(default_factory=BranchConstraints)


class KnowledgeAssertionIn(_Model):
    profile_id: str = Field(min_length=1)
    investigation_id: str | None = None
    subject: str = Field(min_length=1)
    predicate: str = "related_to"
    object: str = ""
    text: str = ""
    epistemic_state: KnowledgeState
    basis: Literal["user_confirmation", "profile_declared", "profile_prior", "retrieved", "inferred"] = "user_confirmation"


class KnowledgeNode(_Model):
    id: str
    label: str
    epistemic_state: KnowledgeState
    basis: str
    confirmed: bool = False
    inferred: bool = False
    note: str = ""
    investigation_id: str | None = None


class KnowledgeEdge(_Model):
    id: str
    source: str
    target: str
    label: str
    basis: str


class KnowledgeGraph(_Model):
    profile_id: str | None = None
    investigation_id: str | None = None
    question: str | None = None
    nodes: list[KnowledgeNode] = Field(default_factory=list)
    edges: list[KnowledgeEdge] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def evidence_json_schema() -> dict[str, Any]:
    return ExplorationExport.model_json_schema(mode="serialization")


def build_example_payload() -> ExplorationExport:
    """Synthetic contract example. Not a live search and not a measured metric."""
    now = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
    content = "Lumenarc Dynamics closed a $400 million Series D."
    start = content.find("$400 million")
    end = start + len("$400 million")
    attention = AttentionComponents(R=1.0, G=0.0, D=0.0, F=0.0, score=0.4)
    budget = InvestigationBudget(max_searches=2, searches_used=1)
    version_id = "ver_example"
    document_id = "doc_example"
    span_id = "span_example"
    claim_id = "clm_example"
    rel_id = "rel_example"
    link_id = "link_example"
    return ExplorationExport(
        investigation=Investigation(
            id="inv_example",
            question="Do three articles independently substantiate a company's claim?",
            budget=budget,
            status="stopped",
            stop_reason="unresolved_uncertainty",
            decision_source="fallback",
            decision_rationale="Example only. The convergence endpoint was not called to build this file.",
            session_id=None,
            usage=UsageMetrics(search_calls=0, network_calls=0, llm_tokens=None, cost_usd=None, configuration="example"),
            created_at=now,
            updated_at=now,
            metadata={"example": True, "note": "Synthetic contract example. Not a live Telem investigation."},
        ),
        documents=[
            Document(
                id=document_id,
                canonical_url="https://lumenarc.example/press/2026-03-04-helios-array",
                title="Lumenarc Dynamics announcement",
                origin_id="LF-PR-2026-031",
                providers=["fixture"],
            )
        ],
        document_versions=[
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                content_hash="examplehash",
                fetched_at=now,
                content=content,
                publish_date="2026-03-04",
            )
        ],
        evidence_spans=[
            EvidenceSpan(
                id=span_id,
                version_id=version_id,
                document_id=document_id,
                text=content[start:end],
                start=start,
                end=end,
                content_hash="examplehash",
                attention=attention,
                quote_valid=True,
            )
        ],
        entities=[
            Entity(id="ent_lumenarc", name="Lumenarc Dynamics", type="organization"),
            Entity(id="ent_amount", name="$400 million", type="amount"),
        ],
        claims=[
            Claim(
                id=claim_id,
                text="Lumenarc Dynamics closed a $400 million Series D.",
                status="supported",
                document_id=document_id,
                span_id=span_id,
            )
        ],
        relationships=[
            Relationship(
                id=rel_id,
                subject_id="ent_lumenarc",
                predicate="raised_amount",
                object_id="ent_amount",
                claim_id=claim_id,
                kind="factual",
                status="supported",
                document_id=document_id,
                subject_name="Lumenarc Dynamics",
                object_name="$400 million",
                evidence_link_ids=[link_id],
                source_document_ids=[document_id],
            )
        ],
        evidence_links=[
            EvidenceLink(
                id=link_id,
                claim_id=claim_id,
                relationship_id=rel_id,
                evidence_span_id=span_id,
                stance="supports",
                quote_valid=True,
                semantic_status="supported",
            )
        ],
        provenance=[
            SourceProvenance(
                id="prov_example",
                document_id=document_id,
                provider="fixture",
                providers=["fixture"],
                origin_id="LF-PR-2026-031",
                origin_kind="original",
                shared_origin_group="LF-PR-2026-031",
            )
        ],
        unresolved_questions=[
            UnresolvedQuestion(
                id="q_example",
                text="Whether any source independent of wire LF-PR-2026-031 confirms the amount.",
                importance=0.9,
                missing_evidence=1.0,
                kind="independence",
            )
        ],
        candidates=[
            EvidenceCandidate(
                id="cand_example",
                document_id=document_id,
                url="https://lumenarc.example/press/2026-03-04-helios-array",
                title="Lumenarc Dynamics announcement",
                snippet=content,
                providers=["fixture"],
                attention=attention,
                publish_date="2026-03-04",
            )
        ],
        actions=[
            InvestigationAction(
                id="act_example",
                query="independent primary confirmation of the Lumenarc Dynamics Series D amount",
                reason="The open independence question has the highest importance times missing evidence.",
                target_question_id="q_example",
                utility=0.9,
                already_executed=False,
            )
        ],
        contradictions=[],
        origin_clusters=[
            OriginCluster(
                origin_id="LF-PR-2026-031",
                document_ids=[document_id],
                original_document_ids=[document_id],
                derivative_document_ids=[],
            )
        ],
        checkpoints=[
            Checkpoint(
                id="cp_example",
                investigation_id="inv_example",
                step=1,
                state_json={"question": "Do three articles independently substantiate a company's claim?"},
                stop_reason="unresolved_uncertainty",
                created_at=now,
                summary="Example checkpoint.",
            )
        ],
        decision=ConvergenceDecision(
            investigation_id="inv_example",
            approved_action=None,
            stop=True,
            stop_reason="unresolved_uncertainty",
            rationale="Example payload. decision_source is fallback because this file was not produced by the controller.",
            decision_source="fallback",
        ),
    )


def write_contract_files(directory: str) -> None:
    import json
    from pathlib import Path

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    schema = evidence_json_schema()
    schema["$id"] = f"https://searchhax.local/schemas/exploration-evidence-{CONTRACT_VERSION}.json"
    schema["$comment"] = (
        f"FrontierGraph exploration contract {CONTRACT_VERSION}. "
        "Attention scores are not factual confidence. "
        "decision_source is recorded by exploration as convergence or fallback."
    )
    (root / "evidence.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    example = build_example_payload()
    (root / "example_payload.json").write_text(
        json.dumps(example.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    from pathlib import Path

    write_contract_files(str(Path(__file__).resolve().parent))
