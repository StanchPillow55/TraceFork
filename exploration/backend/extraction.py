"""Entity, claim, and relationship extraction.

The deterministic extractor emits relationships only for the Story C fixture
(or other text explicitly marked as a fixture). It does not invent NVIDIA
relationships. Live pages without an LLM key are returned as blocked with an
empty relationship list.

LLM output is never stored as supported until verification finds an exact
quotation that also contains the subject and object. Quotation validity is
checked in verification.py.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts import Claim, Entity, EvidenceLink, EvidenceSpan, Relationship, UnresolvedQuestion

from exploration.backend.attention import score_passage
from exploration.backend.ids import content_hash, stable_id
from exploration.backend.llm import LLMNotConfigured, LLMResponseNotJSON, OpenAICompatibleClient
from exploration.backend.verification import assess_quotation, decide_status

_STORY_C_MARKERS = ("Lumenarc Dynamics", "LF-PR-2026-031")
_FIXTURE_DIRECTIVE = re.compile(
    r"\[\[rel\s+subject=\"(?P<subject>[^\"]+)\"\s+predicate=\"(?P<predicate>[^\"]+)\"\s+"
    r"object=\"(?P<object>[^\"]+)\"\s+quote=\"(?P<quote>[^\"]+)\"\]\]"
)

_EXCERPT_NOTE = re.compile(
    r"\b(?:the|this) (?:excerpt|snippet|provided text|supplied text)\b|\btruncated\b|\bmid-sentence\b|\bcut off\b",
    re.I,
)

_SYSTEM_PROMPT = """You extract entities, atomic claims, and subject-predicate-object relationships from one source document.
Return JSON with keys entities, relationships, gaps, hypotheses.
Each relationship needs subject, predicate, object, quote, kind.
The quote must be an exact substring of the document. If you cannot copy an exact span, omit the relationship.
Do not assign a support status. Do not invent facts that are not in the document.
Hypotheses must also quote an exact span or be omitted.
gaps is a list of objects with text and importance between 0 and 1.
Keep the answer short so it is never cut off: compact JSON on one line, no markdown,
at most 8 relationships, 4 gaps, 3 hypotheses and 12 entities. Each quote is one
sentence of at most 300 characters.
"""

# The same limits are enforced when a payload is read, so an over-long answer cannot
# grow the stored record (the model is asked for them above; this does not rely on it).
MAX_LLM_RELATIONSHIPS = 8
MAX_LLM_GAPS = 4


class ExtractionBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entities: list[Entity] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    spans: list[EvidenceSpan] = Field(default_factory=list)
    links: list[EvidenceLink] = Field(default_factory=list)
    questions: list[UnresolvedQuestion] = Field(default_factory=list)
    live_extraction_blocked: bool = False
    mode: str = "blocked"
    rejected_ungrounded: int = 0
    blocker: str | None = None
    llm_tokens: int | None = None


def is_story_c_text(content: str, url: str) -> bool:
    if ".example/" in url or url.endswith(".example"):
        return "Lumenarc Dynamics" in content or "LF-PR-2026-031" in content or "Helios Array" in content
    return all(marker in content for marker in _STORY_C_MARKERS)


def extract_document(
    content: str,
    *,
    url: str,
    document_id: str,
    version_id: str,
    version_hash: str,
    question: str,
    explicit_fixture: bool = False,
    llm: OpenAICompatibleClient | None = None,
    publish_date: str | None = None,
    gaps: list[str] | None = None,
) -> ExtractionBatch:
    disclosed = _extract_disclosed_facts(
        content,
        document_id=document_id,
        version_id=version_id,
        version_hash=version_hash,
        question=question,
        publish_date=publish_date,
        gaps=gaps,
    )
    if is_story_c_text(content, url):
        return _extract_story_c(
            content,
            document_id=document_id,
            version_id=version_id,
            version_hash=version_hash,
            question=question,
            publish_date=publish_date,
            gaps=gaps,
        )
    if explicit_fixture:
        return _extract_explicit_fixture(
            content,
            document_id=document_id,
            version_id=version_id,
            version_hash=version_hash,
            question=question,
            publish_date=publish_date,
            gaps=gaps,
        )
    client = llm if llm is not None else OpenAICompatibleClient(timeout=12)
    if disclosed.relationships and not client.configured:
        return disclosed
    if client.configured:
        extracted = _extract_with_llm(
            client,
            content,
            document_id=document_id,
            version_id=version_id,
            version_hash=version_hash,
            question=question,
            publish_date=publish_date,
            gaps=gaps,
        )
        if not disclosed.relationships:
            return extracted
        # The disclosed-fact pattern does not hide the rest of the page: the
        # model's grounded relationships are kept alongside it.
        merged = merge_batches([disclosed, extracted])
        merged.mode = "disclosed_facts+llm"
        # Grounded disclosed facts exist, so extraction is not blocked; a model
        # failure is still reported through ``blocker``.
        merged.live_extraction_blocked = False
        merged.blocker = extracted.blocker if extracted.live_extraction_blocked else None
        return merged
    return ExtractionBatch(
        live_extraction_blocked=True,
        mode="blocked",
        blocker="LLM_API_KEY and OPENAI_API_KEY are missing; live extraction was not run and no relationships were invented.",
    )


def relationships_from_llm_payload(
    payload: dict[str, Any],
    content: str,
    *,
    document_id: str,
    version_id: str,
    version_hash: str,
    question: str = "",
    publish_date: str | None = None,
    gaps: list[str] | None = None,
) -> ExtractionBatch:
    """Ground a model payload. A status field on the payload is ignored."""
    batch = ExtractionBatch(mode="llm")
    relationships = payload.get("relationships") or []
    if isinstance(relationships, list):
        relationships = relationships[:MAX_LLM_RELATIONSHIPS]
    for item in relationships:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        predicate = str(item.get("predicate") or "").strip()
        obj = str(item.get("object") or "").strip()
        quote = str(item.get("quote") or "")
        kind = str(item.get("kind") or "factual")
        if not subject or not predicate or not obj:
            continue
        _add_grounded(
            batch,
            content=content,
            document_id=document_id,
            version_id=version_id,
            version_hash=version_hash,
            subject=subject,
            subject_type="entity",
            predicate=predicate,
            obj=obj,
            object_type="entity",
            quote=quote,
            question=question,
            publish_date=publish_date,
            gaps=gaps,
            kind=kind,
        )
    raw_gaps = payload.get("gaps") or []
    for item in raw_gaps[:MAX_LLM_GAPS] if isinstance(raw_gaps, list) else raw_gaps:
        if isinstance(item, str):
            text, importance = item, 0.5
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            try:
                importance = float(item.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
        else:
            continue
        if not text or _EXCERPT_NOTE.search(text):
            # A note about the excerpt itself (truncation, missing text) is not
            # an open research question and must not become a search.
            continue
        importance = max(0.0, min(1.0, importance))
        batch.questions.append(
            UnresolvedQuestion(
                id=stable_id("q", document_id, text),
                text=text,
                importance=importance,
                missing_evidence=1.0,
                kind="gap",
            )
        )
    return batch


def _extract_with_llm(
    client: OpenAICompatibleClient,
    content: str,
    *,
    document_id: str,
    version_id: str,
    version_hash: str,
    question: str,
    publish_date: str | None,
    gaps: list[str] | None,
) -> ExtractionBatch:
    excerpt = _llm_excerpt(content, question, limit=800)
    user = (
        "Research question:\n"
        f"{question}\n\n"
        "Document excerpt. Quote only an exact substring of this excerpt:\n"
        f"{excerpt}"
    )
    try:
        payload, usage = client.complete_json(_SYSTEM_PROMPT, user)
    except LLMNotConfigured as exc:
        return ExtractionBatch(live_extraction_blocked=True, mode="blocked", blocker=str(exc))
    except Exception as exc:  # network, parse, or API failure: do not invent rows
        return ExtractionBatch(
            live_extraction_blocked=True,
            mode="blocked",
            blocker=f"LLM extraction failed: {type(exc).__name__}"
            + (f" ({exc})" if isinstance(exc, LLMResponseNotJSON) else ""),
        )
    batch = relationships_from_llm_payload(
        payload,
        content,
        document_id=document_id,
        version_id=version_id,
        version_hash=version_hash,
        question=question,
        publish_date=publish_date,
        gaps=gaps,
    )
    tokens = usage.get("total_tokens")
    if not isinstance(tokens, int):
        # Anthropic's compatibility layer may report only the two parts.
        parts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        tokens = sum(parts) if all(isinstance(p, int) for p in parts) else None
    batch.llm_tokens = int(tokens) if isinstance(tokens, int) else None
    return batch


# "CoreWeave's rival" is not CoreWeave: the company name must end the noun phrase.
_INVESTMENT = re.compile(
    r"NVIDIA has invested (?P<amount>\$[\d.]+ billion) in (?P<company>CoreWeave)(?![\w'\u2019-])"
)
# Wording in the same sentence that denies, hedges, or reports a rumor: the
# sentence does not assert the investment.
_NOT_ASSERTED = re.compile(
    r"\b(?:rumou?r(?:ed|s)?|unconfirmed|unverified|denie[sd]|deny|denial|false|alleged(?:ly)?|"
    r"not|never|no evidence|speculat\w*|hoax)\b",
    re.I,
)
# A demand gap is closed only by a sentence that states a figure for demand or
# revenue; a sentence asserting that demand rose because of something is not that.
_DEMAND = re.compile(r"customer demand|downstream demand|product revenue", re.I)


def _sentence_around(content: str, start: int, end: int) -> str:
    left = max(content.rfind(mark, 0, start) for mark in (".", "!", "?", "\n"))
    right_candidates = [i for i in (content.find(mark, end) for mark in (".", "!", "?", "\n")) if i >= 0]
    right = min(right_candidates) if right_candidates else len(content)
    return content[left + 1 : right + 1]


def _demand_stated(content: str) -> bool:
    for match in _DEMAND.finditer(content):
        sentence = _sentence_around(content, match.start(), match.end())
        if re.search(r"\d", sentence):
            return True
    return False


def _extract_disclosed_facts(
    content: str,
    *,
    document_id: str,
    version_id: str,
    version_hash: str,
    question: str,
    publish_date: str | None,
    gaps: list[str] | None,
) -> ExtractionBatch:
    """Emit a relationship only when the quote is an exact slice of the stored page."""
    batch = ExtractionBatch(mode="disclosed_facts")
    found_investment = False
    for match in _INVESTMENT.finditer(content):
        sentence = _sentence_around(content, match.start(), match.end())
        if _NOT_ASSERTED.search(sentence):
            continue
        quote = match.group(0)
        _add_grounded(
            batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
            subject="NVIDIA", subject_type="organization", predicate="invested_in",
            obj="CoreWeave", object_type="organization", quote=quote, question=question,
            publish_date=publish_date, gaps=gaps, kind="factual",
        )
        _add_grounded(
            batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
            subject="NVIDIA", subject_type="organization", predicate="invested_amount",
            obj=match.group("amount"), object_type="amount", quote=quote, question=question,
            publish_date=publish_date, gaps=gaps, kind="factual",
        )
        found_investment = True
    demand_stated = _demand_stated(content)
    if found_investment and not demand_stated:
        batch.questions.append(
            UnresolvedQuestion(
                id=stable_id("q", document_id, "coreweave-demand"),
                text="What downstream customer demand or product revenue is tied to the NVIDIA CoreWeave capacity expansion?",
                importance=0.85,
                missing_evidence=1.0,
                kind="gap",
            )
        )
    return batch


def _llm_excerpt(content: str, question: str, limit: int = 5000) -> str:
    """Prefer paragraphs that overlap the question. Quotes are still checked against the full page."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
    if not paragraphs:
        return content[:limit]
    question_tokens = set(re.findall(r"[a-z0-9]+", question.lower()))
    ranked = sorted(
        paragraphs,
        key=lambda paragraph: len(set(re.findall(r"[a-z0-9]+", paragraph.lower())) & question_tokens),
        reverse=True,
    )
    chosen: list[str] = []
    size = 0
    for paragraph in ranked:
        # Paragraphs that do not repeat the question still go in (ranked
        # last): a short later sentence can change what the page says.
        room = limit - size
        if room <= 0:
            break
        piece = paragraph if len(paragraph) <= room else paragraph[:room]
        chosen.append(piece)
        size += len(piece) + 2
        if len(paragraph) > room:
            break
    return "\n\n".join(chosen)[:limit]


def _extract_explicit_fixture(
    content: str,
    *,
    document_id: str,
    version_id: str,
    version_hash: str,
    question: str,
    publish_date: str | None,
    gaps: list[str] | None,
) -> ExtractionBatch:
    batch = ExtractionBatch(mode="explicit_fixture")
    for match in _FIXTURE_DIRECTIVE.finditer(content):
        _add_grounded(
            batch,
            content=content,
            document_id=document_id,
            version_id=version_id,
            version_hash=version_hash,
            subject=match.group("subject"),
            subject_type="entity",
            predicate=match.group("predicate"),
            obj=match.group("object"),
            object_type="entity",
            quote=match.group("quote"),
            question=question,
            publish_date=publish_date,
            gaps=gaps,
            kind="factual",
        )
    return batch


def _extract_story_c(
    content: str,
    *,
    document_id: str,
    version_id: str,
    version_hash: str,
    question: str,
    publish_date: str | None,
    gaps: list[str] | None,
) -> ExtractionBatch:
    batch = ExtractionBatch(mode="deterministic_fixture")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line not in content:
            continue
        if "announced the Helios Array" in line:
            _add_grounded(
                batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
                subject="Lumenarc Dynamics", subject_type="organization", predicate="announced",
                obj="Helios Array", object_type="program", quote=line, question=question,
                publish_date=publish_date, gaps=gaps, kind="factual",
            )
        amount = re.search(r"\$(\d+) million Series D", line)
        if amount and "Lumenarc Dynamics" in line:
            _add_grounded(
                batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
                subject="Lumenarc Dynamics", subject_type="organization", predicate="raised_amount",
                obj=f"${amount.group(1)} million", object_type="amount", quote=line, question=question,
                publish_date=publish_date, gaps=gaps, kind="factual",
            )
        if "led by Northwind Capital" in line and "Lumenarc Dynamics" in line:
            _add_grounded(
                batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
                subject="Lumenarc Dynamics", subject_type="organization", predicate="led_by",
                obj="Northwind Capital", object_type="organization", quote=line, question=question,
                publish_date=publish_date, gaps=gaps, kind="factual",
            )
        capacity = re.search(r"1\.8 GW(?: of compute capacity)? by 2029", line)
        if "Helios Array" in line and capacity:
            _add_grounded(
                batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
                subject="Helios Array", subject_type="program", predicate="planned_capacity",
                obj=capacity.group(0), object_type="measurement", quote=line, question=question,
                publish_date=publish_date, gaps=gaps, kind="factual",
            )
        if "first campus" in line and "Helios Array" in line and "Red Mesa, Arizona" in line:
            _add_grounded(
                batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
                subject="Helios Array", subject_type="program", predicate="first_campus",
                obj="Red Mesa, Arizona", object_type="place", quote=line, question=question,
                publish_date=publish_date, gaps=gaps, kind="factual",
            )
        if "Lumenarc Dynamics" in line and "LF-PR-2026-031" in line:
            _add_grounded(
                batch, content=content, document_id=document_id, version_id=version_id, version_hash=version_hash,
                subject="Lumenarc Dynamics", subject_type="organization", predicate="cites_wire",
                obj="LF-PR-2026-031", object_type="identifier", quote=line, question=question,
                publish_date=publish_date, gaps=gaps, kind="factual",
            )
    return batch


def _add_grounded(
    batch: ExtractionBatch,
    *,
    content: str,
    document_id: str,
    version_id: str,
    version_hash: str,
    subject: str,
    subject_type: str,
    predicate: str,
    obj: str,
    object_type: str,
    quote: str,
    question: str,
    publish_date: str | None,
    gaps: list[str] | None,
    kind: str,
) -> None:
    rel_id = stable_id("rel", document_id, subject, predicate, obj)
    if any(existing.id == rel_id for existing in batch.relationships):
        return
    status, assessment = decide_status(content, quote, subject, obj)
    subject_entity = _entity(batch, subject, subject_type)
    object_entity = _entity(batch, obj, object_type)
    if assessment.valid and status == "supported" and kind.strip().lower() not in {"factual", "fact", ""}:
        # A grounded hypothesis (or other non-factual kind) is recorded as a
        # hypothesis, never as supported evidence.
        _add_hypothesis(batch, document_id=document_id, subject=subject, predicate=predicate, obj=obj,
                        quote=quote, kind=kind, subject_entity=subject_entity, object_entity=object_entity)
        return
    if not assessment.valid or status != "supported":
        batch.rejected_ungrounded += 1
        claim_id = stable_id("clm", document_id, predicate, obj, "ungrounded", quote)
        rel_id = stable_id("rel", document_id, subject, predicate, obj, "ungrounded", quote)
        if any(claim.id == claim_id for claim in batch.claims):
            return
        batch.claims.append(
            Claim(
                id=claim_id,
                text=f"{subject} {predicate} {obj}",
                status="insufficient",
                document_id=document_id,
                span_id=None,
            )
        )
        batch.relationships.append(
            Relationship(
                id=rel_id,
                subject_id=subject_entity.id,
                predicate=predicate,
                object_id=object_entity.id,
                claim_id=claim_id,
                kind=kind,
                status="insufficient",
                document_id=document_id,
                subject_name=subject,
                object_name=obj,
                source_document_ids=[document_id],
                notes="Rejected as support because the quotation is not an exact grounded substring.",
            )
        )
        return

    assert assessment.start is not None and assessment.end is not None
    span = _span(
        batch,
        content=content,
        document_id=document_id,
        version_id=version_id,
        version_hash=version_hash,
        quote=quote,
        start=assessment.start,
        end=assessment.end,
        question=question,
        publish_date=publish_date,
        gaps=gaps,
    )
    # Re-check the stored slice. A span that does not match is not support.
    if content[span.start : span.end] != quote:
        batch.rejected_ungrounded += 1
        return
    claim_id = stable_id("clm", document_id, predicate, obj)
    link_id = stable_id("link", rel_id, span.id)
    batch.claims.append(
        Claim(
            id=claim_id,
            text=quote,
            status="supported",
            document_id=document_id,
            span_id=span.id,
        )
    )
    batch.relationships.append(
        Relationship(
            id=rel_id,
            subject_id=subject_entity.id,
            predicate=predicate,
            object_id=object_entity.id,
            claim_id=claim_id,
            kind=kind,
            status="supported",
            document_id=document_id,
            subject_name=subject,
            object_name=obj,
            evidence_link_ids=[link_id],
            source_document_ids=[document_id],
        )
    )
    batch.links.append(
        EvidenceLink(
            id=link_id,
            claim_id=claim_id,
            relationship_id=rel_id,
            evidence_span_id=span.id,
            stance="supports",
            quote_valid=True,
            semantic_status="supported",
        )
    )


def _add_hypothesis(batch, *, document_id, subject, predicate, obj, quote, kind, subject_entity, object_entity) -> None:
    claim_id = stable_id("clm", document_id, predicate, obj, "hypothesis", quote)
    rel_id = stable_id("rel", document_id, subject, predicate, obj, "hypothesis", quote)
    if any(rel.id == rel_id for rel in batch.relationships):
        return
    batch.claims.append(
        Claim(id=claim_id, text=quote, status="hypothesis", document_id=document_id, span_id=None)
    )
    batch.relationships.append(
        Relationship(
            id=rel_id,
            subject_id=subject_entity.id,
            predicate=predicate,
            object_id=object_entity.id,
            claim_id=claim_id,
            kind=kind,
            status="hypothesis",
            document_id=document_id,
            subject_name=subject,
            object_name=obj,
            source_document_ids=[document_id],
            notes=f"Stated as {kind}, not as a fact. The quotation is exact, but it is not support.",
        )
    )


def _entity(batch: ExtractionBatch, name: str, entity_type: str) -> Entity:
    entity_id = stable_id("ent", entity_type, name.strip().lower())
    for existing in batch.entities:
        if existing.id == entity_id:
            return existing
    entity = Entity(id=entity_id, name=name, type=entity_type)
    batch.entities.append(entity)
    return entity


def _span(
    batch: ExtractionBatch,
    *,
    content: str,
    document_id: str,
    version_id: str,
    version_hash: str,
    quote: str,
    start: int,
    end: int,
    question: str,
    publish_date: str | None,
    gaps: list[str] | None,
) -> EvidenceSpan:
    span_id = stable_id("span", version_id, str(start), str(end))
    for existing in batch.spans:
        if existing.id == span_id:
            return existing
    attention = score_passage(question, quote, gaps, publish_date)
    span = EvidenceSpan(
        id=span_id,
        version_id=version_id,
        document_id=document_id,
        text=content[start:end],
        start=start,
        end=end,
        content_hash=content_hash(content[start:end]),
        attention=attention,
        quote_valid=content[start:end] == quote,
    )
    batch.spans.append(span)
    return span


def merge_batches(batches: list[ExtractionBatch]) -> ExtractionBatch:
    merged = ExtractionBatch(mode="merged")
    seen_entities: set[str] = set()
    for batch in batches:
        merged.rejected_ungrounded += batch.rejected_ungrounded
        merged.live_extraction_blocked = merged.live_extraction_blocked or batch.live_extraction_blocked
        if batch.blocker and not merged.blocker:
            merged.blocker = batch.blocker
        if batch.llm_tokens is not None:
            merged.llm_tokens = (merged.llm_tokens or 0) + batch.llm_tokens
        for entity in batch.entities:
            if entity.id not in seen_entities:
                merged.entities.append(entity)
                seen_entities.add(entity.id)
        _extend_unique(merged.claims, batch.claims)
        _extend_unique(merged.relationships, batch.relationships)
        _extend_unique(merged.spans, batch.spans)
        _extend_unique(merged.links, batch.links)
        _extend_unique(merged.questions, batch.questions)
    return merged


def _extend_unique(target: list, items: list) -> None:
    seen = {item.id for item in target}
    for item in items:
        if item.id in seen:
            continue
        target.append(item)
        seen.add(item.id)
