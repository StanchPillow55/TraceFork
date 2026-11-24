import { useState } from "react";
import { Icon, InfoBlock, Tag, childEvidence, parentEvidence, projectEvidence } from "../shared";
import { QUESTION, STATUS_META, summarize } from "./data";
import { policyForDocument, policyForSpan, toEvidencePoint } from "../persisted/policy";
import { isGroundedObjective } from "../api/grounding";
import type { EvidencePoint } from "../shared";
import {
  CLAIM_LABEL,
  CLAIM_TONE,
  OUTCOME,
  ORIGIN_LABEL,
  claimSupport,
  docOf,
  docsOf,
  groupOfNode,
  incoming,
  nodeStatus,
  outgoing,
  passagesOf,
  queryOfGroup,
} from "./graph";
import type { Checkpoint, ComposerDraft, Lantern, Trace, TraceNode } from "./types";

export const READ_ONLY_REASON = "Branching is unavailable for this investigation.";

export interface BranchingState {
  pending: boolean;
  error: string | null;
  /** Fork & Investigate runs billable research; only allowed when explicitly enabled. */
  allowInvestigate: boolean;
  investigateDisabledReason?: string;
}

interface Props {
  /** Original research question (from the trace; demo traces fall back to the sample question). */
  question?: string;
  /** Read-only: every branching action is disabled with an explanation. */
  readOnly?: boolean;
  readOnlyReason?: string;
  /** Persisted branching state (live mode). */
  branching?: BranchingState;
  lantern: Lantern;
  trace: Trace;
  node: TraceNode | null;
  forkStub: Lantern | null;
  cursor: number;
  latest: number;
  snapshot: Checkpoint | null;
  composer: ComposerDraft | null;
  expanded: Set<string>;
  onSelect: (id: string) => void;
  onFocus: (ids: string[]) => void;
  onToggleExpand: (id: string) => void;
  onSeek: (seq: number) => void;
  onLoadSnapshot: (cpId: string) => void;
  onLatest: () => void;
  onCompose: (d: ComposerDraft | null) => void;
  onCreate: (d: ComposerDraft, mode: "saved" | "investigate") => void;
  onCompare: (a: string, b: string) => void;
  onOpen3D: (evId: string | null) => void;
  onOpenLantern: (id: string) => void;
  onExpandRound: (r: number) => void;
  onClose: () => void;
}

const KIND_LABEL: Record<string, string> = {
  intent: "Research intent",
  inherited: "Inherited context",
  round: "Exploration round · recorded activity",
  query: "Search query · agent event",
  group: "Retrieved source group",
  doc: "Retrieved source",
  passage: "Extracted passage",
  claim: "Claim · what the evidence shows",
  contradiction: "Contradicting evidence",
  relation: "Relationship between claims",
  question: "Unresolved question",
  decision: "Convergence decision",
  checkpoint: "Recorded checkpoint",
  pending: "In progress",
};

export function TraceInspector(p: Props) {
  const { trace, node, cursor } = p;
  if (p.composer) return <Composer {...p} draft={p.composer} />;
  if (p.forkStub) return <ForkStubPanel {...p} child={p.forkStub} />;
  if (!node) return <Overview {...p} />;

  const nodeById = (id: string) => trace.nodes.find((n) => n.id === id)!;
  const Link = ({ id, children }: { id: string; children?: React.ReactNode }) => {
    const n = nodeById(id);
    const later = n.revealAt > cursor;
    return (
      <button className={`ix-link k-${n.kind} ${later ? "later" : ""}`} onClick={() => p.onSelect(id)} title={later ? "Recorded later in the investigation" : undefined}>
        <span className={`ix-kind-dot k-${n.kind}`} />
        <span>{children ?? n.label}</span>
        {later && <em>later</em>}
      </button>
    );
  };
  const roReason = p.readOnlyReason ?? READ_ONLY_REASON;
  const branchProps = { disabled: !!p.readOnly, title: p.readOnly ? roReason : undefined };
  const compose = (intent: ComposerDraft["intent"], claimId: string | null, sourceIds: string[], objective?: string, title?: string, cpId?: string) => {
    const claim = claimId ? nodeById(claimId) : null;
    const eligible = trace.checkpoints.filter((c) => c.forkable && c.seq <= p.latest && (!claim || claim.revealAt <= c.seq));
    const preferred =
      (cpId && eligible.find((c) => c.id === cpId)) ||
      (p.snapshot && eligible.find((c) => c.id === p.snapshot!.id)) ||
      [...eligible].reverse().find((c) => c.seq <= cursor) ||
      eligible[0] ||
      null;
    const defaults: Record<ComposerDraft["intent"], [string, string]> = {
      challenge: [
        `Challenge: ${claim?.label ?? "selected claim"}`,
        `Test whether “${claim?.label ?? "this claim"}” holds when relying only on sources that do not originate from company disclosures.`,
      ],
      confirm: [
        `Confirm: ${claim?.label ?? "selected evidence"}`,
        `Find independent confirmation for “${claim?.label ?? nodeById(sourceIds[0] ?? node.id)?.label}” from sources with a different origin.`,
      ],
      uncertainty: [`Explore: ${node.label}`, `Investigate the unresolved question: “${node.label}”`],
      custom: [`Continue from checkpoint`, `Continue the original investigation from this checkpoint in a new direction.`],
      continue: [`Continue from checkpoint`, ``],
    };
    if (p.readOnly) return;
    p.onCompose({
      intent,
      claimId,
      sourceIds,
      checkpointId: preferred?.id ?? null,
      title: title ?? defaults[intent][0],
      objective: objective ?? defaults[intent][1],
    });
  };

  const st = nodeStatus(node, cursor);
  const futureNote = node.revealAt > cursor && (
    <div className="ix-note">
      <Icon name="history" size={14} />
      <p>This item is recorded later (event {node.revealAt + 1}). It is not part of the state you are viewing.</p>
    </div>
  );

  let body: React.ReactNode = null;
  let actions: React.ReactNode = null;

  switch (node.kind) {
    case "intent":
    case "inherited": {
      const s = summarize(trace, cursor);
      body = (
        <>
          <Tag tone={node.kind === "intent" ? "blue" : "violet"}>{node.kind === "intent" ? "Recorded at start" : "Inherited · read-only"}</Tag>
          <div className="claim-context">
            <small>Original research question</small>
            <p>{p.question ?? QUESTION}</p>
          </div>
          {node.body && (
            <InfoBlock label={node.kind === "intent" ? "Research intent" : "What was inherited"}>
              <p>{node.body}</p>
            </InfoBlock>
          )}
          <InfoBlock label="Recorded so far">
            <div className="snapshot-stats">
              <span><b>{s.rounds}</b> rounds</span>
              <span><b>{s.sources}</b> sources</span>
              <span><b>{s.claims}</b> claims</span>
            </div>
          </InfoBlock>
        </>
      );
      actions = (
        <button onClick={() => p.onSeek(0)}>
          <Icon name="skipStart" size={14} /> Replay from the start
        </button>
      );
      break;
    }
    case "round": {
      const rq = trace.nodes.filter((n) => n.round === node.round && n.kind === "query" && n.revealAt <= cursor);
      const groups = trace.nodes.filter((n) => n.round === node.round && n.kind === "group" && n.revealAt <= cursor);
      const docs = groups.flatMap((g) => docsOf(trace, g.id));
      const passages = docs.flatMap((d) => passagesOf(trace, d.id)).filter((x) => x.revealAt <= cursor);
      const pol = (k: string) => passages.filter((x) => x.policy === k).length;
      const prompted = incoming(trace, node.id, "prompted").map((e) => nodeById(e.from));
      const prevDecision = incoming(trace, node.id, "order").map((e) => nodeById(e.from)).find((n) => n.kind === "decision");
      const decision = trace.nodes.find((n) => n.round === node.round && n.kind === "decision");
      body = (
        <>
          <Tag tone="blue">Recorded exploration · round {node.round}</Tag>
          <InfoBlock label="Why this direction was taken">
            {prevDecision ? (
              <p>
                The previous recorded decision — <b>{prevDecision.label}</b> — requested this round.
                {prompted.length > 0 && " It was prompted by the open question below."}
              </p>
            ) : (
              <p>First exploration round, planned directly from the research intent.</p>
            )}
            {prompted.map((q) => (
              <Link key={q.id} id={q.id} />
            ))}
          </InfoBlock>
          <InfoBlock label="Searches the agent issued">
            {rq.map((q) => (
              <Link key={q.id} id={q.id}>“{q.label}”</Link>
            ))}
          </InfoBlock>
          <InfoBlock label="Recorded activity">
            <ul>
              <li>{rq.length} search queries issued</li>
              <li>{docs.filter((d) => d.revealAt <= cursor).length} documents retrieved</li>
              <li>
                {passages.length} passages scored — {pol("consider")} Consider · {pol("investigate")} Investigate · {pol("archive")} Archive
              </li>
            </ul>
          </InfoBlock>
          {decision && decision.revealAt <= cursor && (
            <InfoBlock label="How the round ended">
              <Link id={decision.id} />
            </InfoBlock>
          )}
          <div className="reasoning-note">
            <Icon name="source" size={14} />
            <p>Rounds are recorded events. Viewing one is historical playback, not a resumable state.</p>
          </div>
        </>
      );
      actions = (
        <>
          <button onClick={() => p.onSeek(node.revealAt)}>
            <Icon name="history" size={14} /> Replay this round
          </button>
          <button onClick={() => p.onExpandRound(node.round)}>
            <Icon name="layers" size={14} /> Expand all sources
          </button>
        </>
      );
      break;
    }
    case "query": {
      const group = outgoing(trace, node.id, "retrieved").map((e) => nodeById(e.to))[0];
      const prompted = incoming(trace, node.id, "prompted").map((e) => nodeById(e.from));
      body = (
        <>
          <Tag tone="blue">Agent event · search issued</Tag>
          <blockquote className="ix-query-quote">“{node.label}”</blockquote>
          <InfoBlock label="Recorded details">
            <ul>
              <li>Provider: {node.provider}</li>
              <li>Issued at event {node.revealAt + 1} of round {node.round}</li>
              {group && <li>{docsOf(trace, group.id).length} documents returned</li>}
            </ul>
          </InfoBlock>
          {prompted.length > 0 && (
            <InfoBlock label="Prompted by unresolved question">
              {prompted.map((q) => (
                <Link key={q.id} id={q.id} />
              ))}
            </InfoBlock>
          )}
          {group && (
            <InfoBlock label="Results">
              <Link id={group.id} />
            </InfoBlock>
          )}
        </>
      );
      actions = group ? (
        <button
          className="primary"
          onClick={() => {
            if (!p.expanded.has(group.id)) p.onToggleExpand(group.id);
            p.onSelect(group.id);
          }}
        >
          <Icon name="layers" size={14} /> Open source group
        </button>
      ) : null;
      break;
    }
    case "group": {
      const docs = docsOf(trace, node.id).filter((d) => d.revealAt <= cursor);
      const q = queryOfGroup(trace, node.id);
      const shared = trace.edges.filter((e) => e.kind === "sharedOrigin" && docs.some((d) => d.id === e.from));
      const independent = docs.filter((d) => d.origin !== "derivative").length;
      body = (
        <>
          <Tag tone="blue">Retrieved in round {node.round}</Tag>
          {q && (
            <InfoBlock label="Retrieval query">
              <Link id={q.id}>“{q.label}”</Link>
            </InfoBlock>
          )}
          <InfoBlock label={`Documents (${docs.length})`}>
            {docs.map((d) => (
              <Link key={d.id} id={d.id}>
                <span className={`ix-origin inline o-${d.origin}`}>{d.origin?.[0].toUpperCase()}</span> {d.label}
              </Link>
            ))}
          </InfoBlock>
          <InfoBlock label="Provenance">
            <p>
              {independent} of {docs.length} documents have an independent origin.
              {shared.length > 0 && ` ${shared.length} recorded shared-origin link${shared.length > 1 ? "s" : ""}:`}
            </p>
            {shared.map((e) => (
              <p key={e.id} className="ix-shared-row">
                <span className="ix-shared-badge">shared origin</span> {nodeById(e.from).label} → {nodeById(e.to).label}
              </p>
            ))}
          </InfoBlock>
        </>
      );
      actions = (
        <>
          <button onClick={() => p.onToggleExpand(node.id)}>
            <Icon name={p.expanded.has(node.id) ? "collapse" : "expand"} size={14} /> {p.expanded.has(node.id) ? "Collapse" : "Expand"} documents
          </button>
          <button {...branchProps} className="primary" onClick={() => compose("confirm", null, docs.map((d) => d.id))}>
            <Icon name="branch" size={14} /> Find independent confirmation
          </button>
        </>
      );
      break;
    }
    case "doc":
    case "passage": {
      const doc = node.kind === "doc" ? node : docOf(trace, node.id)!;
      const group = groupOfNode(trace, doc.id);
      const q = group ? queryOfGroup(trace, group.id) : undefined;
      const passages = passagesOf(trace, doc.id).filter((x) => x.revealAt <= cursor);
      const passage = node.kind === "passage" ? node : passages.find((x) => x.evId) ?? passages[0];
      const supportsFrom = [doc.id, ...passages.map((x) => x.id)];
      const claims = trace.edges.filter((e) => supportsFrom.includes(e.from) && e.kind === "supports").map((e) => nodeById(e.to));
      const raised = trace.edges.filter((e) => supportsFrom.includes(e.from) && e.kind === "raised").map((e) => nodeById(e.to));
      const sharedOut = trace.edges.filter((e) => e.kind === "sharedOrigin" && (e.from === doc.id || e.to === doc.id));
      const corroborated = claims.some((c) => (c.independent ?? 0) >= 2);
      if (trace.origin === "persisted") {
        body = <PersistedSource trace={trace} doc={doc} node={node} cursor={cursor} passages={passages} Link={Link} onSelect={p.onSelect} onOpen3D={p.onOpen3D} />;
        break;
      }
      body = (
        <>
          <div className="ix-tag-row">
            <Tag tone={doc.origin === "derivative" ? "gray" : "blue"}>{ORIGIN_LABEL[doc.origin ?? "secondary"]} source</Tag>
            {passage?.policy && <Tag tone={passage.policy === "consider" ? "green" : passage.policy === "investigate" ? "amber" : "gray"}>{passage.policy}</Tag>}
          </div>
          {node.kind === "passage" && <h3 className="ix-doc-title">From: {doc.label}</h3>}
          <InfoBlock label="Publisher & origin">
            <p>
              {doc.publisher} · {doc.docType}
            </p>
          </InfoBlock>
          {q && (
            <InfoBlock label="Retrieval query">
              <Link id={q.id}>“{q.label}”</Link>
            </InfoBlock>
          )}
          <InfoBlock label={node.kind === "passage" ? "Extracted passage" : `Relevant passages (${passages.length})`}>
            {(node.kind === "passage" ? [node] : passages).map((x) => (
              <button key={x.id} className={`ix-passage-quote ${x.id === p.node?.id ? "current" : ""}`} onClick={() => p.onSelect(x.id)}>
                <i className={x.policy ?? "unscored"} />
                <span>“{x.text}”</span>
              </button>
            ))}
            {passage?.relevance && <p className="ix-relevance">Relation to the question: {passage.relevance}</p>}
          </InfoBlock>
          <InfoBlock label="Recorded policy action">
            {passage?.coords || passage?.evId ? (
              <Mini3D points={p.lantern.id === "power" ? childEvidence : parentEvidence} selectedId={passage.evId!} onOpen={() => p.onOpen3D(passage.evId!)} />
            ) : (
              <div className="ix-no-coords">
                <Icon name="cube" size={15} />
                <p>{passage?.coordNote ?? "No recorded coordinates for this source."} Coordinates are never estimated.</p>
              </div>
            )}
          </InfoBlock>
          <InfoBlock label="Relationships to other evidence">
            {claims.length === 0 && raised.length === 0 && sharedOut.length === 0 && <p>No recorded relationships.</p>}
            {claims.map((c) => (
              <Link key={c.id} id={c.id}>
                {c.kind === "contradiction" ? "Supports contradiction: " : "Supports: "}
                {c.label}
              </Link>
            ))}
            {raised.map((c) => (
              <Link key={c.id} id={c.id}>Raised: {c.label}</Link>
            ))}
            {sharedOut.map((e) => (
              <Link key={e.id} id={e.from === doc.id ? e.to : e.from}>
                Shared origin ({e.label}): {nodeById(e.from === doc.id ? e.to : e.from).label}
              </Link>
            ))}
          </InfoBlock>
          <InfoBlock label="Independent corroboration">
            <p>
              {doc.origin === "derivative"
                ? "This document restates another source; it adds no independent corroboration."
                : corroborated
                  ? "At least one claim it supports has another independent source."
                  : "No independent corroboration recorded for the claims it supports."}
            </p>
          </InfoBlock>
          {futureNote}
        </>
      );
      actions = (
        <>
          <button {...branchProps} onClick={() => compose("confirm", claims.find((c) => c.kind === "claim")?.id ?? null, [doc.id])}>
            <Icon name="search" size={14} /> Find independent confirmation
          </button>
          <button {...branchProps} className="primary" onClick={() => compose("challenge", claims.find((c) => c.kind === "claim")?.id ?? null, [doc.id])}>
            <Icon name="branch" size={14} /> Create child Lantern
          </button>
        </>
      );
      break;
    }
    case "claim":
    case "contradiction":
    case "relation": {
      const { supports, contradictions } = claimSupport(trace, node.id, cursor);
      const challenged = outgoing(trace, node.id, "contradicts").map((e) => nodeById(e.to));
      const raises = outgoing(trace, node.id, "raised").map((e) => nodeById(e.to)).filter((n) => n.revealAt <= cursor);
      const history = (node.history ?? []).filter((h) => h.at <= cursor);
      const linkedClaims = node.kind === "relation" ? incoming(trace, node.id, "supports").map((e) => nodeById(e.from)) : [];
      const sources = supports.map((s) => (s.kind === "passage" ? docOf(trace, s.id)! : s));
      body = (
        <>
          <div className="ix-tag-row">
            {node.kind === "claim" && st && <Tag tone={CLAIM_TONE[st.status]}>{CLAIM_LABEL[st.status]}</Tag>}
            {node.kind === "contradiction" && <Tag tone="red">Contradicting evidence</Tag>}
            {node.kind === "relation" && <Tag tone="teal">Recorded relationship</Tag>}
            {node.independent !== undefined && <span className="ix-indep">{node.independent} independent origin{node.independent === 1 ? "" : "s"}</span>}
          </div>
          <div className="claim-context">
            <small>Attached to original question</small>
            <p>{p.question ?? QUESTION}</p>
          </div>
          {node.body && (
            <InfoBlock label="What the evidence shows">
              <p>{node.body}</p>
            </InfoBlock>
          )}
          {history.length > 1 && (
            <InfoBlock label="Recorded revisions">
              {history.map((h, i) => (
                <div key={i} className="ix-rev">
                  <Tag tone={CLAIM_TONE[h.status]}>{CLAIM_LABEL[h.status]}</Tag>
                  <span>
                    Event {h.at + 1}: {h.note}
                  </span>
                </div>
              ))}
            </InfoBlock>
          )}
          {node.relationships && node.relationships.length > 0 && (
            <InfoBlock label={`Recorded relationships (${node.relationships.length})`}>
              {node.relationships.map((r) => (
                <div key={r.id} className="ix-rel-row">
                  <span>
                    {r.subject} <em>{r.predicate}</em> {r.object}
                  </span>
                  <Tag tone={r.status === "supported" ? "teal" : r.status === "contradicted" ? "red" : "gray"}>{r.status}</Tag>
                  <code>{r.id}</code>
                </div>
              ))}
            </InfoBlock>
          )}
          {trace.origin === "persisted" && (() => {
            const other = trace.edges.filter((e) => e.to === node.id && (e.kind === "contradicts" || e.kind === "mentions")).map((e) => ({ e, n: nodeById(e.from) })).filter((x) => x.n.revealAt <= cursor);
            return other.length ? (
              <InfoBlock label="Passages citing this claim without support">
                {other.map(({ e, n }) => (
                  <Link key={e.id} id={n.id}>
                    {e.kind === "contradicts" ? "Contradicting stance: " : "Mentions only: "}“{n.text ?? n.label}”
                  </Link>
                ))}
              </InfoBlock>
            ) : null;
          })()}
          {linkedClaims.length > 0 && (
            <InfoBlock label="Claims it connects">
              {linkedClaims.map((c) => (
                <Link key={c.id} id={c.id} />
              ))}
            </InfoBlock>
          )}
          {sources.length > 0 && (
            <InfoBlock label={`Supporting evidence (${sources.length})`}>
              {supports.map((s, i) => (
                <Link key={s.id} id={s.id}>
                  <span className={`ix-origin inline o-${sources[i].origin}`}>{sources[i].origin?.[0].toUpperCase()}</span> {s.kind === "passage" ? `“${s.text}”` : s.label}
                </Link>
              ))}
            </InfoBlock>
          )}
          {contradictions.length > 0 && (
            <InfoBlock label="Contradicting evidence">
              {contradictions.map((c) => (
                <Link key={c.id} id={c.id} />
              ))}
            </InfoBlock>
          )}
          {challenged.length > 0 && (
            <InfoBlock label="Challenges">
              {challenged.map((c) => (
                <Link key={c.id} id={c.id} />
              ))}
            </InfoBlock>
          )}
          {raises.length > 0 && (
            <InfoBlock label="Related uncertainty">
              {raises.map((c) => (
                <Link key={c.id} id={c.id} />
              ))}
            </InfoBlock>
          )}
          <div className="ix-note subtle">
            <Icon name="alert" size={13} />
            <p>Support counts describe recorded evidence, not whether the claim is true.</p>
          </div>
          {futureNote}
          {node.kind === "claim" && (
            <div className="ix-claim-actions">
              <button onClick={() => p.onFocus([node.id, ...supports.map((s) => s.id)])}>
                <Icon name="target" size={14} /> Inspect supporting evidence
              </button>
              <button {...branchProps} onClick={() => compose("confirm", node.id, sources.map((s) => s.id))}>
                <Icon name="search" size={14} /> Find independent confirmation
              </button>
              <button {...branchProps} onClick={() => compose("challenge", node.id, sources.map((s) => s.id))}>
                <Icon name="alert" size={14} /> Challenge this claim
              </button>
              <button
                disabled={raises.length === 0 && contradictions.length === 0}
                onClick={() => (raises[0] ? p.onSelect(raises[0].id) : contradictions[0] && p.onSelect(contradictions[0].id))}
              >
                <Icon name="knowledge" size={14} /> Explore related uncertainty
              </button>
            </div>
          )}
        </>
      );
      actions =
        node.kind === "claim" ? (
          <button {...branchProps} className="primary" onClick={() => compose("challenge", node.id, sources.map((s) => s.id))}>
            <Icon name="branch" size={14} /> Create child Lantern
          </button>
        ) : null;
      break;
    }
    case "question": {
      const raisedBy = incoming(trace, node.id, "raised").map((e) => nodeById(e.from));
      const prompted = outgoing(trace, node.id, "prompted").map((e) => nodeById(e.to));
      const history = (node.history ?? []).filter((h) => h.at <= cursor);
      body = (
        <>
          {st && <Tag tone={CLAIM_TONE[st.status]}>{CLAIM_LABEL[st.status]}</Tag>}
          <InfoBlock label="Raised by">
            {raisedBy.map((r) => (
              <Link key={r.id} id={r.id} />
            ))}
          </InfoBlock>
          <InfoBlock label="Status over time">
            {history.map((h, i) => (
              <div key={i} className="ix-rev">
                <Tag tone={CLAIM_TONE[h.status]}>{CLAIM_LABEL[h.status]}</Tag>
                <span>
                  Event {h.at + 1}: {h.note}
                </span>
              </div>
            ))}
          </InfoBlock>
          <InfoBlock label="Follow-up searches it prompted">
            {prompted.length ? prompted.map((r) => <Link key={r.id} id={r.id} />) : <p>None recorded — it remains on the frontier.</p>}
          </InfoBlock>
          {futureNote}
        </>
      );
      actions = (
        <button {...branchProps} className="primary" onClick={() => compose("uncertainty", null, raisedBy.map((r) => (r.kind === "passage" ? docOf(trace, r.id)?.id ?? r.id : r.id)))}>
          <Icon name="branch" size={14} /> Investigate in a child Lantern
        </button>
      );
      break;
    }
    case "decision": {
      const o = OUTCOME[node.outcome ?? "continue"];
      const considered = incoming(trace, node.id, "considered").map((e) => nodeById(e.from));
      const next = outgoing(trace, node.id, "order").map((e) => nodeById(e.to))[0];
      const cpBefore = [...trace.checkpoints].reverse().find((c) => c.seq < node.revealAt && c.seq > (trace.nodes.find((n) => n.kind === "round" && n.round === node.round)?.revealAt ?? 0));
      body = (
        <>
          <Tag tone={o.tone}>{o.label}</Tag>
          <InfoBlock label="What the agent did">
            <p>{node.did}</p>
          </InfoBlock>
          <InfoBlock label="Recorded controller rationale">
            <dl className="ix-rationale">
              {node.rationale?.map((r) => (
                <div key={r.label}>
                  <dt>{r.label}</dt>
                  <dd>{r.value}</dd>
                </div>
              ))}
            </dl>
          </InfoBlock>
          <InfoBlock label="Evidence considered">
            {considered.map((c) => (
              <Link key={c.id} id={c.id} />
            ))}
          </InfoBlock>
          {next && (
            <InfoBlock label="What happened next">
              <Link id={next.id} />
            </InfoBlock>
          )}
          <div className="reasoning-note">
            <Icon name="alert" size={14} />
            <p>
              This explanation is assembled from recorded controller fields — not a transcript of private model reasoning.
              {node.outcome?.startsWith("stop") && " Stopping (convergence) does not mean the conclusions were verified."}
            </p>
          </div>
          {futureNote}
        </>
      );
      actions = cpBefore ? (
        <button onClick={() => p.onSelect(cpBefore.nodeId)}>
          <span className="tiny-gem" /> Checkpoint before this decision · {cpBefore.short}
        </button>
      ) : null;
      break;
    }
    case "checkpoint": {
      const cp = trace.checkpoints.find((c) => c.id === node.checkpointId)!;
      const s = summarize(trace, cp.seq);
      const claimsAt = trace.nodes.filter((n) => n.kind === "claim" && n.revealAt <= cp.seq);
      const later = trace.checkpoints.find((c) => c.seq > cp.seq && c.seq <= p.latest);
      const loaded = p.snapshot?.id === cp.id;
      const decisionAfter = trace.nodes.find((n) => n.kind === "decision" && n.revealAt > cp.seq && n.round === node.round);
      const decisionBefore = trace.nodes.find((n) => n.kind === "decision" && n.revealAt < cp.seq && n.round === node.round);
      body = (
        <>
          <div className="checkpoint-hero ix-cp-hero">
            <span className="large-diamond" />
            <div className="ix-tag-row center">
              <Tag tone="amber">{cp.short} · Persisted</Tag>
              <Tag tone={cp.resumable ? "teal" : "gray"}>{cp.resumable ? "Resumable" : "View only"}</Tag>
            </div>
            <h3>{cp.summary}</h3>
          </div>
          {!cp.resumable && (
            <div className="ix-note warn">
              <Icon name="alert" size={14} />
              <p>{cp.notResumableReason}</p>
            </div>
          )}
          <InfoBlock label="Checkpoint snapshot">
            <div className="snapshot-stats">
              <span><b>{s.sources}</b> sources</span>
              <span><b>{cp.recordsClaims ? s.claims : "—"}</b> claims</span>
              <span><b>{cp.recordsClaims ? s.openQuestions : "—"}</b> open questions</span>
            </div>
            <p>Status at this point: {cp.status}</p>
          </InfoBlock>
          {cp.detail && <RecordedSnapshot cp={cp} policyCount={trace.policyByCheckpoint?.[cp.id]?.length ?? 0} />}
          <InfoBlock label="Recorded research frontier">
            {cp.frontier === null ? (
              <p className="ix-muted">Not recorded at this checkpoint.</p>
            ) : cp.frontier.length === 0 ? (
              <p>No open frontier recorded.</p>
            ) : (
              <ul>
                {cp.frontier.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            )}
          </InfoBlock>
          <InfoBlock label="Why the agent continued or stopped">
            {cp.why ? <p>{cp.why}</p> : <p className="ix-muted">No decision was recorded at this checkpoint.</p>}
            {(decisionAfter ?? decisionBefore) && <Link id={(decisionAfter ?? decisionBefore)!.id} />}
          </InfoBlock>
          {cp.recordsClaims && claimsAt.length > 0 && (
            <InfoBlock label="Challenge a claim from this state">
              {claimsAt.map((c) => (
                <button key={c.id} className="ix-link k-claim" disabled={!cp.forkable || !!p.readOnly} title={branchProps.title} onClick={() => compose("challenge", c.id, [], undefined, undefined, cp.id)}>
                  <span className="ix-kind-dot k-claim" />
                  <span>{c.label}</span>
                  <em>{CLAIM_LABEL[nodeStatus(c, cp.seq)?.status ?? "supported"]}</em>
                </button>
              ))}
            </InfoBlock>
          )}
        </>
      );
      actions = (
        <>
          <button className={loaded ? "active" : ""} onClick={() => p.onLoadSnapshot(cp.id)}>
            <Icon name="eye" size={14} /> {loaded ? "Snapshot loaded" : "View historical snapshot"}
          </button>
          <button onClick={p.onLatest} disabled={cursor >= p.latest && !p.snapshot}>
            Return to latest
          </button>
          <button disabled={!later} title={later ? undefined : "No later checkpoint recorded"} onClick={() => later && p.onCompare(cp.id, later.id)}>
            <Icon name="compare" size={14} /> {later ? `Compare with ${later.short}` : "No later checkpoint"}
          </button>
          <button
            disabled={!cp.resumable || !!p.readOnly}
            title={p.readOnly ? roReason : cp.resumable ? undefined : "Not resumable: no controller state was persisted"}
            onClick={() => compose("continue", null, [], "", `Continue from ${cp.short}`, cp.id)}
          >
            <Icon name="play" size={13} /> Continue from here
          </button>
          <button className="primary" disabled={!cp.forkable || !!p.readOnly} title={branchProps.title} onClick={() => compose("custom", null, [], `Explore an alternative direction from ${cp.short}: `, `Fork from ${cp.short}`, cp.id)}>
            <Icon name="branch" size={14} /> Fork Lantern from {cp.short}
          </button>
          {!cp.forkable && <small>Forking and continuing are disabled: {cp.short} did not persist a resumable research state.</small>}
          {cp.forkable && p.readOnly && <small>{roReason}</small>}
        </>
      );
      break;
    }
    case "pending":
      body = (
        <div className="ix-note">
          <span className="pulse" />
          <p>This Lantern is still researching. New events appear on the timeline as they are recorded.</p>
        </div>
      );
      break;
  }

  return (
    <aside className="ix-inspector">
      <div className="ix-insp-head">
        <div>
          <small className={`k-${node.kind}`}>{KIND_LABEL[node.kind]}</small>
          <h2>{node.kind === "query" ? `“${node.label}”` : node.label}</h2>
        </div>
        <button className="icon-button" onClick={p.onClose} title="Close">
          <Icon name="close" size={15} />
        </button>
      </div>
      <div className="ix-insp-scroll">
        {trace.origin === "persisted" && <RecordRef node={node} />}
        {body}
      </div>
      {actions && <div className="ix-insp-actions">{actions}</div>}
    </aside>
  );
}

function Overview(p: Props) {
  const s = summarize(p.trace, p.cursor);
  const decisions = p.trace.nodes.filter((n) => n.kind === "decision" && n.revealAt <= p.cursor);
  return (
    <aside className="ix-inspector">
      <div className="ix-insp-head">
        <div>
          <small>Investigation overview</small>
          <h2>{p.lantern.title}</h2>
        </div>
      </div>
      <div className="ix-insp-scroll">
        <Tag tone={STATUS_META[p.lantern.status].tone}>{STATUS_META[p.lantern.status].label}</Tag>
        <div className="ix-overview-grid">
          <span><b>{s.rounds}</b>rounds</span>
          <span><b>{s.queries}</b>searches</span>
          <span><b>{s.sources}</b>sources</span>
          <span><b>{s.claims}</b>claims</span>
          <span><b>{s.openQuestions}</b>open questions</span>
          <span><b>{s.checkpoints}</b>checkpoints</span>
        </div>
        {p.trace.origin === "persisted" && (
          <>
            <div className="ix-ref" data-testid="record-ref">
              <Icon name="source" size={12} /> Persisted investigation · <code>{p.lantern.id}</code>
            </div>
            {(p.trace.notes?.length ?? 0) > 0 && (
              <InfoBlock label="What this record does not contain">
                <ul data-testid="trace-notes">
                  {p.trace.notes!.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </InfoBlock>
            )}
            {(p.trace.unplacedCheckpoints?.length ?? 0) > 0 && (
              <InfoBlock label="Persisted checkpoints without a timeline event">
                {p.trace.unplacedCheckpoints!.map((c) => (
                  <p key={c.id}>
                    {c.label} <code>{c.id}</code>
                  </p>
                ))}
              </InfoBlock>
            )}
          </>
        )}
        <InfoBlock label="How to read this view">
          <ul>
            <li><b>Blue</b> — what the agent did: rounds, searches, retrieved sources.</li>
            <li><b>Teal / coral</b> — what the evidence shows: claims and contradictions.</li>
            <li><b>Amber dashed</b> — open questions that prompted further searches.</li>
            <li><b>Decision nodes</b> — where the controller chose to continue or stop.</li>
            <li><b>Gold diamonds</b> — persisted checkpoints you can revisit{p.readOnly ? "" : " or fork"}.</li>
          </ul>
        </InfoBlock>
        <InfoBlock label="Recorded decisions">
          {decisions.length ? (
            decisions.map((d) => (
              <button key={d.id} className="ix-link k-decision" onClick={() => p.onSelect(d.id)}>
                <span className="ix-kind-dot k-decision" />
                <span>{d.label}</span>
              </button>
            ))
          ) : (
            <p>No decisions recorded yet.</p>
          )}
        </InfoBlock>
        <InfoBlock label="Checkpoints">
          {p.trace.checkpoints.filter((c) => c.seq <= p.latest).map((c) => (
            <button key={c.id} className="ix-link k-checkpoint" onClick={() => p.onSelect(c.nodeId)}>
              <span className="tiny-gem" />
              <span>
                {c.short} · {c.label}
              </span>
              <em>{c.resumable ? "resumable" : "view only"}</em>
            </button>
          ))}
        </InfoBlock>
      </div>
    </aside>
  );
}

function ForkStubPanel(p: Props & { child: Lantern }) {
  const cp = p.trace.checkpoints.find((c) => c.id === p.child.fromCheckpoint);
  const meta = STATUS_META[p.child.status];
  const claim = p.child.attachedClaim ? p.trace.nodes.find((n) => n.id === p.child.attachedClaim) : null;
  return (
    <aside className="ix-inspector">
      <div className="ix-insp-head">
        <div>
          <small className="k-fork">Child Lantern</small>
          <h2>{p.child.title}</h2>
        </div>
        <button className="icon-button" onClick={p.onClose}>
          <Icon name="close" size={15} />
        </button>
      </div>
      <div className="ix-insp-scroll">
        <Tag tone={meta.tone}>{meta.label}</Tag>
        <InfoBlock label="Branch objective">
          <p>{p.child.objective}</p>
        </InfoBlock>
        <InfoBlock label="Created from">
          {cp && (
            <button className="ix-link k-checkpoint" onClick={() => p.onSelect(cp.nodeId)}>
              <span className="tiny-gem" />
              <span>
                {cp.short} · {cp.label}
              </span>
            </button>
          )}
          {claim && (
            <button className="ix-link k-claim" onClick={() => p.onSelect(claim.id)}>
              <span className="ix-kind-dot k-claim" />
              <span>{claim.label}</span>
            </button>
          )}
        </InfoBlock>
        <div className="difference">
          <Icon name="lantern" />
          <div>
            <b>Parent preserved</b>
            <p>This branch inherited the checkpoint without rewriting the original investigation.</p>
          </div>
        </div>
      </div>
      <div className="ix-insp-actions">
        <button className="primary" onClick={() => p.onOpenLantern(p.child.id)}>
          Enter this Lantern <Icon name="arrow" size={14} />
        </button>
      </div>
    </aside>
  );
}

function Composer(p: Props & { draft: ComposerDraft }) {
  const { trace, draft } = p;
  const claim = draft.claimId ? trace.nodes.find((n) => n.id === draft.claimId) : null;
  const sources = draft.sourceIds.map((id) => trace.nodes.find((n) => n.id === id)).filter(Boolean) as TraceNode[];
  const set = (patch: Partial<ComposerDraft>) => p.onCompose({ ...draft, ...patch });
  const cps = trace.checkpoints.filter((c) => c.seq <= p.latest);
  const reason = (c: Checkpoint) =>
    !c.forkable ? "Not resumable — no research state was persisted" : claim && claim.revealAt > c.seq ? "The selected claim was not recorded yet at this checkpoint" : null;
  const chosen = cps.find((c) => c.id === draft.checkpointId);
  const persisted = trace.origin === "persisted";
  const isContinue = draft.intent === "continue";
  const b = p.branching;
  const [confirming, setConfirming] = useState(false);
  const [authorised, setAuthorised] = useState(false);
  // A challenge must be about something: a selected claim, or an objective that names what to test.
  const ungroundedChallenge = persisted && draft.intent === "challenge" && !claim && !isGroundedObjective(draft.objective);
  const ready = !!chosen && !reason(chosen) && (isContinue || draft.objective.trim().length > 0) && !ungroundedChallenge && !b?.pending;
  const intents: [ComposerDraft["intent"], string][] = [
    ["challenge", "Challenge claim"],
    ["confirm", "Find confirmation"],
    ["uncertainty", "Explore uncertainty"],
    ["custom", "Custom direction"],
    ["continue", "Continue (same question)"],
  ];
  return (
    <aside className="ix-inspector composer">
      <div className="ix-insp-head">
        <div>
          <small className="k-fork">Create child Lantern</small>
          <h2>Branch from this evidence</h2>
        </div>
        <button className="icon-button" onClick={() => p.onCompose(null)} title="Cancel">
          <Icon name="close" size={15} />
        </button>
      </div>
      <div className="ix-insp-scroll">
        <div className="ix-intents">
          {intents.map(([k, label]) => (
            <button key={k} className={draft.intent === k ? "active" : ""} onClick={() => set({ intent: k })}>
              {label}
            </button>
          ))}
        </div>
        {!persisted && (
          <label className="ix-field">
            <span>Lantern name</span>
            <input value={draft.title} onChange={(e) => set({ title: e.target.value })} />
          </label>
        )}
        {isContinue ? (
          <p className="ix-field ix-muted-plain" data-testid="continue-note">
            Continue keeps the original research question. The child resumes from the chosen checkpoint; the parent is not modified.
          </p>
        ) : (
          <label className="ix-field">
            <span>{persisted ? "New branch objective (becomes the child's research question)" : "New branch objective"}</span>
            <textarea data-testid="branch-objective" value={draft.objective} onChange={(e) => set({ objective: e.target.value })} />
          </label>
        )}
        {persisted && (
          <label className="ix-field">
            <span>Search budget for the child</span>
            <input
              type="number"
              min={1}
              max={2}
              value={draft.maxSearches ?? 1}
              onChange={(e) => set({ maxSearches: Math.max(1, Math.min(2, Number(e.target.value) || 1)) })}
            />
          </label>
        )}
        <div className="ix-field">
          <span>Branch from a recorded checkpoint</span>
          <div className="ix-cp-options">
            {cps.map((c) => {
              const why = reason(c);
              return (
                <button key={c.id} disabled={!!why} className={draft.checkpointId === c.id ? "active" : ""} onClick={() => set({ checkpointId: c.id })} title={why ?? undefined}>
                  <span className="tiny-gem" />
                  <span>
                    <b>
                      {c.short} · {c.label}
                    </b>
                    <small>{why ?? (c.resumable ? "Eligible · resumable" : "Eligible")}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
        <div className="ix-field">
          <span>Context that will stay attached</span>
          <div className="ix-context-chain">
            <div>
              <small>Original question</small>
              <p>{p.question ?? QUESTION}</p>
            </div>
            <div>
              <small>Parent investigation</small>
              <p>{p.lantern.title}</p>
            </div>
            {claim && (
              <div className="claim">
                <small>Selected claim</small>
                <p>{claim.label}</p>
                <button onClick={() => set({ claimId: null })}>Remove</button>
              </div>
            )}
            {sources.length > 0 && (
              <div>
                <small>Source references ({sources.length})</small>
                {sources.map((s) => (
                  <p key={s.id} className="ix-src-ref">
                    <span className={`ix-origin inline o-${s.origin}`}>{s.origin?.[0].toUpperCase()}</span> {s.label}
                    <button onClick={() => set({ sourceIds: draft.sourceIds.filter((x) => x !== s.id) })}>×</button>
                  </p>
                ))}
              </div>
            )}
            <div>
              <small>Eligible checkpoint</small>
              <p>{chosen ? `${chosen.short} · ${chosen.label}` : "None selected"}</p>
            </div>
          </div>
        </div>
      </div>
      <div className="ix-insp-actions">
        {b?.error && (
          <p className="ix-compose-error" role="alert" data-testid="branch-error">
            {b.error}
          </p>
        )}
        {ungroundedChallenge && draft.objective.trim().length > 0 && (
          <p className="ix-compose-hint" data-testid="challenge-grounding">
            Select the claim to challenge, or describe what to test (for example the amount, party or date). “{draft.objective.trim()}” is not
            specific enough to search for on its own.
          </p>
        )}
        {persisted && confirming && b?.allowInvestigate && (
          <label className="ix-billable" data-testid="billable-confirm">
            <input type="checkbox" checked={authorised} onChange={(e) => setAuthorised(e.target.checked)} />
            <span>
              I authorise a <b>billable</b> research step: up to {draft.maxSearches ?? 1} search(es); the backend also caps fetched URLs and LLM calls.
            </span>
          </label>
        )}
        <button data-testid="save-lantern" disabled={!ready} onClick={() => p.onCreate(draft, "saved")}>
          <Icon name="lantern" size={14} /> {b?.pending ? "Saving…" : "Save Lantern"}
        </button>
        {persisted ? (
          <button
            className="primary"
            data-testid="fork-investigate"
            disabled={!ready || !b?.allowInvestigate || (confirming && !authorised)}
            title={!b?.allowInvestigate ? b?.investigateDisabledReason : undefined}
            onClick={() => {
              if (!confirming) {
                setConfirming(true);
                return;
              }
              p.onCreate(draft, "investigate");
            }}
          >
            <Icon name="branch" size={14} /> {confirming ? "Run billable research" : "Fork & Investigate"}
          </button>
        ) : (
          <button className="primary" disabled={!ready} onClick={() => p.onCreate(draft, "investigate")}>
            <Icon name="branch" size={14} /> Fork & Investigate
          </button>
        )}
        <small>
          <b>Save Lantern</b> creates the child investigation from the checkpoint without new research. <b>Fork & Investigate</b> also runs a research step, which
          {persisted ? " makes real search, fetch and LLM calls and may cost money" : " may consume credits in the production product (simulated here)"}. The original investigation is never overwritten.
          {persisted && !b?.allowInvestigate && b?.investigateDisabledReason ? <> {b.investigateDisabledReason}</> : null}
        </small>
      </div>
    </aside>
  );
}

function Mini3D({ points, selectedId, onOpen, caption }: { points: EvidencePoint[]; selectedId: string; onOpen?: () => void; caption?: string }) {
  const [yaw] = useState(-0.58);
  const evId = selectedId;
  const all = points.length && points.some((x) => x.id === evId) ? points : [...parentEvidence, ...childEvidence.filter((c) => !parentEvidence.some((x) => x.id === c.id))];
  const sel = all.find((x) => x.id === evId);
  if (!sel) return null;
  const proj = (pt: { relevance: number; novelty: number; graphGap: number }) => {
    const r = projectEvidence(pt, yaw, 0.38, 0.38, { x: 0, y: 0 });
    return { x: (r.x - 390) + 110, y: (r.y - 275) + 78 };
  };
  const corners = [
    [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1],
  ].map(([a, b, c]) => proj({ relevance: a, novelty: b, graphGap: c }));
  const edges = [[0, 1], [0, 2], [0, 4], [1, 3], [1, 5], [2, 3], [2, 6], [3, 7], [4, 5], [4, 6], [5, 7], [6, 7]];
  const tone = sel.action === "consider" ? "green" : sel.action === "investigate" ? "amber" : "gray";
  return (
    <div className="ix-mini3d">
      <div className="ix-mini3d-head">
        <Tag tone={tone}>{sel.action}</Tag>
        <span>
          Intent <b>{sel.relevance.toFixed(2)}</b> · Novelty <b>{sel.novelty.toFixed(2)}</b> · Open questions <b>{sel.graphGap.toFixed(2)}</b>
        </span>
      </div>
      <svg viewBox="0 0 220 156" className="ix-mini3d-plot">
        <g className="policy-cube">
          {edges.map(([a, b], i) => (
            <line key={i} x1={corners[a].x} y1={corners[a].y} x2={corners[b].x} y2={corners[b].y} />
          ))}
        </g>
        {points.map((pt) => {
          const q = proj(pt);
          return <circle key={pt.id} cx={q.x} cy={q.y} r={pt.id === evId ? 5 : 3} className={`ix-m3-pt ${pt.action} ${pt.id === evId ? "sel" : ""}`} />;
        })}
        {!points.some((x) => x.id === evId) && <circle cx={proj(sel).x} cy={proj(sel).y} r={5} className={`ix-m3-pt ${sel.action} sel`} />}
      </svg>
      <p>{caption ?? "Scores show research relevance, not factual correctness."}</p>
      {onOpen && (
        <button className="inspector-link" onClick={onOpen}>
          <Icon name="cube" size={14} /> Show this passage in the 3D evidence space
        </button>
      )}
    </div>
  );
}

function RecordRef({ node }: { node: TraceNode }) {
  if (node.derived)
    return (
      <div className="ix-ref derived">
        <Icon name="layers" size={12} /> Presentation grouping · not a persisted record
      </div>
    );
  if (!node.ref) return null;
  return (
    <div className="ix-ref" data-testid="record-ref">
      <Icon name="source" size={12} /> Persisted record · {node.ref.table} · <code>{node.ref.id}</code>
      {node.ref.eventId && node.ref.eventId !== node.ref.id && (
        <>
          {" "}· event <code>{node.ref.eventId}</code>
        </>
      )}
    </div>
  );
}

function RecordedSnapshot({ cp, policyCount }: { cp: Checkpoint; policyCount: number }) {
  const d = cp.detail!;
  const countKeys = ["documents", "claims", "relationships", "evidence_spans", "unresolved_questions", "candidates"];
  return (
    <InfoBlock label={`Recorded snapshot · step ${d.step}`}>
      <p className="ix-muted-plain">
        Persisted {new Date(d.createdAt).toLocaleString()} · status {d.status ?? "not recorded"}
        {d.stopReason ? ` · stop reason ${d.stopReason}` : ""}
      </p>
      <div className="ix-snap-counts">
        {countKeys
          .filter((k) => k in d.counts)
          .map((k) => (
            <span key={k}>
              <b>{d.counts[k]}</b>
              {k.replace(/_/g, " ")}
            </span>
          ))}
        <span>
          <b>{policyCount}</b>3D points
        </span>
      </div>
      {d.documents.length > 0 && (
        <details className="ix-snap-list">
          <summary>Documents in this snapshot ({d.documents.length})</summary>
          {d.documents.map((doc) => (
            <p key={doc.id}>
              {doc.title || doc.url} <code>{doc.id}</code>
            </p>
          ))}
        </details>
      )}
      {d.relationshipStatus.length > 0 && (
        <details className="ix-snap-list">
          <summary>Relationship status at this point ({d.relationshipStatus.length})</summary>
          {d.relationshipStatus.map((r) => (
            <p key={r.id}>
              {r.predicate} → {r.object} · <b>{r.status}</b>
            </p>
          ))}
        </details>
      )}
      {d.budget && (
        <p className="ix-muted-plain">
          Budget at this point: {d.budget.searches_used ?? "?"}/{d.budget.max_searches ?? "?"} searches · {d.budget.urls_fetched ?? "?"}/{d.budget.max_fetched_urls ?? "?"} URLs · {d.budget.llm_calls_used ?? "?"}/{d.budget.max_llm_calls ?? "?"} LLM calls
        </p>
      )}
    </InfoBlock>
  );
}

function PersistedSource({
  trace,
  doc,
  node,
  cursor,
  passages,
  Link,
  onSelect,
  onOpen3D,
}: {
  trace: Trace;
  doc: TraceNode;
  node: TraceNode;
  cursor: number;
  passages: TraceNode[];
  Link: (p: { id: string; children?: React.ReactNode }) => React.ReactElement;
  onSelect: (id: string) => void;
  onOpen3D: (evId: string | null) => void;
}) {
  const pv = doc.provenance ?? {};
  const group = groupOfNode(trace, doc.id);
  const q = group ? queryOfGroup(trace, group.id) : undefined;
  const docPolicy = policyForDocument(trace, cursor, doc.id);
  const spanId = node.kind === "passage" && node.ref?.table === "evidence_spans" ? node.ref.id : null;
  const spanPolicy = spanId ? policyForSpan(trace, cursor, spanId) : null;
  const passageLevel = !!spanPolicy?.point;
  const { checkpoint, points, point } = passageLevel ? spanPolicy! : docPolicy;
  const shown = node.kind === "passage" ? [node] : passages;
  const claimEdges = trace.edges.filter((e) => shown.some((x) => x.id === e.from) && ["supports", "contradicts", "mentions"].includes(e.kind));
  return (
    <>
      <div className="ix-tag-row">
        <Tag tone={pv.originKind === "derivative" ? "gray" : pv.originKind === "original" ? "teal" : "gray"}>{ORIGIN_LABEL[pv.originKind ?? "unknown"] ?? pv.originKind} source</Tag>
        {point && <Tag tone={point.action === "consider" ? "green" : point.action === "investigate" ? "amber" : "gray"}>{point.action}</Tag>}
      </div>
      {node.kind === "passage" && <h3 className="ix-doc-title">From: {doc.label}</h3>}
      <InfoBlock label="Source provenance (recorded)">
        <dl className="ix-rationale">
          {pv.url && (
            <div>
              <dt>URL</dt>
              <dd>
                <a className="ix-ext" href={pv.url} target="_blank" rel="noreferrer noopener">
                  {pv.url}
                </a>
              </dd>
            </div>
          )}
          <div>
            <dt>Provider</dt>
            <dd>{[pv.provider, ...(pv.providers ?? [])].filter((v, i, a) => v && a.indexOf(v) === i).join(", ") || "not recorded"}</dd>
          </div>
          <div>
            <dt>Origin</dt>
            <dd>
              {pv.originKind ?? "unknown"}
              {pv.originId ? ` · ${pv.originId}` : ""}
              {pv.sharedOriginGroup ? ` · shared group ${pv.sharedOriginGroup}` : ""}
            </dd>
          </div>
          {pv.fetchedAt && (
            <div>
              <dt>Fetched</dt>
              <dd>
                {new Date(pv.fetchedAt).toLocaleString()}
                {pv.fromCache !== undefined ? ` · ${pv.fromCache ? "from cache" : "network"}` : ""}
              </dd>
            </div>
          )}
          {pv.publishDate && (
            <div>
              <dt>Published</dt>
              <dd>{pv.publishDate}</dd>
            </div>
          )}
          {pv.contentHash && (
            <div>
              <dt>Version</dt>
              <dd>
                <code>{pv.versionId}</code> · sha {pv.contentHash.slice(0, 12)}…
              </dd>
            </div>
          )}
          {(pv.attentionScore != null || pv.knowledgeValueScore != null) && (
            <div>
              <dt>Ranking</dt>
              <dd>
                attention {pv.attentionScore?.toFixed(2) ?? "—"} · knowledge value {pv.knowledgeValueScore?.toFixed(2) ?? "—"}
                <br />
                {(pv.knowledgeValueConfirmed?.length ?? 0) > 0 && (
                  <>
                    <small data-testid="kv-confirmed">
                      Knowledge value counted {pv.knowledgeValueConfirmed!.length} thing(s) you confirmed you know ({pv.knowledgeValueConfirmed!.join(", ")}).
                    </small>
                    <br />
                  </>
                )}
                <small>Ranking signals, not factual confidence.</small>
              </dd>
            </div>
          )}
        </dl>
        {pv.notes && <p className="ix-relevance">{pv.notes}</p>}
      </InfoBlock>
      {q && (
        <InfoBlock label="Retrieval query">
          <Link id={q.id}>“{q.label}”</Link>
        </InfoBlock>
      )}
      <InfoBlock label={node.kind === "passage" ? "Extracted passage" : `Extracted passages (${passages.length})`}>
        {shown.length === 0 && <p className="ix-muted">No passage was extracted from this document in the recorded events.</p>}
        {shown.map((x) => (
          <button key={x.id} className={`ix-passage-quote ${x.id === node.id ? "current" : ""}`} onClick={() => onSelect(x.id)}>
            <i className="unscored" />
            <span>
              {x.text ? `“${x.text}”` : <em>No stored span text</em>}
              <small className="ix-span-meta">
                {x.provenance?.spanStart !== undefined ? `chars ${x.provenance.spanStart}–${x.provenance.spanEnd} · ` : ""}
                quote {x.provenance?.quoteValid ? "valid" : "not validated"} · stance {x.stances?.join(", ") || "none recorded"}
              </small>
            </span>
          </button>
        ))}
      </InfoBlock>
      <InfoBlock label="Claims these passages bear on">
        {claimEdges.length === 0 && <p>No recorded claim links.</p>}
        {claimEdges.map((e) => (
          <Link key={e.id} id={e.to}>
            {e.kind === "supports" ? "Supports: " : e.kind === "contradicts" ? "Contradicts: " : "Mentions: "}
            {trace.nodes.find((n) => n.id === e.to)?.label}
          </Link>
        ))}
      </InfoBlock>
      <InfoBlock label="Recorded Focus policy position">
        {point ? (
          <Mini3D
            points={points.map(toEvidencePoint)}
            selectedId={point.evidenceId}
            onOpen={() => onOpen3D(point.evidenceId)}
            caption={`${passageLevel ? "This passage's own recorded point" : node.kind === "passage" ? "Document-level point (no passage coordinate was recorded)" : "Document-level point"} at checkpoint ${checkpoint!.short} (${checkpoint!.id}). Scores show research relevance, not factual correctness.`}
          />
        ) : (
          <div className="ix-no-coords">
            <Icon name="cube" size={15} />
            <p>
              {checkpoint
                ? `Checkpoint ${checkpoint.short} recorded no coordinates for this document.`
                : "No checkpoint with recorded coordinates exists at this point in the timeline."}{" "}
              Coordinates are never estimated.
            </p>
          </div>
        )}
      </InfoBlock>
    </>
  );
}
