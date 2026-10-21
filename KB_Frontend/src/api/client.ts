import type {
  AgeDecayState,
  ClassDetail,
  ClaudeAuthStatus,
  Concept,
  ConsolidationStatus,
  CreateBody,
  CurationCommandInfo,
  CurationOp,
  CurationParse,
  DbTableMeta,
  DbTablePage,
  DocKindRow,
  EntityGraphData,
  EvalsPayload,
  ExcludedMemory,
  GraphData,
  ImpactReport,
  IngestEvent,
  IngestStatus,
  LinkedMemory,
  McpToolState,
  Me,
  MergeSuggestion,
  NodeDetail,
  NodeMeta,
  PolicyView,
  ProvenanceEntity,
  RetrievalHeatmapData,
  RetrievalHeatmapParams,
  SkillDetailData,
  SkillFileContent,
  SkillLibraryUsageData,
  SkillPage,
  SkillSummary,
  SkillUsageData,
  Trace,
  TraceDetail,
  TraceJudgment,
  TraceReplay,
  TraceSummary,
  Turn,
  TurnDetail,
  UpdatePatch,
  UsageDays,
} from "../types";
import { isMe, UNKNOWN_ME } from "../lib/identity";

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return (await res.json()) as T;
}

const jsonHeaders = { "Content-Type": "application/json" };

export function fetchTree(): Promise<NodeMeta[]> {
  return fetch("/api/tree").then((r) => asJson<NodeMeta[]>(r));
}

export function fetchSubtree(path: string): Promise<GraphData> {
  return fetch(`/api/subtree?path=${encodeURIComponent(path)}`).then((r) =>
    asJson<GraphData>(r),
  );
}

// Warm start for the entity and concept graphs. Both are fetched on entry into their mode,
// which made the first switch wait on a 0.5 MB round trip; App primes them in the
// background once the tree has landed. A primed response is handed to the next fetch ONCE
// and only while fresh — after that (or if priming failed) the fetch goes to the network,
// so "fresh on each entry" still holds beyond the first minute.
const PRIME_TTL_MS = 60_000;
const primed = new Map<string, { at: number; promise: Promise<unknown> }>();

function prime(url: string): void {
  const promise = fetch(url).then((r) => asJson<unknown>(r));
  promise.catch(() => {}); // a failed prime is only a missed head start, never an error
  primed.set(url, { at: Date.now(), promise });
}

function fetchPrimed<T>(url: string): Promise<T> {
  const load = () => fetch(url).then((r) => asJson<T>(r));
  const hit = primed.get(url);
  primed.delete(url);
  if (!hit || Date.now() - hit.at >= PRIME_TTL_MS) return load();
  return (hit.promise as Promise<T>).catch(load);
}

/** The isolated entity graph: the `entities` hub, its classes, and their instances. */
export function fetchEntities(): Promise<EntityGraphData> {
  return fetchPrimed<EntityGraphData>("/api/entities");
}

export function primeEntities(): void {
  prime("/api/entities");
}

/** The derived concept layer: every active concept with its scored members. */
export function fetchConcepts(): Promise<{ concepts: Concept[] }> {
  return fetchPrimed<{ concepts: Concept[] }>("/api/concepts");
}

export function primeConcepts(): void {
  prime("/api/concepts");
}

/** Is the concept layer rebuilding, and what did the last rebuild do? */
export function fetchConsolidationStatus(): Promise<ConsolidationStatus> {
  return fetch("/api/concepts/run-status").then((r) => asJson<ConsolidationStatus>(r));
}

/** Ask the consolidation daemon to rebuild the concept layer now. Idempotent — the server
 *  returns current state rather than queueing a second pass. */
export function requestConsolidation(): Promise<ConsolidationStatus> {
  return fetch("/api/concepts/run", { method: "POST", headers: jsonHeaders }).then((r) =>
    asJson<ConsolidationStatus>(r),
  );
}

/** Rename/re-kind/re-describe a concept. Locks the label — future consolidation runs won't
 *  overwrite it. */
export function updateConceptLabel(
  id: string,
  patch: { name?: string; kind?: string; description?: string },
): Promise<Concept> {
  return fetch(`/api/concepts/${id}/label`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(patch),
  }).then((r) => asJson<Concept>(r));
}

/** Hide a concept from the default graph/list without deleting it — reversible via
 *  `restoreConcept`. */
export function dismissConcept(id: string): Promise<Concept> {
  return fetch(`/api/concepts/${id}/dismiss`, { method: "POST" }).then((r) =>
    asJson<Concept>(r),
  );
}

/** Undo a `dismissConcept`. */
export function restoreConcept(id: string): Promise<Concept> {
  return fetch(`/api/concepts/${id}/restore`, { method: "POST" }).then((r) =>
    asJson<Concept>(r),
  );
}

/** Pull one member node out of a concept (an override the next consolidation run respects). */
export function removeConceptMember(
  conceptId: string,
  nodeId: string,
): Promise<{ concept_id: string; node_id: string; action: string }> {
  return fetch(`/api/concepts/${conceptId}/members/${nodeId}/remove`, {
    method: "POST",
  }).then((r) => asJson<{ concept_id: string; node_id: string; action: string }>(r));
}

/** NL curation: compile an instruction into ops + a human preview. Read-only. */
export function parseCuration(text: string): Promise<CurationParse> {
  return fetch("/api/concepts/curate/parse", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ text }),
  }).then((r) => asJson<CurationParse>(r));
}

/** Apply previously previewed concept-curation ops (from `parseCuration` or a UI action). */
export function applyCuration(
  ops: CurationOp[],
): Promise<{ applied: CurationOp[]; errors: string[] }> {
  return fetch("/api/concepts/curate/apply", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ ops }),
  }).then((r) => asJson<{ applied: CurationOp[]; errors: string[] }>(r));
}

export function fetchNode(id: string): Promise<NodeDetail> {
  return fetch(`/api/node/${id}`).then((r) => asJson<NodeDetail>(r));
}

/**
 * The "shut off" memories for the settings Memories view — everything excluded from the
 * going-forward entity system. `kind` narrows to noise (no meaning, influence retracted) or
 * dead (retired/frozen, influence kept); omit it for both.
 */
export function fetchExcludedMemories(
  kind?: "noise" | "dead",
): Promise<{ memories: ExcludedMemory[] }> {
  const q = kind ? `?kind=${kind}` : "";
  return fetch(`/api/memories/excluded${q}`).then((r) =>
    asJson<{ memories: ExcludedMemory[] }>(r),
  );
}

/** Bring a retired (dead) memory back: active and crawlable again. */
export function reviveMemory(id: string): Promise<NodeDetail> {
  return fetch(`/api/node/${id}/revive`, { method: "POST" }).then((r) =>
    asJson<NodeDetail>(r),
  );
}

/**
 * Manually trigger a re-crawl of one memory: asks the re-analysis daemon to re-run its LLM
 * passes over this node now, instead of waiting for its timer. Fire-and-forget — the work
 * happens in the background (reflected in the live change feed); resolves once the request
 * is accepted.
 */
export function reanalyzeNode(
  id: string,
): Promise<{ requested: boolean; node_id: string }> {
  return fetch(`/api/node/${id}/reanalyze`, { method: "POST" }).then((r) =>
    asJson<{ requested: boolean; node_id: string }>(r),
  );
}

/** Exclude a node (folder or document) from entity analysis; `retract` also pulls past entities. */
export function setAnalysisExcluded(
  id: string,
  opts: { retract: boolean },
): Promise<NodeDetail> {
  return fetch(`/api/node/${id}/analysis-exclude`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ retract: opts.retract }),
  }).then((r) => asJson<NodeDetail>(r));
}

/** Re-include a node in entity analysis (clear its own exclusion flag). */
export function clearAnalysisExcluded(id: string): Promise<NodeDetail> {
  return fetch(`/api/node/${id}/analysis-include`, { method: "POST" }).then((r) =>
    asJson<NodeDetail>(r),
  );
}

/** Set a node's significance value. On a folder it cascades to every descendant. */
export function setSignificance(id: string, value: number): Promise<NodeDetail> {
  return fetch(`/api/node/${id}/significance`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ value }),
  }).then((r) => asJson<NodeDetail>(r));
}

/** One importance-ordered page of the memories an entity was found in (its in-links),
 *  plus the total. Empty for non-entity nodes. */
export function fetchNodeMemories(
  id: string,
  opts: { limit?: number; offset?: number } = {},
): Promise<{ memories: LinkedMemory[]; total: number }> {
  const params = new URLSearchParams();
  if (opts.limit != null) params.set("limit", String(opts.limit));
  if (opts.offset != null) params.set("offset", String(opts.offset));
  const qs = params.toString();
  return fetch(`/api/node/${id}/memories${qs ? `?${qs}` : ""}`).then((r) =>
    asJson<{ memories: LinkedMemory[]; total: number }>(r),
  );
}

/** Every active entity with the memories it came from (its entity_links in-links), grouped
 *  by entity. Drives the Provenance view. */
export function fetchProvenance(): Promise<{ entities: ProvenanceEntity[] }> {
  return fetch("/api/provenance").then((r) =>
    asJson<{ entities: ProvenanceEntity[] }>(r),
  );
}

export function fetchIngestLog(
  limit = 50,
  source?: string | null,
  event?: string | null,
): Promise<{ events: IngestEvent[] }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (source) params.set("source", source);
  if (event) params.set("event", event);
  return fetch(`/api/ingest/log?${params}`).then((r) =>
    asJson<{ events: IngestEvent[] }>(r),
  );
}

export function fetchIngestStatus(): Promise<IngestStatus> {
  return fetch("/api/ingest/status").then((r) => asJson<IngestStatus>(r));
}

const ME_TIMEOUT_MS = 10_000;
const ME_RETRY_DELAYS_MS = [1_000, 3_000, 9_000];

/** One /api/me attempt: an identity, null (old backend), UNKNOWN_ME (a definitive but
 *  unusable answer), or "retry" (transient: 5xx, network error, timeout). */
async function fetchMeOnce(): Promise<Me | null | "retry"> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ME_TIMEOUT_MS);
  try {
    const r = await fetch("/api/me", { signal: ctrl.signal });
    if (r.status === 404) return null; // pre-tenancy backend: no such route
    // 401/403 are answers, not glitches (an expired session, an unmapped email): retrying
    // cannot change them.
    if (r.status === 401 || r.status === 403) return UNKNOWN_ME;
    if (!r.ok) return "retry";
    const text = await r.text();
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      // A 200 non-JSON body is the SPA fallback of a backend without /api/me — unless we
      // were redirected to get it (an Access login page), which is not an identity.
      return r.redirected ? UNKNOWN_ME : null;
    }
    return isMe(body) ? body : UNKNOWN_ME;
  } catch {
    return "retry"; // network error, CORS-blocked auth redirect, or the timeout's abort
  } finally {
    clearTimeout(timer);
  }
}

/** Who is signed in. Fails CLOSED: null (read by isOwner as the owner) only when the
 *  backend has no /api/me at all (404, or a 200 non-JSON SPA fallback). Transient failures
 *  retry after 1 s, 3 s and 9 s, each attempt capped at 10 s; anything still failing —
 *  and any 401/403/malformed answer — settles as UNKNOWN_ME, a non-owner. Never rejects. */
export async function fetchMe(): Promise<Me | null> {
  for (let attempt = 0; ; attempt++) {
    const result = await fetchMeOnce();
    if (result !== "retry") return result;
    if (attempt >= ME_RETRY_DELAYS_MS.length) return UNKNOWN_ME;
    await new Promise((resolve) => setTimeout(resolve, ME_RETRY_DELAYS_MS[attempt]));
  }
}

/** Memory notes currently being enriched — diffed by the UI to surface "began analysis" toasts. */
export function fetchProcessing(): Promise<{ nodes: NodeMeta[] }> {
  return fetch("/api/ingest/processing").then((r) => asJson<{ nodes: NodeMeta[] }>(r));
}

/**
 * Re-queue failed enrichments as pending so the worker re-attempts them (e.g.
 * after the LLM backend recovers). `source` scopes broadly; `nodeIds` scopes
 * precisely (e.g. only the retryable failures). With neither, every failure is
 * re-queued. Resolves with how many were re-queued.
 */
export function retryFailedIngest(
  opts: { source?: string | null; nodeIds?: string[] | null } = {},
): Promise<{ requeued: number }> {
  const body: { source?: string; node_ids?: string[] } = {};
  if (opts.source) body.source = opts.source;
  if (opts.nodeIds && opts.nodeIds.length > 0) body.node_ids = opts.nodeIds;
  return fetch("/api/ingest/retry-failed", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(body),
  }).then((r) => asJson<{ requeued: number }>(r));
}

/**
 * Dismiss a stuck ingest record from the banner (writes an ingest_resolved event). A
 * still-failing pull record is re-opened by its source's next tick; a push failure, which
 * never retries, leaves the banner for good. Not optimistic — the caller reloads after.
 */
export function dismissStuckRecord(
  source: string,
  externalId: string,
): Promise<{ ok: boolean }> {
  return fetch("/api/ingest/stuck/dismiss", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ source, external_id: externalId }),
  }).then((r) => asJson<{ ok: boolean }>(r));
}

export function updateNode(id: string, patch: UpdatePatch): Promise<NodeDetail> {
  return fetch(`/api/node/${id}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify(patch),
  }).then((r) => asJson<NodeDetail>(r));
}

export function moveNode(id: string, newParentPath: string): Promise<NodeDetail> {
  return fetch(`/api/node/${id}/move`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ new_parent_path: newParentPath }),
  }).then((r) => asJson<NodeDetail>(r));
}

export function deleteNode(id: string, recursive = false): Promise<{ deleted: number }> {
  return fetch(`/api/node/${id}?recursive=${recursive}`, { method: "DELETE" }).then(
    (r) => asJson<{ deleted: number }>(r),
  );
}

export function createNode(body: CreateBody): Promise<NodeDetail> {
  return fetch("/api/node", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(body),
  }).then((r) => asJson<NodeDetail>(r));
}

export function fetchClass(name: string): Promise<ClassDetail> {
  return fetch(`/api/classes/${encodeURIComponent(name)}`).then((r) => asJson<ClassDetail>(r));
}

export function updateClassDescription(name: string, description: string): Promise<ClassDetail> {
  return fetch(`/api/classes/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify({ description }),
  }).then((r) => asJson<ClassDetail>(r));
}

export function blockClass(
  name: string,
): Promise<{ name: string; status: string; requeued: number }> {
  return fetch(`/api/classes/${encodeURIComponent(name)}/block`, { method: "POST" }).then((r) =>
    asJson<{ name: string; status: string; requeued: number }>(r),
  );
}

export function fetchClasses(): Promise<{ classes: ClassDetail[] }> {
  return fetch("/api/classes").then((r) => asJson<{ classes: ClassDetail[] }>(r));
}

export function fetchCurationCommands(): Promise<{ commands: CurationCommandInfo[] }> {
  return fetch("/api/curation/commands").then((r) =>
    asJson<{ commands: CurationCommandInfo[] }>(r),
  );
}

export function previewCuration(
  action: string,
  args: Record<string, string>,
): Promise<ImpactReport> {
  return fetch(`/api/curation/${encodeURIComponent(action)}/preview`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(args),
  }).then((r) => asJson<ImpactReport>(r));
}

/** Apply a class-curation action (create/merge/rename/prune/pin/…) previewed via
 *  `previewCuration`. Named distinctly from the concept-curation `applyCuration` above —
 *  same verb, different subsystem (entity classes vs. derived concepts), different endpoint. */
export function applyClassCuration(
  action: string,
  args: Record<string, string>,
): Promise<ImpactReport> {
  return fetch(`/api/curation/${encodeURIComponent(action)}`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(args),
  }).then((r) => asJson<ImpactReport>(r));
}

// ---- raw Postgres table browser (the password-gated Database page) ----------
// The shared password gates the /api/db/* endpoints server-side; we hold it only in
// sessionStorage (cleared when the tab session ends — never localStorage) and send it as
// the X-DB-Password header on every db request.
const DB_PW_KEY = "kb.db.password";

export function storeDbPassword(password: string): void {
  // sessionStorage throws in private-mode / quota-exceeded; a failed persist just means
  // the gate re-prompts next load, so swallow it rather than crash the unlock flow.
  try {
    sessionStorage.setItem(DB_PW_KEY, password);
  } catch {
    /* ignore */
  }
}
export function clearDbPassword(): void {
  sessionStorage.removeItem(DB_PW_KEY);
}
export function hasDbPassword(): boolean {
  return !!sessionStorage.getItem(DB_PW_KEY);
}
function dbHeaders(): Record<string, string> {
  return { "X-DB-Password": sessionStorage.getItem(DB_PW_KEY) ?? "" };
}

/** Verify a password against the gate without revealing data. Rejects (401) if wrong. */
export function dbAuth(password: string): Promise<{ ok: true }> {
  return fetch("/api/db/auth", {
    method: "POST",
    headers: { "X-DB-Password": password },
  }).then((r) => asJson<{ ok: true }>(r));
}

export function fetchDbTables(): Promise<{ tables: DbTableMeta[] }> {
  return fetch("/api/db/tables", { headers: dbHeaders() }).then((r) =>
    asJson<{ tables: DbTableMeta[] }>(r),
  );
}

export function fetchDbTable(
  schema: string,
  name: string,
  opts: { limit?: number; offset?: number } = {},
): Promise<DbTablePage> {
  const params = new URLSearchParams();
  if (opts.limit != null) params.set("limit", String(opts.limit));
  if (opts.offset != null) params.set("offset", String(opts.offset));
  const q = params.toString();
  const path = `/api/db/table/${encodeURIComponent(schema)}/${encodeURIComponent(name)}`;
  return fetch(q ? `${path}?${q}` : path, { headers: dbHeaders() }).then((r) =>
    asJson<DbTablePage>(r),
  );
}

export function fetchMergeSuggestions(): Promise<MergeSuggestion[]> {
  return fetch("/api/merge-suggestions").then((r) => asJson<MergeSuggestion[]>(r));
}

export function approveMerge(
  id: number,
  targetId?: string,
): Promise<{ merged: boolean; target_id: string | null }> {
  return fetch(`/api/merge-suggestions/${id}/approve`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(targetId !== undefined ? { target_id: targetId } : {}),
  }).then((r) => asJson<{ merged: boolean; target_id: string | null }>(r));
}

export function rejectMerge(
  id: number,
  reason?: string,
): Promise<{ rejected: boolean }> {
  return fetch(`/api/merge-suggestions/${id}/reject`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify(reason !== undefined ? { reason } : {}),
  }).then((r) => asJson<{ rejected: boolean }>(r));
}

export function fetchDocKinds(all = false): Promise<{ kinds: DocKindRow[] }> {
  return fetch(`/api/doc-kinds${all ? "?all=true" : ""}`).then((r) =>
    asJson<{ kinds: DocKindRow[] }>(r),
  );
}

export function fetchDocKindMemories(
  kind: string,
): Promise<{ memories: NodeMeta[] }> {
  return fetch(`/api/doc-kinds/${encodeURIComponent(kind)}/memories`).then((r) =>
    asJson<{ memories: NodeMeta[] }>(r),
  );
}

export interface TraceFilters {
  limit?: number;
  tool?: string;
  confidence?: string;
  judged?: boolean;
  includeEvals?: boolean;
}

/** Recent MCP retrieval traces, newest first (scalar columns; detail is separate). */
export function fetchTraces(f: TraceFilters = {}): Promise<{ traces: Trace[] }> {
  const q = new URLSearchParams();
  if (f.limit !== undefined) q.set("limit", String(f.limit));
  if (f.tool) q.set("tool", f.tool);
  if (f.confidence) q.set("confidence", f.confidence);
  if (f.judged !== undefined) q.set("judged", String(f.judged));
  if (f.includeEvals) q.set("include_evals", "true");
  return fetch(`/api/retrieval/traces?${q}`).then((r) =>
    asJson<{ traces: Trace[] }>(r),
  );
}

/** Aggregates over the trailing `days` of traces (eval traffic excluded by default). */
export function fetchTraceSummary(days = 7, includeEvals = false): Promise<TraceSummary> {
  return fetch(
    `/api/retrieval/traces/summary?days=${days}&include_evals=${includeEvals}`,
  ).then((r) => asJson<TraceSummary>(r));
}

/** Per-memory surfaced/opened counts per bucket (feeds the graph's retrieval coloring). */
export function fetchRetrievalHeatmap(p: RetrievalHeatmapParams): Promise<RetrievalHeatmapData> {
  const q = new URLSearchParams({ days: String(p.days), bucket: p.bucket, tz: p.tz });
  if (p.path) q.set("path", p.path);
  if (p.limit !== undefined) q.set("limit", String(p.limit));
  return fetch(`/api/retrieval/heatmap?${q}`).then((r) => asJson<RetrievalHeatmapData>(r));
}

/** Eval history parsed from the committed result files. */
export function fetchRetrievalEvals(): Promise<EvalsPayload> {
  return fetch("/api/retrieval/evals").then((r) => asJson<EvalsPayload>(r));
}

/** Full trace payload: args, resolved results, crawl trace, judgments. */
export function fetchTraceDetail(id: number): Promise<TraceDetail> {
  return fetch(`/api/retrieval/traces/${id}`).then((r) => asJson<TraceDetail>(r));
}

/** A search trace's per-stage replay, labelled live; 404 for non-search/errored traces. */
export function fetchTraceReplay(id: number): Promise<TraceReplay> {
  return fetch(`/api/retrieval/traces/${id}/replay`).then((r) => asJson<TraceReplay>(r));
}

/** Adjudicate a trace (append-only; judged_by is fixed server-side). */
export function postTraceJudgment(
  id: number,
  verdict: "good" | "partial" | "bad",
  notes?: string,
): Promise<TraceJudgment> {
  return fetch(`/api/retrieval/traces/${id}/judgment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verdict, notes: notes || null }),
  }).then((r) => asJson<TraceJudgment>(r));
}

/** Recent requests (grouped calls), newest first. Same filters as fetchTraces: tool and
 *  confidence match a request containing such a call; judged applies to request verdicts. */
export function fetchTurns(f: TraceFilters = {}): Promise<{ turns: Turn[] }> {
  const q = new URLSearchParams();
  if (f.limit !== undefined) q.set("limit", String(f.limit));
  if (f.tool) q.set("tool", f.tool);
  if (f.confidence) q.set("confidence", f.confidence);
  if (f.judged !== undefined) q.set("judged", String(f.judged));
  if (f.includeEvals) q.set("include_evals", "true");
  return fetch(`/api/retrieval/turns?${q}`).then((r) => asJson<{ turns: Turn[] }>(r));
}

/** One request with its calls (oldest first) and judgments; 404 for a non-opener id. */
export function fetchTurnDetail(id: number): Promise<TurnDetail> {
  return fetch(`/api/retrieval/turns/${id}`).then((r) => asJson<TurnDetail>(r));
}

/** Adjudicate a whole request (append-only; judged_by is fixed server-side). */
export function postTurnJudgment(
  id: number,
  verdict: "good" | "partial" | "bad",
  notes?: string,
): Promise<TraceJudgment> {
  return fetch(`/api/retrieval/turns/${id}/judgment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verdict, notes: notes || null }),
  }).then((r) => asJson<TraceJudgment>(r));
}

export function fetchMcpTools(): Promise<{ tools: McpToolState[] }> {
  return fetch("/api/mcp/tools").then((r) => asJson<{ tools: McpToolState[] }>(r));
}

/** Flip one tool. Gated server-side by the same shared password as the Database page. */
export function setMcpToolEnabled(
  tool: string,
  enabled: boolean,
): Promise<McpToolState> {
  return fetch(`/api/mcp/tools/${encodeURIComponent(tool)}`, {
    method: "POST",
    headers: { ...jsonHeaders, ...dbHeaders() },
    body: JSON.stringify({ enabled }),
  }).then((r) => asJson<McpToolState>(r));
}

export function fetchClaudeAuth(): Promise<ClaudeAuthStatus> {
  return fetch("/api/auth/claude").then((r) => asJson<ClaudeAuthStatus>(r));
}

/** Opens a Terminal on the host running `claude auth login`. Owner-only, opt-in
 *  (KB_DASHBOARD_ALLOW_RELOGIN; 404 when off) and password-gated server-side. */
export function postClaudeRelogin(): Promise<{ launched: true }> {
  return fetch("/api/auth/claude/relogin", {
    method: "POST",
    headers: dbHeaders(),
  }).then((r) => asJson<{ launched: true }>(r));
}

export function fetchAgeDecay(): Promise<AgeDecayState> {
  return fetch("/api/aging").then((r) => asJson<AgeDecayState>(r));
}

export function setAgeDecay(enabled: boolean): Promise<AgeDecayState> {
  return fetch("/api/aging", {
    method: "POST", headers: jsonHeaders, body: JSON.stringify({ enabled }),
  }).then((r) => asJson<AgeDecayState>(r));
}

export function fetchPolicy(): Promise<PolicyView> {
  return fetch("/api/policy").then((r) => asJson<PolicyView>(r));
}

// ---- Skills page (read-only; see docs/setup/recipes/skill-registry.md)

export function fetchSkills(
  p: { q?: string; cursor?: string | null; archived?: boolean } = {},
): Promise<SkillPage> {
  const q = new URLSearchParams();
  if (p.q) q.set("q", p.q);
  if (p.cursor) q.set("cursor", p.cursor);
  if (p.archived) q.set("archived", "true");
  const qs = q.toString();
  return fetch(`/api/skills${qs ? `?${qs}` : ""}`).then((r) => asJson<SkillPage>(r));
}

/** No revision = current. */
export function fetchSkill(name: string, revision?: number | null): Promise<SkillDetailData> {
  const qs = revision ? `?revision=${revision}` : "";
  return fetch(`/api/skills/${encodeURIComponent(name)}${qs}`).then((r) =>
    asJson<SkillDetailData>(r),
  );
}

/** The agent library. Normalised to SkillPage so the list renders either kind. */
export function fetchAgents(
  p: { q?: string; cursor?: string | null; archived?: boolean } = {},
): Promise<SkillPage> {
  const q = new URLSearchParams();
  if (p.q) q.set("q", p.q);
  if (p.cursor) q.set("cursor", p.cursor);
  if (p.archived) q.set("archived", "true");
  const qs = q.toString();
  return fetch(`/api/agents${qs ? `?${qs}` : ""}`)
    .then((r) => asJson<{ agents: SkillSummary[]; next_cursor: string | null; query: string | null }>(r))
    .then((b) => ({ skills: b.agents, next_cursor: b.next_cursor, query: b.query }));
}

/** No revision = the newest (the page shows what waits for review too). */
export function fetchAgent(name: string, revision?: number | null): Promise<SkillDetailData> {
  const qs = revision ? `?revision=${revision}` : "";
  return fetch(`/api/agents/${encodeURIComponent(name)}${qs}`).then((r) =>
    asJson<SkillDetailData>(r),
  );
}

/** `revision` is the one being viewed -- never "current" -- so a view never mixes
 * files across an update. Each path segment is encoded; the slashes stay. */
export function fetchSkillFile(
  name: string,
  path: string,
  revision: number,
): Promise<SkillFileContent> {
  const p = path.split("/").map(encodeURIComponent).join("/");
  return fetch(`/api/skills/${encodeURIComponent(name)}/files/${p}?revision=${revision}`).then(
    (r) => asJson<SkillFileContent>(r),
  );
}

export function fetchSkillUsage(name: string, days: UsageDays, tz: string): Promise<SkillUsageData> {
  const q = new URLSearchParams({ days: String(days), tz });
  return fetch(`/api/skills/${encodeURIComponent(name)}/usage?${q}`).then((r) =>
    asJson<SkillUsageData>(r),
  );
}

/** /api/skill-usage, not /api/skills/usage: `usage` is a valid skill name. */
export function fetchSkillLibraryUsage(days: UsageDays, tz: string): Promise<SkillLibraryUsageData> {
  const q = new URLSearchParams({ days: String(days), tz });
  return fetch(`/api/skill-usage?${q}`).then((r) => asJson<SkillLibraryUsageData>(r));
}
