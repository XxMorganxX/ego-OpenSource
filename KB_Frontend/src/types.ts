export type NodeMeta = {
  id: string;
  path: string;
  parent_id: string | null;
  type: string;
  slug: string;
  title: string | null;
  tags: string[];
  /** Emergent document-kind facet ("meeting notes", …); null/absent until classified. */
  doc_kind?: string | null;
  /** Stored significance score from the database (importance amplifier). */
  importance: number;
  /** Last write to the node (ISO datetime). Touched by pipeline re-upserts (enrichment,
   *  re-analysis), not only user edits — recency coloring prefers content_changed_at. */
  updated_at?: string;
  /** Last REAL content change (ISO datetime), from ingest-log hash transitions; folders
   *  roll up their subtree's newest. Null when the log never saw the node. Preferred
   *  timestamp for the recency color scheme. */
  content_changed_at?: string | null;
  /** Self-flagged out of entity analysis (the exact node the user toggled). */
  analysis_excluded?: boolean;
  /** Effective exclusion: self OR an ancestor folder is flagged. Travels with tree/graph
   *  metadata so the file explorer icon and graph node dim a whole excluded subtree. */
  analysis_excluded_effective?: boolean;
};

/** A memory's entity-system participation state. */
export type Lifecycle = "active" | "noise" | "dead";

/** One page-row of the memories an entity was found in (its entity_links in-links). */
export type LinkedMemory = NodeMeta & {
  link_type: "mentions" | "belongs_to";
  weight: number | null;
  /** Real-world event date (event_at ?? source_created_at); null when unknown. */
  event_at: string | null;
  /** Last write to the node — fallback display date. */
  updated_at: string;
};

/** One significance factor's contribution to a node's importance. */
export type SignificanceFactor = {
  name: string;        // "manual" | "access" | "inherited" | "project_dormancy"
  value: number;       // the factor's raw input
  coefficient: number; // its weight in the aggregate
  contribution: number; // coefficient * value
};

export type NodeDetail = NodeMeta & {
  content: string;
  summary: string | null;
  uri: string | null;
  source: string;
  /** The source's own identifier — for file sources (e.g. obsidian) the original file path. */
  external_id: string;
  lifecycle?: Lifecycle;
  entity_skip_reason?: string | null;
  dead_at?: string | null;
  dead_reason?: string | null;
  analysis_excluded?: boolean;
  analysis_excluded_effective?: boolean;
  /** The ancestor folder path causing an inherited exclusion; null when self-flagged or none. */
  excluded_by_path?: string | null;
  /** Non-folder descendants (1 for a document); powers "applies to N docs". */
  descendant_doc_count?: number;
  /** Per-modifier significance breakdown (from significance_view). */
  importance_modifiers?: Record<string, number>;
  factors?: SignificanceFactor[];
  /** Ancestor folders whose `manual` values cascade into this node's `inherited` factor. */
  inherited_from?: { path: string; value: number }[];
  /** The decayed importance search uses right now. */
  effective_importance?: number;
  /** Age in days behind the decay. */
  age_days?: number;
  /** Descendants the value cascades to (0 for a leaf). */
  descendant_count?: number;
  /** Whole days since the node's project last changed; null outside a project subtree. */
  project_dormant_days?: number | null;
};

/** A "shut off" memory listed in the settings Memories view — noise or dead, with the why. */
export type ExcludedMemory = NodeMeta & {
  summary: string | null;
  lifecycle: Lifecycle; // "noise" | "dead"
  reason: string | null;
  dead_at: string | null; // ISO timestamp; set for dead memories
};

/** A memory in the Provenance view, tagged with how it links to the entity above it. */
export type ProvenanceMemory = NodeMeta & {
  summary: string | null;
  /** 'belongs_to' (the memory was classified under this class) | 'mentions' (named this instance). */
  link_type: string;
};

/** One active entity with the memories it was found in — a Provenance view group. */
export type ProvenanceEntity = NodeMeta & {
  /** Canonical display name of the entity. */
  name: string;
  /** 'class' (a category) | 'instance' (a specific thing). */
  entity_type: string | null;
  status: string; // always 'active' here — the endpoint filters to active entities
  summary: string | null;
  memories: ProvenanceMemory[];
};

export type TreeNode = NodeMeta & { children: TreeNode[] };

export type GraphLink = {
  source: string;
  target: string;
  kind: "containment" | "edge";
  /** Concept-membership links: the member's score (drives stroke width). */
  weight?: number;
};

export type GraphData = { nodes: NodeMeta[]; links: GraphLink[] };

/** Which graph the workspace is showing: memories, entities, or concepts. */
export type GraphMode = "memories" | "entities" | "concepts";

/**
 * Role of a node within the entity graph. `root` is the reserved `entities` hub folder,
 * `class` is a broad category, `instance` is a specific entity filed under a class.
 */
export type EntityRole = "root" | "class" | "instance";

export type EntityNodeMeta = NodeMeta & {
  role: EntityRole;
  /** Kind discriminator: 'class' or 'instance' (null for the root hub). An instance's category
   *  membership is a link now, not this field — read it from the class->instance graph links. */
  entity_type: string | null;
  /** Entity lifecycle ('active' | 'inactive' | …); null/absent for the hub or old payloads. */
  status?: string | null;
};

export type EntityGraphData = { nodes: EntityNodeMeta[]; links: GraphLink[] };

/** The label kinds the concept labeler can assign (mirrors the DB CHECK). */
export type ConceptKind = "project" | "topic" | "area" | "event" | "other";

/** One scored member of a concept — a real KB node (memory or entity). */
export type ConceptMember = {
  node_id: string;
  member_kind: "memory" | "entity";
  score: number;
  label: string | null;
  doc_kind?: string | null;
};

/** One active concept from the derived consolidation layer. Mostly read-only —
 *  a consolidation run replaces the derived fields — but label/status carry human
 *  curation (rename + lock, dismiss/restore) across runs. */
export type Concept = {
  id: string;
  name: string;
  kind: ConceptKind;
  level: number;
  parent_id: string | null;
  description: string | null;
  member_count: number;
  members: ConceptMember[];
  /** True once a human has set the name/kind/description — consolidation won't overwrite it. */
  label_locked: boolean;
  /** 'active' | 'dismissed' — dismissed concepts are hidden from the default graph/list. */
  status: string;
};

/** One emergent document-kind facet with its memory count and lifecycle stage. */
export type DocKindRow = { kind: string; count: number; established: boolean };

/** One structured curation operation — either compiled from NL by `parseCuration`, or
 *  built directly by a UI action, then sent to `applyCuration`. */
export type CurationOp = {
  op: string;
  concept_id?: string;
  node_id?: string;
  left_id?: string;
  right_id?: string;
  name?: string;
  kind?: string;
  description?: string;
  note?: string;
};

/** Result of compiling a natural-language curation instruction: grounded ops, a
 *  human-readable preview per op, and warnings for anything dropped (e.g. out-of-roster
 *  references). Read-only — nothing is written until the ops are sent to `applyCuration`. */
export type CurationParse = {
  ops: CurationOp[];
  summary: string[];
  warnings: string[];
};

/** Role of a node within the concept graph: a discovered hub, or a real KB member node. */
export type ConceptRole = "concept" | "member";

export type ConceptNodeMeta = NodeMeta & {
  role: ConceptRole;
  /** concept nodes: the labeler's kind; member nodes: null. */
  conceptKind: ConceptKind | null;
  /** member nodes: what the underlying KB node is. */
  memberKind?: "memory" | "entity";
  /** member nodes: strongest membership score across concepts. */
  score?: number;
  /** concept nodes: total members (drives hub radius). */
  memberCount?: number;
  description?: string | null;
};

export type ConceptGraphData = { nodes: ConceptNodeMeta[]; links: GraphLink[] };

/**
 * Identifies the active node color scheme (the graph view-filter). Each id maps to a
 * concrete {@link NodeColorScheme} in `lib/colorScheme`. Stored in {@link GraphSettings}.
 */
export type ColorSchemeId = "type" | "depth" | "importance" | "recency" | "retrieval";

export type GraphSettings = {
  /** Layout tightness — drives link distance and charge repulsion. */
  spacing: number;
  /**
   * Strength of the centering pull that draws separate top-level directories
   * (disconnected subgraphs) toward each other. 0 leaves them fully dispersed.
   */
  gravity: number;
  /** Active node color scheme selected in the graph view-filter overlay. */
  colorScheme: ColorSchemeId;
};

export type UpdatePatch = {
  title?: string | null;
  content?: string | null;
  summary?: string | null;
  uri?: string | null;
  tags?: string[] | null;
};

export type IngestEvent = {
  id: number;
  at: string; // ISO timestamp
  event: string; // node_created | node_updated | enrichment_done
  source: string;
  external_id: string | null;
  node_id: string;
  path: string | null;
  content_hash: string | null;
  detail: Record<string, unknown>;
};

export type SyncSource = {
  source: string;
  last_synced_at: string; // ISO timestamp
  cursor: string | null;
};

export type ProviderPause = {
  provider: string;
  until: string; // ISO timestamp
  reason: string;
};

/** A record the engine refused to store; its latest ingest_log event is ingest_failed. */
export type StuckRecord = {
  source: string;
  external_id: string; // push events without one: "inbox#<id>"
  node_id: string;
  path: string | null; // the intended ltree path when derivable
  error: string | null;
  since: string; // ISO timestamp — when the CURRENT error text began
};

/** The signed-in identity (GET /api/me). Absent on backends that predate tenancy. */
export type Me = { email: string; tenant: string; is_owner: boolean };

export type IngestStatus = {
  // When this tenant's kb-agent last reached the gateway; null = never. Absent (undefined)
  // on backends that predate tenancy, so the page can tell "never" from "not reported".
  agent_last_seen_at?: string | null;
  enrichment: Record<string, number>; // enrichment_status -> node count
  pauses: ProviderPause[]; // active provider outages (self-healing pause windows)
  stuck: StuckRecord[]; // records the engine refused to store (banner rows)
  sources: SyncSource[];
};

export type ClassDetail = {
  name: string;
  status: string;
  description: string | null;
  description_locked: boolean;
  pinned: boolean;
  members: number;
  instances: number;
  voters: number;
};

export type ImpactReport = {
  action: string;
  source: string | null;
  target: string | null;
  summary: string;
  affected_memories: number;
  affected_instances: number;
  queued_reenrichment: number;
  warnings: string[];
  reversible: boolean;
  applied: boolean;
};

export type CurationCommandInfo = {
  action: string;
  label: string;
  reversible: boolean;
  args: string[];
};

export type CreateBody = {
  content: string;
  title?: string | null;
  slug?: string | null;
  parent_path?: string;
  type?: string;
  tags?: string[] | null;
};

/** One table in the raw Postgres browser (the password-gated Database page). */
export type DbTableMeta = {
  schema: string;
  name: string;
  /** Number of columns. */
  columns: number;
  /** Estimated row count from pg_class.reltuples; null when never analyzed. */
  est_rows: number | null;
};

/** A column's metadata in the raw table view. */
export type DbColumn = {
  name: string;
  type: string;
  nullable: boolean;
  default: string | null;
};

/** One page of a table's rows, 1:1 with Postgres. Cell values are JSON-coerced server-side. */
export type DbTablePage = {
  schema: string;
  name: string;
  columns: DbColumn[];
  primary_key: string[];
  rows: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
};

/** One side of a pending entity merge suggestion. */
export type MergeSuggestionSide = {
  id: string;
  name: string;
  aliases: string[];
  mention_count: number;
};

/** A pending semantic entity-merge suggestion awaiting human review. */
export type MergeSuggestion = {
  id: number;
  kind: string;
  distance: number;
  judge_reason: string | null;
  suggested_target_id: string | null;
  left: MergeSuggestionSide;
  right: MergeSuggestionSide;
};

/** One returned result inside a trace (ids only; label resolves on the detail route). */
export interface TraceResult {
  id: string;
  kind: string | null;
  rank?: number | null;
  score?: number | null;
  similarity?: number | null;
  via?: string | null;
  label?: string;               // present on TraceDetail rows; "(deleted)" if gone
}

/** One traced MCP retrieval call (list shape: scalar columns only). */
export interface Trace {
  id: number;
  at: string;
  request_id: string;
  session_id: string | null;
  turn_id: number;                // the request this call belongs to (its opener's id)
  caller: string | null;
  tool: string;
  query: string | null;
  target_id: string | null;
  latency_ms: number;
  result_count: number;
  result_bytes: number | null;    // exact serialized payload size; null = unmeasured
  result_tokens: number | null;   // estimated from bytes; null (never 0) = unmeasured
  confidence_level: string | null;
  best_similarity: number | null;
  strategy: string | null;
  judge: string | null;
  crawl_mode: string | null;
  llm_calls: number;
  embedder_ok: boolean;
  error: string | null;
  judgment: "good" | "partial" | "bad" | null;   // latest verdict
  has_replay?: boolean;           // a captured replay exists (else Replay is partial)
}

export interface TraceJudgment {
  id: number;
  verdict: "good" | "partial" | "bad";
  judged_by: string;
  notes: string | null;
  at: string;
}

/** Drill-down shape: the full row plus payloads and judgments. */
export interface TraceDetail extends Trace {
  args: Record<string, unknown>;
  results: TraceResult[] | null;
  trace: Record<string, unknown> | null;
  judgments: TraceJudgment[];
}

/** How a request ended, from its own calls: opened > returned > error > empty. */
export type TurnOutcome = "opened" | "returned" | "empty" | "error";

/** One user request: the calls one agent made for it, rolled up. Identified by its
 *  first call's id. */
export interface Turn {
  id: number;
  session_id: string | null;
  caller: string | null;
  started_at: string;
  ended_at: string;
  calls: number;
  tools: Record<string, number>;
  first_query: string | null;
  result_bytes: number | null;    // summed over measured calls; null = none measured
  result_tokens: number | null;   // estimated from bytes; null (never 0) = unmeasured
  errors: number;
  outcome: TurnOutcome;
  judgment: "good" | "partial" | "bad" | null;   // latest request-level verdict
}

/** Drill-down shape: the request plus its calls (oldest first) and judgments. */
export interface TurnDetail extends Turn {
  traces: Trace[];
  judgments: TraceJudgment[];
}

/** One tool's slice of the window's context cost. */
export interface TokensByTool {
  tool: string;
  calls: number;
  tokens_median: number | null;
  tokens_total: number | null;
}

/** Aggregates over the trailing trace window. */
export interface TraceSummary {
  days: number;
  calls: number;
  latency_p50_ms: number | null;
  latency_p90_ms: number | null;
  weak_or_none: number;
  weak_or_none_rate: number | null;
  embedder_failures: number;
  errors: number;
  llm_calls: number;
  confidence_levels: Record<string, number>;
  followup_rate: number | null;
  turns: number;                        // requests whose first call ended in the window
  calls_per_turn_median: number | null;
  tokens_per_turn_median: number | null;
  turn_gap_s: number;                   // the grouping gap in force, disclosed
  tokens_measured: number;        // calls in the window carrying a measurement
  tokens_median: number | null;
  tokens_total: number | null;
  tokens_per_result: number | null;
  bytes_per_token: number;        // the divisor in force, disclosed in the glossary
  tokens_by_tool: TokensByTool[];
}

export interface ArchetypeScore {
  n: number;
  scored: number;
  must_recall: number | null;
  mrr: number | null;
}

/** One timestamped eval of the legacy/current plans (the trend series). */
export interface ReferenceRun {
  ran_at: string;
  overall_must_recall: number;
  mrr: number;
  items_scored: number;
  items_total: number;
  by_archetype: Record<string, ArchetypeScore>;
}

/** One benchmark-grid cell, aggregated over its repeats. */
export interface GridCell {
  cell: string;
  set: string | null;
  strategy: string | null;
  judge: string | null;
  repeats: number;
  must_recall: number | null;
  neg_hits: number | null;
  latency_p50_ms: number | null;
  latency_p90_ms: number | null;
  by_archetype: Record<string, number>;
}

export interface EvalGrid {
  date: string;
  git_heads: string[];
  cells: GridCell[];
}

export interface OffCorpusProbe {
  probe: string;
  mode_a: { level: string; best_similarity: number | null; n: number };
  mode_b: { level: string; best_similarity: number | null; n: number };
}

export interface OffCorpusReport {
  verdict: string;
  threshold: number;
  worst_best_similarity: number;
  probes: OffCorpusProbe[];
}

export interface AblationSweep {
  name: string;
  set: string;
  must_recall: number;
  n_scored: number;
}

export interface EvalsPayload {
  reference_runs: ReferenceRun[];
  grids: EvalGrid[];
  off_corpus: OffCorpusReport | null;
  ablations: { sweeps: AblationSweep[] } | null;
  warnings: string[];
}

export interface McpToolState {
  tool: string;
  kind: "read" | "write";
  enabled: boolean;
  updated_at: string;
  updated_by: string | null;
}

/** One entry in the manual-rebuild queue (the click the dashboard made). */
export type ConsolidationRequest = {
  id: number;
  requested_at: string;
  claimed_at: string | null;
  finished_at: string | null;
  error: string | null;
};

/** Is the concept layer rebuilding, and what did the last rebuild do?
 *  `stalled` means either nobody claimed the request (daemon down) or a `running` row
 *  outlived KB_CONSOLIDATE_RUN_TIMEOUT. */
export type ConsolidationStatus = {
  state: "idle" | "queued" | "running" | "stalled";
  request: ConsolidationRequest | null;
  running: {
    run_id: number;
    started_at: string;
    phase: string | null;
    trigger_reason: string;
  } | null;
  last: {
    run_id: number;
    finished_at: string;
    status: string;
    trigger_reason: string;
    /** The whole-pass failure reason on a `failed` row (from `run_once`'s `except`);
     *  null on a successful run. */
    error: string | null;
    communities: number | null;
    kept: number | null;
    created: number | null;
    updated: number | null;
    retired: number | null;
    discarded: number | null;
    label_failures: number | null;
    embed_failures: number | null;
    label_error: string | null;
    embed_error: string | null;
  } | null;
  /** Memories still pending/processing enrichment — a forced pass clusters over them. */
  backlog: number;
};

/** Health of the Claude credential the enrichment provider runs on. Never carries a token. */
export interface ClaudeAuthStatus {
  level: "ok" | "warn" | "critical" | "out";
  logged_in: boolean;
  days_left: number | null;   // fractional days; null when the credential file is unreadable
  expires_at: string | null;  // ISO 8601
  email: string | null;
  /** Server opted in to the one-click re-auth (KB_DASHBOARD_ALLOW_RELOGIN); absent = off. */
  relogin_enabled?: boolean;
}

/** State of the content age-decay master switch. */
export interface AgeDecayState {
  enabled: boolean;
}

/** GET /api/policy — the per-install policy the dashboard process loaded (read-only). */
export interface PolicyDecayRule {
  name: string;
  match: { tags?: string[]; path_prefix?: string; source?: string };
  half_life_days: number;
  floor: number;
}

export interface PolicyView {
  source: string | null;      // policy file path, or null for the shipped defaults
  is_default: boolean;
  overlays: string[];         // prompt files the overlay directory shadows
  policy: {
    decay: PolicyDecayRule[];
    projects: { folder_label: string; max_depth: number; dormancy_half_life_days: number } | null;
    importance: { recency_half_life_days: number; recency_floor: number; access_saturation: number };
    concepts: {
      goal_window_days: number;
      data_recency_half_life_days: number;
      retrieval_recency_half_life_days: number;
      recency_floor: number;
    };
    taxonomy: {
      categories: { name: string; kind: string; description: string }[];
      kind_seeds: Record<string, string>;
    };
    prompts: { overlay_dir: string | null };
  };
}

/** Retrieval-heatmap time bucket. */
export type HeatBucket = "day" | "week";

/** Retrieval-heatmap trailing window: a day count, or all recorded history. */
export type HeatWindow = number | "all";

/** One non-empty heatmap cell: a memory's hits within one bucket. */
export interface RetrievalHeatCell {
  bucket: string; // YYYY-MM-DD, the bucket's first day (viewer's timezone)
  surfaced: number;
  opened: number;
}

/** One memory's row: totals over the window plus its sparse cells. */
export interface RetrievalHeatRow {
  id: string;
  title: string | null;
  path: string;
  surfaced: number;
  opened: number;
  cells: RetrievalHeatCell[];
}

/** GET /api/retrieval/heatmap payload. `buckets` is gap-free; `max_cell` is the colour domain. */
export interface RetrievalHeatmapData {
  window: { days: number | null; bucket: HeatBucket; tz: string; start: string; end: string };
  buckets: string[];
  rows: RetrievalHeatRow[];
  total_memories: number;
  max_cell: number;
}

export interface RetrievalHeatmapParams {
  days: HeatWindow;
  bucket: HeatBucket;
  tz: string;
  path: string | null;
  /** Max memories returned (server default 200, clamped to 1..1000). */
  limit?: number;
}

/** One ranked id in a replay list; fields absent when the stage did not record them. */
export interface ReplayScored {
  id: string;
  score?: number | null;
  sim?: number | null;
  rrf?: number | null;
}

export interface ReplayFlat {
  fts_query?: string;
  or_fallback?: boolean;
  fts?: ReplayScored[];      // absent = not recorded (partial replay)
  knn?: ReplayScored[];
  fused: ReplayScored[];
  embedder?: "ok" | "unavailable";
}

export interface ReplayRef {
  kind: "concept" | "entity";
  id: string;
  label: string | null;
}

export interface ReplayEntry extends ReplayRef {
  via: string;
  rank: number;
}

export interface ReplayOffered {
  id: string;
  family: "concept" | "bridge";
  parents: ReplayRef[];
  admitted: boolean;
}

export interface ReplayHop {
  hop: number;
  by?: string | null;
  note?: string | null;
  offered_count: number;
  admitted_count: number;
  offered?: ReplayOffered[]; // absent = not recorded (partial replay)
}

export interface ReplayCrawl {
  entry: ReplayEntry[];
  hops: ReplayHop[];
  lists?: { flat: string[]; sim: string[]; admit: string[]; corrob: string[] };
  final?: ReplayScored[];
  stop_reason?: string | null;
}

export interface ReplayRecord {
  v: 1;
  limit: number;
  flat: ReplayFlat;
  crawl?: ReplayCrawl;
}

export interface ReplayLabel {
  title: string;
  path: string | null;
  doc_kind: string | null;
  deleted: boolean;
}

export interface TraceReplay {
  trace_id: number;
  tool: string;
  query: string | null;
  at: string;
  args: Record<string, unknown>;
  completeness: "full" | "partial";
  replay: ReplayRecord;
  labels: Record<string, ReplayLabel>;
}

// ---- Skills page (read-only over the skill registry) -------------------------------

/** The usage windows the API accepts. */
export type UsageDays = 7 | 30 | 90;

/** Which library a view shows. */
export type LibraryKind = "skill" | "agent";

export type ReviewStatus = "pending" | "approved" | "rejected" | "superseded";

/** One row of GET /api/skills or /api/agents. `revision` is the one served (the approved
 *  one, or the newest while none is approved). */
export interface SkillSummary {
  name: string;
  description: string;
  revision: number;
  latest_revision: number;
  approved_revision: number | null;
  review_status: ReviewStatus;          // of the newest revision
  pending_revision: number | null;
  updated_at: string;
  archived: boolean;
}

export interface SkillPage {
  skills: SkillSummary[];
  next_cursor: string | null;
  query: string | null;
}

export interface SkillFileEntry {
  path: string;
  bytes: number;
  content_hash: string;
}

export interface SkillRevisionEntry {
  revision: number;
  actor: string | null;        // verified principal; null = no principal on that listener
  agent_label: string | null;  // self-reported by the agent, never trusted for access
  change_summary: string;
  package_hash: string;
  created_at: string;
  review_status: ReviewStatus;
  reviewed_at: string | null;
  reviewer_label: string | null;
  review_notes: string | null;
}

/** GET /api/skills/{name}: one revision with SKILL.md inline and the newest history. */
export interface SkillDetailData {
  name: string;
  description: string;
  revision: number;
  current_revision: number;
  archived: boolean;
  archived_at: string | null;
  archived_by: string | null;
  package_hash: string;
  change_summary: string;
  actor: string | null;
  agent_label: string | null;
  created_at: string;
  kind?: LibraryKind;
  review_status: ReviewStatus;
  approved_revision: number | null;
  latest_revision: number;
  pending_revision: number | null;
  reviewer_label: string | null;
  review_notes: string | null;
  skill_md?: string;
  agent_md?: string;
  files: SkillFileEntry[];
  history: SkillRevisionEntry[];
}

export interface SkillFileContent {
  name: string;
  revision: number;
  path: string;
  content: string;
  content_hash: string;
}

export interface SkillLoad {
  at: string;
  revision: number | null;     // null = "current at the time", not recorded
  session_id: string | null;
  turn_id: number | null;
}

export interface SkillUsageData {
  name: string;
  days: UsageDays;
  tz: string;
  since: string;
  loads: number;
  file_reads: number;
  surfaced: number;
  last_loaded_at: string | null;
  surfaced_since: string | null;
  recent_loads: SkillLoad[];
}

export interface SkillLoadCount {
  loads: number;
  last_loaded_at: string;
}

export interface SkillLibraryUsageData {
  days: UsageDays;
  tz: string;
  since: string;
  searches_per_day: { day: string; searches: number; missed: number }[];
  missed_searches: { query: string; count: number; last_at: string }[];
  missing_requests: { name: string; count: number; last_at: string }[];
  per_skill: Record<string, SkillLoadCount>;
  never_loaded: string[];
  surfaced_since: string | null;
}
