/*
 * Persisted → presentation adapter.
 *
 * Translates the backend's recorded investigation (ExplorationExport + InvestigationTrace +
 * BranchRecords) into the presentation model the approved Lantern Interior renders.
 *
 * Rules:
 *  - Every timeline event is exactly one persisted TraceEvent, in recorded order.
 *  - Graph nodes keep backend ids (evt_…, doc_…, clm_…, cp_…, q_…). Passage nodes use the
 *    backend trace-node id `${event_id}:p${index}`.
 *  - Nothing is invented. A missing document, snapshot, coordinate or relationship is
 *    reported in `notes`, never filled in.
 *  - The only presentation-only objects are round headers (marked `derived: true`), which
 *    group recorded events that start at a recorded search.
 */
import { PolicyPoint, type BranchRecordT, type ExplorationExportT, type InvestigationTraceT, type TraceEventT } from "../api/schemas";
import type {
  Checkpoint,
  CheckpointDetail,
  ClaimStatus,
  DecisionOutcome,
  EdgeKind,
  EventKind,
  Lantern,
  LanternStatus,
  PolicyPointView,
  Trace,
  TraceEdge,
  TraceEvent,
  TraceNode,
} from "../trace/types";

export interface AdaptedInvestigation {
  trace: Trace;
  lantern: Lantern;
  related: Lantern[];
  /** The validated export the trace was built from (read by the Findings view). */
  source: ExplorationExportT;
}

const STATUS_MAP: Record<string, ClaimStatus> = {
  supported: "supported",
  contradicted: "contested",
  insufficient: "insufficient",
  hypothesis: "hypothesis",
};

const STOP_OUTCOME: Record<string, DecisionOutcome> = {
  evidence_sufficient: "stop-sufficient",
  budget_exhausted: "stop-budget",
  unresolved_uncertainty: "stop-uncertain",
  low_expected_value: "stop-low-value",
  no_useful_actions: "stop-no-actions",
};

const STOP_LABEL: Record<string, string> = {
  evidence_sufficient: "evidence sufficient under controller policy",
  budget_exhausted: "research budget exhausted",
  unresolved_uncertainty: "unresolved uncertainty",
  low_expected_value: "low expected value",
  no_useful_actions: "no useful actions",
};

export function lanternStatus(status: string, stopReason?: string | null, lifecycle?: string | null): LanternStatus {
  // 1.1.0 backends report the lifecycle explicitly; it wins over status inference.
  if (lifecycle === "saved") return "not-started";
  if (lifecycle === "failed") return "blocked";
  if (status === "stopped") {
    const map: Record<string, LanternStatus> = {
      evidence_sufficient: "stopped-sufficient",
      budget_exhausted: "stopped-budget",
      unresolved_uncertainty: "stopped-uncertain",
      low_expected_value: "stopped-low-value",
      no_useful_actions: "stopped-no-actions",
    };
    return (stopReason && map[stopReason]) || "stopped";
  }
  if (status === "running" || status === "created" || status === "blocked") return status;
  return "stopped";
}

const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const asRecord = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);
function host(url?: string) {
  if (!url) return "";
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}
function questionKey(q: unknown): { id: string; text: string } | null {
  if (typeof q === "string") return q.trim() ? { id: `qtext:${q.trim().toLowerCase()}`, text: q.trim() } : null;
  const r = asRecord(q);
  const text = str(r.text) ?? str(r.query);
  if (!text) return null;
  return { id: str(r.id) ?? `qtext:${text.trim().toLowerCase()}`, text };
}

export function adaptInvestigation(
  exp: ExplorationExportT,
  tr: InvestigationTraceT,
  branches: BranchRecordT[],
): AdaptedInvestigation {
  const inv = exp.investigation;
  if (tr.investigation_id !== inv.id) {
    throw new Error(`Trace ${tr.investigation_id} does not belong to investigation ${inv.id}`);
  }
  const notes: string[] = [];
  const nodes: TraceNode[] = [];
  const edges: TraceEdge[] = [];
  const events: TraceEvent[] = [];
  const checkpoints: Checkpoint[] = [];
  const rounds: Trace["rounds"] = [];
  const nodeById = new Map<string, TraceNode>();

  const docById = new Map(exp.documents.map((d) => [d.id, d]));
  const docByUrl = new Map(exp.documents.map((d) => [d.canonical_url, d]));
  const provByDoc = new Map(exp.provenance.map((p) => [p.document_id, p]));
  const versionByDoc = new Map<string, (typeof exp.document_versions)[number]>();
  for (const v of exp.document_versions) if (!versionByDoc.has(v.document_id)) versionByDoc.set(v.document_id, v);
  const spanById = new Map(exp.evidence_spans.map((s) => [s.id, s]));
  const cpById = new Map(exp.checkpoints.map((c) => [c.id, c]));

  const add = (n: TraceNode) => {
    if (nodeById.has(n.id)) return nodeById.get(n.id)!;
    nodes.push(n);
    nodeById.set(n.id, n);
    return n;
  };
  const edge = (from: string, to: string, kind: EdgeKind, label?: string) => {
    const id = `${from}>${to}:${kind}`;
    if (from === to || edges.some((e) => e.id === id)) return;
    edges.push({ id, from, to, kind, label });
  };

  // Research question: the lineage's original question (1.1.0 root_question), else the row's own.
  const rootQuestion = tr.root_question || inv.question;
  const legacyOverwrite = !!tr.root_question && tr.root_question !== inv.question;
  if (legacyOverwrite) {
    notes.push(
      `Legacy branch: this record's stored question ("${short(inv.question, 80)}") was overwritten by its branch objective when it was created. The original research question shown here was resolved from the parent lineage.`,
    );
  }
  const intentId = `intent:${inv.id}`;
  add({
    id: intentId,
    kind: "intent",
    label: rootQuestion,
    sub: "Original research question",
    round: 0,
    revealAt: 0,
    ref: { table: "investigations", id: inv.id },
    body: rootQuestion,
  });

  if (tr.parent_investigation_id) {
    add({
      id: `branch:${tr.branch_id ?? tr.parent_investigation_id}`,
      kind: "inherited",
      label: `Branched from ${tr.parent_investigation_id}${tr.from_checkpoint_id ? ` at ${tr.from_checkpoint_id}` : ""}`,
      round: 0,
      revealAt: 0,
      ref: { table: "branches", id: tr.branch_id ?? "" },
      body: [
        `Recorded intervention: ${tr.intervention_type ?? "unknown"}. The parent investigation is read-only context.`,
        tr.branch_objective ? `Branch objective: ${tr.branch_objective}` : "",
        tr.attached_claims.length ? `Attached claims: ${tr.attached_claims.map((c) => `“${c.text}” (${c.id})`).join("; ")}` : "",
        tr.attached_sources.length ? `Attached sources: ${tr.attached_sources.map((d) => `${d.title || d.url} (${d.id})`).join("; ")}` : "",
      ]
        .filter(Boolean)
        .join("\n"),
    });
    edge(intentId, `branch:${tr.branch_id ?? tr.parent_investigation_id}`, "order");
  }

  let round = 0;
  let roundHeader: string | null = null;
  let lastQuery: string | null = null;
  let lastGroup: string | null = null;
  let lastApprovedQuery: string | null = null;
  let roundEventsSeen = false;
  const sorted = [...tr.events].sort((a, b) => a.sequence - b.sequence);

  const openRound = (ev: TraceEventT, seq: number, title: string) => {
    round += 1;
    rounds.push({ n: round, title, phase: "Recorded events" });
    roundHeader = `round:${ev.id}`;
    add({
      id: roundHeader,
      kind: "round",
      label: title,
      sub: `Round ${round} · grouping of recorded events`,
      round,
      revealAt: seq,
      derived: true,
      body: "Presentation grouping only: a round starts at a recorded search (or ingest) event and contains the events that follow it. It is not itself a recorded record.",
    });
    edge(nodes.find((n) => n.kind === "decision" && n.round === round - 1)?.id ?? intentId, roundHeader, "order");
    lastQuery = null;
    lastGroup = null;
    roundEventsSeen = true;
  };

  const ensureDoc = (documentId: string | null | undefined, seq: number, fallback: { url?: string; title?: string; fromCache?: boolean; status?: string; provider?: string } = {}): string | null => {
    const doc = documentId ? docById.get(documentId) : fallback.url ? docByUrl.get(fallback.url) : undefined;
    const id = doc?.id ?? documentId ?? null;
    if (!id) return null;
    const existing = nodeById.get(id);
    if (existing) return id;
    if (!lastGroup) return null;
    const prov = provByDoc.get(id);
    const ver = versionByDoc.get(id);
    const url = doc?.canonical_url ?? fallback.url;
    if (!doc) notes.push(`Document ${id} is referenced by an event but has no stored document record.`);
    add({
      id,
      kind: "doc",
      parent: lastGroup,
      label: doc?.title || fallback.title || url || id,
      publisher: host(url),
      origin: (prov?.origin_kind ?? "unknown") as TraceNode["origin"],
      docType: fallback.fromCache === undefined ? "Stored document" : fallback.fromCache ? "Retrieved from cache" : "Fetched",
      round,
      revealAt: seq,
      ref: doc ? { table: "documents", id } : undefined,
      provenance: {
        url,
        provider: prov?.provider ?? fallback.provider ?? null,
        providers: prov?.providers ?? doc?.providers ?? [],
        originKind: prov?.origin_kind ?? "unknown",
        originId: prov?.origin_id ?? doc?.origin_id ?? null,
        sharedOriginGroup: prov?.shared_origin_group ?? null,
        citedOriginIds: prov?.cited_origin_ids ?? [],
        notes: prov?.notes,
        fetchedAt: ver?.fetched_at,
        publishDate: ver?.publish_date ?? null,
        contentHash: ver?.content_hash,
        versionId: ver?.id,
        fromCache: fallback.fromCache,
        retrievalStatus: fallback.status,
        provenanceId: prov?.id,
      },
    });
    return id;
  };

  const ensureQuestion = (q: unknown, seq: number): string | null => {
    const key = questionKey(q);
    if (!key) return null;
    const known = exp.unresolved_questions.find((u) => u.id === key.id || u.text === key.text);
    const id = known?.id ?? key.id;
    const existing = nodeById.get(id);
    if (existing) return id;
    add({
      id,
      kind: "question",
      label: key.text,
      round: Math.max(round, 1),
      revealAt: seq,
      ref: known ? { table: "unresolved_questions", id: known.id } : undefined,
      history: [{ at: seq, status: "open", note: "Recorded as an open question at this event." }],
    });
    return id;
  };

  sorted.forEach((ev, seq) => {
    const p = asRecord(ev.payload);
    let kind: EventKind = "generic";
    let focus: string | undefined;
    if (ev.stage === "search" || ev.stage === "ingest") {
      openRound(ev, seq, ev.stage === "search" ? `Search round ${round + 1}` : "Fixture corpus ingest");
    } else if (!roundEventsSeen) {
      openRound(ev, seq, "Recorded activity");
    }
    switch (ev.stage) {
      case "search": {
        kind = "search";
        const query = str(p.query) ?? ev.summary;
        const providers = asArray(p.providers).filter((x): x is string => typeof x === "string");
        add({ id: ev.id, kind: "query", label: query, sub: `Search · ${p.from_cache ? "cached" : p.network ? "network" : "recorded"}`, provider: providers.join(", ") || undefined, round, revealAt: seq, ref: { table: "investigation_events", id: ev.id, eventId: ev.id } });
        edge(roundHeader!, ev.id, "order");
        // 1.1.0 backends record what prompted the search (planned_from.target_question_id).
        // Prefer that recorded link; fall back to matching the approved query text.
        const plannedFrom = asRecord(p.planned_from);
        const targetQ = str(plannedFrom.target_question_id);
        const recordedQ = targetQ ? nodes.find((n) => n.kind === "question" && n.ref?.id === targetQ) : undefined;
        if (recordedQ) edge(recordedQ.id, ev.id, "prompted", "recorded: approved follow-up for this question");
        else if (lastApprovedQuery && lastApprovedQuery.trim() === query.trim()) {
          const q = nodes.find((n) => n.kind === "question" && n.label.trim() === query.trim());
          if (q) edge(q.id, ev.id, "prompted", "approved follow-up");
        }
        if (typeof p.round === "number" && roundHeader) {
          const header = nodeById.get(roundHeader);
          if (header) header.sub = `Round ${p.round} · round number recorded on the search event`;
        }
        lastQuery = ev.id;
        focus = ev.id;
        break;
      }
      case "retrieval":
      case "ingest": {
        kind = "retrieve";
        const pages = asArray(ev.stage === "ingest" ? p.documents : p.pages).map(asRecord);
        add({ id: ev.id, kind: "group", label: ev.stage === "ingest" ? `Ingested ${pages.length} document(s)` : `Retrieved ${pages.length} page(s)`, sub: `${pages.length} documents`, round, revealAt: seq, ref: { table: "investigation_events", id: ev.id, eventId: ev.id } });
        if (lastQuery) edge(lastQuery, ev.id, "retrieved");
        else edge(roundHeader!, ev.id, "order");
        lastGroup = ev.id;
        for (const page of pages) {
          const url = str(page.url);
          const docId = str(page.id) ?? (url ? docByUrl.get(url)?.id : undefined);
          const created = ensureDoc(docId ?? null, seq, { url, title: str(page.title), fromCache: typeof page.from_cache === "boolean" ? page.from_cache : undefined, status: str(page.status), provider: str(page.provider) });
          if (!created && url) notes.push(`Retrieved page ${url} has no stored document record, so it is not drawn.`);
        }
        focus = ev.id;
        break;
      }
      case "ranking": {
        kind = "rank";
        const rows = asArray(p.candidates).map(asRecord);
        if (!lastGroup) {
          add({ id: ev.id, kind: "group", label: `Ranked ${rows.length} candidate(s)`, sub: `${rows.length} candidates`, round, revealAt: seq, ref: { table: "investigation_events", id: ev.id, eventId: ev.id } });
          if (lastQuery) edge(lastQuery, ev.id, "retrieved");
          lastGroup = ev.id;
        }
        for (const row of rows) {
          const docId = ensureDoc(str(row.document_id) ?? null, seq, { url: str(row.url), title: str(row.title) });
          const node = docId ? nodeById.get(docId) : undefined;
          if (node?.provenance) {
            node.provenance.attentionScore = typeof row.attention_score === "number" ? row.attention_score : null;
            node.provenance.knowledgeValueScore = typeof row.knowledge_value_score === "number" ? row.knowledge_value_score : null;
            node.provenance.knowledgeValueConfirmed = asArray(row.knowledge_value_confirmed).filter((x): x is string => typeof x === "string");
            node.provenance.candidateId = str(row.id);
          }
        }
        focus = lastGroup ?? undefined;
        break;
      }
      case "extraction": {
        kind = "evaluate";
        const passages = asArray(p.passages).map(asRecord);
        passages.forEach((ps, i) => {
          const docId = ensureDoc(str(ps.document_id) ?? null, seq);
          const span = str(ps.span_id) ? spanById.get(str(ps.span_id)!) : undefined;
          const pid = `${ev.id}:p${i}`;
          const stances = asArray(ps.stances).filter((x): x is string => typeof x === "string");
          if (docId) {
            add({
              id: pid,
              kind: "passage",
              parent: docId,
              label: short(str(ps.passage) ?? str(ps.claim_text) ?? "Recorded passage", 90),
              text: str(ps.passage) ?? undefined,
              round,
              revealAt: seq,
              ref: span ? { table: "evidence_spans", id: span.id, eventId: ev.id } : { table: "investigation_events", id: ev.id, eventId: ev.id },
              stances,
              provenance: {
                spanStart: span?.start,
                spanEnd: span?.end,
                quoteValid: typeof ps.quote_valid === "boolean" ? ps.quote_valid : span?.quote_valid,
                contentHash: span?.content_hash,
                versionId: span?.version_id,
                attentionScore: span?.attention?.score ?? null,
              },
              coordNote: undefined,
            });
          } else {
            notes.push(`Passage ${pid} has no document to attach to, so it is not drawn.`);
          }
          const claimId = str(ps.claim_id);
          if (!claimId) return;
          const status = STATUS_MAP[str(ps.status) ?? ""] ?? "unverified";
          const rels = asArray(ps.relationships).map(asRecord).map((r) => ({ id: str(r.id) ?? "", predicate: str(r.predicate) ?? "", subject: str(r.subject) ?? "", object: str(r.object) ?? "", status: str(r.status) ?? "" }));
          const existing = nodeById.get(claimId);
          if (existing) {
            const last = existing.history?.[existing.history.length - 1];
            if (last && last.status !== status) existing.history!.push({ at: seq, status, note: `Recorded status at extraction event ${ev.sequence}.` });
            for (const r of rels) if (!existing.relationships?.some((x) => x.id === r.id)) existing.relationships?.push(r);
          } else {
            add({
              id: claimId,
              kind: "claim",
              label: str(ps.claim_text) ?? claimId,
              round,
              revealAt: seq,
              ref: { table: "claims", id: claimId, eventId: ev.id },
              history: [{ at: seq, status, note: `Recorded claim status "${str(ps.status)}" (basis: ${str(ps.verification_basis) ?? "unknown"}). Not a verification of truth.` }],
              relationships: rels,
              body: `Epistemic status recorded by the backend: ${str(ps.epistemic_status) ?? "unknown"}.`,
            });
          }
          if (docId && nodeById.has(pid)) {
            const k: EdgeKind = stances.includes("contradicts") ? "contradicts" : stances.includes("supports") ? "supports" : "mentions";
            edge(pid, claimId, k);
          }
        });
        if (p.blocker) notes.push(`Extraction event ${ev.sequence} recorded a blocker: ${String(p.blocker)}`);
        focus = passages.length ? (str(passages[0].claim_id) ?? undefined) : undefined;
        break;
      }
      case "convergence":
      case "blocked": {
        kind = "decide";
        const stop = p.stop === true;
        const reason = str(p.stop_reason);
        const outcome: DecisionOutcome = ev.stage === "blocked" ? "blocked" : stop ? (reason ? STOP_OUTCOME[reason] ?? "stop-uncertain" : "stop-uncertain") : "continue";
        const approved = str(p.approved_query) ?? null;
        const alternatives = asArray(p.alternatives).map(asRecord);
        const label = ev.stage === "blocked" ? `Blocked: ${short(str(p.blocker) ?? ev.summary, 70)}` : stop ? `Stop: ${STOP_LABEL[reason ?? ""] ?? reason ?? "reason not recorded"}` : `Continue${approved ? `: ${short(approved, 70)}` : ""}`;
        const rationale: { label: string; value: string }[] = [];
        if (str(p.decision_source)) rationale.push({ label: "Decision source", value: str(p.decision_source)! });
        if (reason) rationale.push({ label: "Stop reason", value: reason });
        if (str(p.rationale)) rationale.push({ label: "Recorded rationale", value: str(p.rationale)! });
        if (approved) rationale.push({ label: "Approved query", value: approved });
        if (alternatives.length) rationale.push({ label: "Alternatives", value: alternatives.map((a) => str(a.query) ?? "").filter(Boolean).join(" · ") });
        add({ id: ev.id, kind: "decision", label, outcome, did: ev.summary, rationale, round: Math.max(round, 1), revealAt: seq, ref: { table: "investigation_events", id: ev.id, eventId: ev.id } });
        for (const q of asArray(p.unresolved_questions)) {
          const qid = ensureQuestion(q, seq);
          if (qid) edge(qid, ev.id, "considered");
        }
        lastApprovedQuery = approved;
        focus = ev.id;
        break;
      }
      case "checkpoint": {
        kind = "checkpoint";
        const cpId = ev.checkpoint_id ?? str(p.checkpoint_id) ?? null;
        const rec = cpId ? cpById.get(cpId) : undefined;
        const nodeId = rec ? rec.id : `${ev.id}`;
        const state = asRecord(rec?.state_json);
        const openQs = asArray(p.open_questions).length ? asArray(p.open_questions) : asArray(state.unresolved_questions);
        for (const q of openQs) ensureQuestion(q, seq);
        const frontier = openQs.length || "open_questions" in p ? openQs.map((q) => questionKey(q)?.text).filter((x): x is string => !!x) : null;
        const cpShort = `C${checkpoints.length + 1}`;
        const cp: Checkpoint = {
          id: rec?.id ?? nodeId,
          short: cpShort,
          label: rec?.summary || ev.summary || "Checkpoint",
          seq,
          nodeId,
          persisted: true,
          resumable: !!rec,
          forkable: !!rec,
          notResumableReason: rec ? undefined : "A checkpoint event was recorded, but no persisted checkpoint snapshot exists for it. It can be viewed in playback only.",
          status: str(state.status) ?? (rec ? "Recorded" : "Event only"),
          summary: rec?.summary || ev.summary,
          frontier,
          recordsClaims: Array.isArray(state.relationship_status) || Array.isArray(asRecord(state.snapshot).claims),
          recordsCoords: asArray(state.policy_space).length > 0,
          why: str(p.decision_rationale) || str(state.decision_rationale) || undefined,
          detail: rec ? checkpointDetail(rec, true) : undefined,
        };
        checkpoints.push(cp);
        add({ id: nodeId, kind: "checkpoint", label: cp.label, sub: cpShort, checkpointId: cp.id, round: Math.max(round, 1), revealAt: seq, ref: rec ? { table: "checkpoints", id: rec.id, eventId: ev.id } : { table: "investigation_events", id: ev.id, eventId: ev.id } });
        const prevDecision = [...nodes].reverse().find((n) => n.kind === "decision" && n.round === round && n.revealAt < seq);
        if (prevDecision) edge(prevDecision.id, nodeId, "order");
        if (!rec) notes.push(`Checkpoint event ${ev.sequence} has no persisted checkpoint record${cpId ? ` (${cpId})` : ""}.`);
        focus = nodeId;
        break;
      }
      default:
        notes.push(`Event ${ev.sequence} has stage "${ev.stage}", which this view does not draw. It remains on the timeline.`);
    }
    events.push({
      seq,
      kind,
      round: Math.max(round, 1),
      label: ev.summary,
      detail: `${ev.stage} · backend event #${ev.sequence} · ${ev.id}`,
      focus,
      id: ev.id,
      backendSequence: ev.sequence,
      stage: ev.stage,
      createdAt: ev.created_at,
      checkpointId: ev.checkpoint_id ?? null,
    });
  });

  // Persisted checkpoints without a recorded event: listed, never placed on the timeline.
  const placed = new Set(checkpoints.map((c) => c.id));
  const unplaced: Checkpoint[] = exp.checkpoints
    .filter((c) => !placed.has(c.id))
    .map((c, i) => ({
      id: c.id,
      short: `U${i + 1}`,
      label: c.summary || c.id,
      seq: -1,
      nodeId: c.id,
      persisted: true,
      resumable: false,
      forkable: false,
      notResumableReason: "This snapshot was persisted without a recorded checkpoint event, so its place in the timeline is unknown.",
      status: str(asRecord(c.state_json).status) ?? "Recorded",
      summary: c.summary,
      frontier: null,
      recordsClaims: false,
      recordsCoords: asArray(asRecord(c.state_json).policy_space).length > 0,
      detail: checkpointDetail(c, false),
    }));
  if (unplaced.length) notes.push(`${unplaced.length} persisted checkpoint(s) have no checkpoint event and are listed separately.`);

  // Recorded Focus coordinates per checkpoint. Invalid points are dropped, never repaired.
  const policyByCheckpoint: Record<string, PolicyPointView[]> = {};
  const passagePolicyByCheckpoint: Record<string, PolicyPointView[]> = {};
  for (const c of exp.checkpoints) {
    const pts: PolicyPointView[] = [];
    let invalid = 0;
    let foreign = 0;
    for (const r of asArray(asRecord(c.state_json).policy_space_passages)) {
      const parsed = PolicyPoint.safeParse(r);
      const spanId = str(asRecord(r).span_id);
      const owner = str(asRecord(r).investigation_id);
      if (!parsed.success || !spanId) {
        invalid += 1;
        continue;
      }
      // A point recorded for another investigation is never plotted here.
      if (owner && owner !== inv.id) {
        foreign += 1;
        continue;
      }
      const d = parsed.data;
      pts.push({ evidenceId: d.evidence_id, spanId, documentId: d.document_id ?? null, title: d.title, url: d.url, passage: d.passage, intent: d.intent_alignment, novelty: d.novelty, unresolved: d.unresolved_alignment, action: d.action, reason: d.reason });
    }
    if (invalid) notes.push(`${invalid} passage coordinate(s) at ${c.id} failed validation and are not plotted.`);
    if (foreign) notes.push(`${foreign} passage coordinate(s) at ${c.id} belong to another investigation and are not plotted.`);
    if (pts.length) passagePolicyByCheckpoint[c.id] = pts;
  }
  for (const c of exp.checkpoints) {
    const raw = asArray(asRecord(c.state_json).policy_space);
    const pts: PolicyPointView[] = [];
    let dropped = 0;
    for (const r of raw) {
      const parsed = PolicyPoint.safeParse(r);
      if (!parsed.success) {
        dropped += 1;
        continue;
      }
      const d = parsed.data;
      pts.push({ evidenceId: d.evidence_id, documentId: d.document_id ?? null, title: d.title, url: d.url, passage: d.passage, intent: d.intent_alignment, novelty: d.novelty, unresolved: d.unresolved_alignment, action: d.action, reason: d.reason });
    }
    if (dropped) notes.push(`${dropped} policy coordinate(s) at ${c.id} failed validation and are not plotted.`);
    policyByCheckpoint[c.id] = pts;
  }

  if (tr.incomplete && tr.incomplete_reason) notes.unshift(tr.incomplete_reason);
  if (exp.contradictions.length) notes.push(`${exp.contradictions.length} recorded contradiction(s) are not tied to a trace event; see the overview.`);
  if (!sorted.length) notes.unshift("No investigation events were recorded, so there is nothing to play back.");

  const trace: Trace = {
    lanternId: inv.id,
    origin: "persisted",
    question: rootQuestion,
    incompleteReason: tr.incomplete ? tr.incomplete_reason ?? "Backend marked this trace incomplete." : null,
    nodes,
    edges,
    events,
    checkpoints,
    rounds,
    notes: Array.from(new Set(notes)),
    unplacedCheckpoints: unplaced,
    policyByCheckpoint,
    passagePolicyByCheckpoint,
    branchId: tr.branch_id ?? null,
    branchObjective: tr.branch_objective ?? null,
    attachments: { claims: tr.attached_claims, sources: tr.attached_sources },
  };

  const lantern: Lantern = {
    id: inv.id,
    title: short(tr.branch_objective || rootQuestion, 80),
    objective: tr.branch_objective || rootQuestion,
    status: lanternStatus(inv.status, inv.stop_reason, tr.lifecycle),
    parentId: tr.parent_investigation_id ?? undefined,
    fromCheckpoint: tr.from_checkpoint_id ?? undefined,
  };
  const related: Lantern[] = [];
  if (tr.parent_investigation_id) {
    related.push({ id: tr.parent_investigation_id, title: `Parent ${tr.parent_investigation_id}`, objective: "", status: "recorded-branch" });
  }
  for (const b of branches) {
    if (b.parent_investigation_id !== inv.id) continue;
    related.push({
      id: b.child_investigation_id,
      title: b.objective ? short(b.objective, 60) : `${b.intervention_type} · ${b.child_investigation_id}`,
      objective: b.objective,
      status: "recorded-branch",
      parentId: inv.id,
      fromCheckpoint: b.from_checkpoint_id,
    });
  }
  return { trace, lantern, related, source: exp };
}

function checkpointDetail(rec: ExplorationExportT["checkpoints"][number], hasEvent: boolean): CheckpointDetail {
  const state = asRecord(rec.state_json);
  const snap = asRecord(state.snapshot);
  const counts: Record<string, number> = {};
  for (const [k, v] of Object.entries(snap)) if (Array.isArray(v)) counts[k] = v.length;
  const documents = asArray(snap.documents)
    .map(asRecord)
    .map((d) => ({ id: str(d.id) ?? "", title: str(d.title) ?? "", url: str(d.canonical_url) ?? "" }))
    .filter((d) => d.id);
  const relationshipStatus = asArray(state.relationship_status)
    .map(asRecord)
    .map((r) => ({ id: str(r.id) ?? "", predicate: str(r.predicate) ?? "", object: str(r.object_name) ?? "", status: str(r.status) ?? "" }));
  const questions = asArray(state.unresolved_questions).map((q) => questionKey(q)?.text).filter((x): x is string => !!x);
  const budget = asRecord(state.budget);
  const numericBudget: Record<string, number> = {};
  for (const [k, v] of Object.entries(budget)) if (typeof v === "number") numericBudget[k] = v;
  return {
    step: rec.step,
    createdAt: rec.created_at,
    stopReason: rec.stop_reason ?? null,
    status: str(state.status),
    decisionSource: str(state.decision_source) ?? null,
    decisionRationale: str(state.decision_rationale),
    counts,
    documents,
    relationshipStatus,
    questions,
    budget: Object.keys(numericBudget).length ? numericBudget : undefined,
    policyPointCount: asArray(state.policy_space).length,
    hasEvent,
  };
}
