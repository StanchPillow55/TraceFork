/*
 * Investigation trace model.
 *
 * Three concepts are kept deliberately separate:
 *  - TraceEvent      what the agent recorded doing (search issued, sources returned, …)
 *  - Checkpoint      a persisted snapshot that may (or may not) be resumable / forkable
 *  - Decision node   a recorded convergence decision (continue / stop …)
 *
 * Everything in this prototype is illustrative. Nothing here is real backend output.
 */

export type PolicyAction = "consider" | "investigate" | "archive";

export type NodeKind =
  | "intent"
  | "inherited"
  | "round"
  | "query"
  | "group"
  | "doc"
  | "passage"
  | "claim"
  | "contradiction"
  | "relation"
  | "question"
  | "decision"
  | "checkpoint"
  | "fork"
  | "pending";

export type EdgeKind =
  | "order" // recorded event order only — no causal claim
  | "retrieved" // returned by the same query
  | "supports"
  | "contradicts"
  | "sharedOrigin"
  | "prompted" // follow-up search prompted by an unresolved question
  | "considered" // evidence referenced by a recorded decision
  | "raised" // evidence that raised an open question
  | "fork"
  | "mentions"; // persisted stance "mentions"/"insufficient": cited, not support

export type ClaimStatus = "supported" | "qualified" | "mixed" | "contested" | "regional" | "unverified" | "insufficient" | "hypothesis";
export type QuestionStatus = "open" | "partial" | "resolved";
export type DecisionOutcome =
  | "continue"
  | "investigate"
  | "stop-sufficient"
  | "stop-budget"
  | "stop-uncertain"
  | "awaiting"
  | "stop-low-value"
  | "stop-no-actions"
  | "blocked";
export type Origin = "primary" | "secondary" | "derivative" | "original" | "unknown";

export type Coords = { intent: number; novelty: number; openq: number };

/** Where a presentation object came from. Persisted objects keep their backend ids. */
export type DataOrigin = "demo" | "persisted";

export interface ProvenanceView {
  url?: string;
  provider?: string | null;
  providers?: string[];
  originKind?: string;
  originId?: string | null;
  sharedOriginGroup?: string | null;
  citedOriginIds?: string[];
  notes?: string;
  fetchedAt?: string;
  publishDate?: string | null;
  contentHash?: string;
  versionId?: string;
  fromCache?: boolean;
  retrievalStatus?: string;
  spanStart?: number;
  spanEnd?: number;
  quoteValid?: boolean;
  attentionScore?: number | null;
  knowledgeValueScore?: number | null;
  /** User assertions (confirmed known) counted when the knowledge value was recorded. */
  knowledgeValueConfirmed?: string[];
  candidateId?: string;
  provenanceId?: string;
}

export interface TraceNode {
  id: string;
  /** Persisted record this node renders (e.g. {table: "documents", id: "doc_…"}). Absent for demo nodes. */
  ref?: { table: string; id: string; eventId?: string };
  /** True for presentation-only groupings (e.g. a "round" header) that are not a recorded record. */
  derived?: boolean;
  provenance?: ProvenanceView;
  relationships?: { id: string; predicate: string; subject: string; object: string; status: string }[];
  stances?: string[];
  kind: NodeKind;
  label: string;
  sub?: string;
  round: number;
  /** event seq at which this node first appears */
  revealAt: number;
  /** disclosure parent: doc → group, passage → doc */
  parent?: string;
  // query
  provider?: string;
  // doc
  publisher?: string;
  origin?: Origin;
  docType?: string;
  // passage
  text?: string;
  policy?: PolicyAction;
  coords?: Coords;
  coordNote?: string;
  evId?: string;
  relevance?: string;
  // claim / question status history
  history?: { at: number; status: ClaimStatus | QuestionStatus; note: string }[];
  independent?: number;
  // decision
  outcome?: DecisionOutcome;
  did?: string;
  rationale?: { label: string; value: string }[];
  // checkpoint
  checkpointId?: string;
  // fork
  lanternId?: string;
  // free description
  body?: string;
}

export interface TraceEdge {
  id: string;
  from: string;
  to: string;
  kind: EdgeKind;
  label?: string;
}

export type EventKind =
  | "intent"
  | "inherit"
  | "plan"
  | "search"
  | "retrieve"
  | "extract"
  | "evaluate"
  | "relate"
  | "question"
  | "decide"
  | "checkpoint"
  | "pending"
  | "rank"
  | "generic";

export interface TraceEvent {
  seq: number;
  /** Persisted event id / sequence / stage (persisted mode only). */
  id?: string;
  backendSequence?: number;
  stage?: string;
  createdAt?: string;
  checkpointId?: string | null;
  kind: EventKind;
  round: number;
  label: string;
  detail: string;
  /** node to select / focus when the user lands on this event */
  focus?: string;
}

export interface Checkpoint {
  id: string;
  short: string;
  label: string;
  seq: number;
  nodeId: string;
  persisted: true;
  resumable: boolean;
  forkable: boolean;
  notResumableReason?: string;
  status: string;
  summary: string;
  frontier: string[] | null; // null = not recorded at this checkpoint
  recordsClaims: boolean;
  recordsCoords: boolean;
  why?: string;
  /** Recorded snapshot details (persisted mode). Only fields present in state_json are filled. */
  detail?: CheckpointDetail;
}

export interface CheckpointDetail {
  step: number;
  createdAt: string;
  stopReason?: string | null;
  status?: string;
  decisionSource?: string | null;
  decisionRationale?: string;
  counts: Record<string, number>;
  documents: { id: string; title: string; url: string }[];
  relationshipStatus: { id: string; predicate: string; object: string; status: string }[];
  questions: string[];
  budget?: Record<string, number>;
  policyPointCount: number;
  hasEvent: boolean;
}

export interface Trace {
  lanternId: string;
  origin?: DataOrigin;
  question?: string;
  /** Persisted traces: reasons the backend gave for an incomplete trace, plus adapter notes. */
  incompleteReason?: string | null;
  notes?: string[];
  /** Persisted checkpoints that have no recorded checkpoint event, so they cannot be placed on the timeline. */
  unplacedCheckpoints?: Checkpoint[];
  /** Persisted child investigations: the branch record that created them. */
  branchId?: string | null;
  branchObjective?: string | null;
  attachments?: { claims: { id: string; text: string; status?: string | null }[]; sources: { id: string; title: string; url: string }[] };
  /** Recorded Focus policy coordinates per checkpoint id (never estimated). */
  policyByCheckpoint?: Record<string, PolicyPointView[]>;
  /** Passage-level coordinates (one per stored evidence span), when the backend recorded them. */
  passagePolicyByCheckpoint?: Record<string, PolicyPointView[]>;
  nodes: TraceNode[];
  edges: TraceEdge[];
  events: TraceEvent[];
  checkpoints: Checkpoint[];
  rounds: { n: number; title: string; phase: string }[];
}

export interface PolicyPointView {
  evidenceId: string;
  documentId: string | null;
  title: string;
  url: string;
  passage: string;
  intent: number;
  novelty: number;
  unresolved: number;
  action: PolicyAction;
  reason: string;
  /** Set on passage-level points (checkpoint policy_space_passages, 1.1.0). */
  spanId?: string | null;
}

export type LanternStatus =
  | "created"
  | "running"
  | "blocked"
  | "stopped-sufficient"
  | "stopped-budget"
  | "stopped-low-value"
  | "stopped-no-actions"
  | "stopped"
  | "recorded-branch"
  | "stopped-uncertain"
  | "researching"
  | "completed"
  | "awaiting"
  | "not-started";

export interface Lantern {
  id: string;
  title: string;
  objective: string;
  status: LanternStatus;
  parentId?: string;
  fromCheckpoint?: string; // checkpoint id in parent trace
  attachedClaim?: string; // node id in parent trace
  attachedSources?: string[];
  userCreated?: boolean;
  createdMode?: "saved" | "investigate";
  slot?: number;
}

export type Selection = { id: string } | null;

export interface ViewState {
  cursor: number; // event seq currently shown
  snapshot: string | null; // loaded checkpoint id
  selected: string | null;
  expanded: string[]; // expanded group / doc ids
  collapsedRounds: number[];
  zoom: number;
  pan: { x: number; y: number };
  fitted: boolean;
  granularity: "event" | "round";
  composer: ComposerDraft | null;
  focusNonce: number;
  focusIds: string[];
}

export interface ComposerDraft {
  intent: "challenge" | "confirm" | "uncertainty" | "custom" | "continue";
  /** Persisted mode: branch budget sent as constraints.max_searches. */
  maxSearches?: number;
  claimId: string | null;
  sourceIds: string[];
  checkpointId: string | null;
  title: string;
  objective: string;
}
