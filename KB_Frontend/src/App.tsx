import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  fetchConcepts,
  fetchEntities,
  fetchMe,
  fetchTree,
  primeConcepts,
  primeEntities,
} from "./api/client";
import { useChangeFeed } from "./hooks/useChangeFeed";
import { useIsMobile } from "./hooks/useMediaQuery";
import { useUrlParam } from "./hooks/useUrlParam";
import { AnalysisToasts } from "./components/AnalysisToasts";
import { AgeDecayToggle } from "./components/AgeDecayToggle";
import { ConceptCard } from "./components/ConceptCard";
import { ConceptCommandBar } from "./components/ConceptCommandBar";
import { ConceptExplorer } from "./components/ConceptExplorer";
import { ConceptRebuild } from "./components/ConceptRebuild";
import { CreateClass } from "./components/CreateClass";
import { DatabasePage } from "./components/DatabasePage";
import { DetailPanel } from "./components/DetailPanel";
import { DocKindsPanel } from "./components/DocKindsPanel";
import { EntityNav } from "./components/EntityNav";
import {
  ActivityIcon,
  ChevronIcon,
  DatabaseIcon,
  FunnelIcon,
  GearIcon,
  GraphIcon,
  LayersIcon,
  MenuIcon,
  NoteIcon,
  SparkIcon,
} from "./components/icons";
import { IngestionPage } from "./components/IngestionPage";
import { MergeSuggestions } from "./components/MergeSuggestions";
import { ProvenancePage } from "./components/ProvenancePage";
import { ClaudeAuthBanner } from "./components/ClaudeAuthBanner";
import { ReenrichBanner } from "./components/ReenrichBanner";
import { RetrievalPage } from "./components/RetrievalPage";
import { SettingsModal } from "./components/SettingsModal";
import { SkillsPage } from "./components/SkillsPage";
import { TreeView } from "./components/TreeView";
import { importanceDomain } from "./lib/color";
import { categoryNameForNode } from "./lib/entities";
import { isOwner } from "./lib/identity";
import { loadSettings, saveSettings } from "./lib/settings";
import { buildTree } from "./lib/tree";
import type { Concept, EntityGraphData, GraphMode, GraphSettings, Me, NodeMeta } from "./types";

// Lazy-load the graph: react-force-graph-2d (canvas + d3) is the heaviest dependency and
// only the Graph view renders it, so keep it out of the initial bundle.
const GraphView = lazy(() =>
  import("./components/GraphView").then((m) => ({ default: m.GraphView })),
);

type View = "graph" | "ingest" | "database" | "provenance" | "merges" | "kinds" | "retrieval" | "skills";

export default function App() {
  const [nodes, setNodes] = useState<NodeMeta[]>([]);
  const [folderPath, setFolderPath] = useState<string | null>(null);
  const [nodeId, setNodeId] = useState<string | null>(null);
  const [settings, setSettings] = useState<GraphSettings>(loadSettings);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // View tab and graph mode live in the URL so refresh / share / back / deep-link work.
  const [view, setView] = useUrlParam<View>("view", "graph");
  // Mobile collapses the dashboard to a graph-only surface: the other pages are
  // desktop-only, the explorer pane becomes a slide-in drawer, and the topbar
  // folds its stat + settings behind a disclosure chevron.
  const isMobile = useIsMobile();
  // Who is signed in: undefined until fetchMe settles (it retries, then gives up), then the
  // identity; null on a pre-tenancy backend without /api/me, which isOwner() reads as the
  // owner, so the chrome is unchanged there; UNKNOWN_ME (a non-owner) when /api/me cannot
  // be read. Owner-only chrome waits for the answer and fails closed, so a tenant never
  // sees it, not even for one paint (the backend 403s those routes regardless).
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  useEffect(() => {
    fetchMe().then(setMe);
  }, []);
  const meKnown = me !== undefined;
  const owner = meKnown && isOwner(me);
  // A tenant deep-linked to ?view=database lands on the graph: the page is owner-only.
  const effectiveView: View =
    isMobile || (view === "database" && meKnown && !owner) ? "graph" : view;
  const [explorerOpen, setExplorerOpen] = useState(false);
  const [topbarOpen, setTopbarOpen] = useState(false);
  // The graph view toggles between the memory folder graph and the isolated entity graph.
  const [graphMode, setGraphMode] = useUrlParam<GraphMode>("mode", "memories");
  const [entityData, setEntityData] = useState<EntityGraphData | null>(null);
  const [entityClass, setEntityClass] = useState<string | null>(null);
  const [showInstances, setShowInstances] = useState(false);
  // Inactive (never-activated) entity classes are hidden from the graph by default.
  const [showInactive, setShowInactive] = useState(false);
  const [conceptsRaw, setConceptsRaw] = useState<Concept[] | null>(null);
  const [conceptsError, setConceptsError] = useState<string | null>(null);
  const [conceptId, setConceptId] = useState<string | null>(null);
  const conceptsRequestGeneration = useRef(0);
  // Bumped on every live `nodes` change so the open subtree refetches without remounting.
  const [liveVersion, setLiveVersion] = useState(0);

  const reloadTree = useCallback(() => {
    fetchTree().then(setNodes);
  }, []);
  const reloadEntities = useCallback(() => {
    fetchEntities().then(setEntityData);
  }, []);
  const reloadConcepts = useCallback(async () => {
    const generation = ++conceptsRequestGeneration.current;
    setConceptsError(null);
    try {
      const response = await fetchConcepts();
      if (generation !== conceptsRequestGeneration.current) return;
      setConceptsRaw(response.concepts);
    } catch (error) {
      if (generation !== conceptsRequestGeneration.current) return;
      setConceptsRaw(null);
      setConceptsError(error instanceof Error ? error.message : "Concepts could not be loaded");
    }
  }, []);

  useEffect(() => reloadTree(), [reloadTree]);
  // Once the tree has landed (never before — it is the critical path), warm the other
  // graph layers so the first switch into entities/concepts renders without a round trip.
  // The layer the page opened on is skipped: its own mode-entry effect already fetched it.
  const layersPrimed = useRef(false);
  useEffect(() => {
    if (layersPrimed.current || nodes.length === 0) return;
    layersPrimed.current = true;
    if (graphMode !== "entities") primeEntities();
    if (graphMode !== "concepts") primeConcepts();
  }, [nodes, graphMode]);
  useEffect(() => saveSettings(settings), [settings]);
  // Load the entity graph whenever the user switches into entities mode (cheap for a
  // personal KB, and keeps it fresh on each entry).
  useEffect(() => {
    if (graphMode === "entities") reloadEntities();
  }, [graphMode, reloadEntities]);
  // Concepts refresh on each entry into concepts mode (read-only derived layer — no
  // change-feed trigger exists for it); leaving the mode closes any open concept card.
  useEffect(() => {
    if (graphMode === "concepts") reloadConcepts();
    else setConceptId(null);
  }, [graphMode, reloadConcepts]);

  // A node selection and a concept selection are mutually exclusive.
  const selectNode = useCallback((id: string) => {
    setConceptId(null);
    setNodeId(id);
  }, []);
  const selectConcept = useCallback((id: string) => {
    setNodeId(null);
    setConceptId(id);
  }, []);
  // Explorer-pane selections also dismiss the mobile drawer so the graph (or the
  // opened detail) is immediately visible. No-op on desktop, where the drawer
  // state has no effect on layout.
  const closeExplorer = useCallback(() => setExplorerOpen(false), []);
  const selectNodeFromPane = useCallback(
    (id: string) => {
      setNodeId(id);
      closeExplorer();
    },
    [closeExplorer],
  );
  const selectConceptFromPane = useCallback(
    (id: string) => {
      selectConcept(id);
      closeExplorer();
    },
    [selectConcept, closeExplorer],
  );
  const selectFolderFromPane = useCallback(
    (path: string) => {
      setFolderPath(path);
      closeExplorer();
    },
    [closeExplorer],
  );
  const selectRootFromPane = useCallback(() => {
    setFolderPath(null);
    closeExplorer();
  }, [closeExplorer]);
  const selectClassFromPane = useCallback(
    (name: string | null) => {
      setEntityClass(name);
      closeExplorer();
    },
    [closeExplorer],
  );
  const selectedConcept = useMemo(
    () => conceptsRaw?.find((c) => c.id === conceptId) ?? null,
    [conceptsRaw, conceptId],
  );
  const selectedConceptParent = useMemo(
    () => conceptsRaw?.find((c) => c.id === selectedConcept?.parent_id)?.name ?? null,
    [conceptsRaw, selectedConcept],
  );

  // Keep the UI live: when the DB's nodes change (ingest, enrichment, edits — from any
  // process), refetch the tree, nudge the graph to refresh its current subtree, and
  // refresh the entity graph when it's the active view.
  useChangeFeed((tables) => {
    if (tables.has("nodes")) {
      reloadTree();
      setLiveVersion((v) => v + 1);
      if (graphMode === "entities") reloadEntities();
    }
  });

  const roots = useMemo(() => buildTree(nodes), [nodes]);
  const noteCount = useMemo(
    () => nodes.filter((n) => n.type !== "folder").length,
    [nodes],
  );
  // Comparative importance scale spans the whole KB so colors stay consistent whether
  // you're viewing the root graph or a single subtree. In entities mode it's scaled over
  // the entity nodes so their colors compare against each other, not the memory tree.
  const impDomain = useMemo(() => {
    const source =
      graphMode === "entities" && entityData ? entityData.nodes : nodes;
    return importanceDomain(source.map((n) => n.importance));
  }, [graphMode, entityData, nodes]);

  // When the open node is a class (not an instance/memory), DetailPanel switches to curation.
  // Use the canonical category name (not the slug) so multi-word classes resolve in the API.
  const selectedClassName = useMemo(
    () => categoryNameForNode(entityData, nodeId),
    [entityData, nodeId],
  );

  const topbarStat = (
    <span className="topbar-stat">
      <b>{nodes.length}</b> nodes
      <span style={{ opacity: 0.4 }}>·</span>
      <b>{noteCount}</b> notes
    </span>
  );
  const settingsButton = (
    <button
      className="icon-btn"
      aria-label="Graph settings"
      title="Graph settings"
      onClick={() => setSettingsOpen(true)}
    >
      <GearIcon size={18} />
    </button>
  );

  return (
    <div className="app">
      <header className="topbar">
        {isMobile && (
          <button
            className="icon-btn"
            aria-label="Toggle explorer"
            title="Toggle explorer"
            aria-expanded={explorerOpen}
            onClick={() => setExplorerOpen((open) => !open)}
          >
            <MenuIcon size={18} />
          </button>
        )}
        <div className="brand">
          <span className="brand-mark">
            <SparkIcon size={17} />
          </span>
          <h1 className="brand-name">Knowledge Base</h1>
          <span className="brand-sub">· locate by path, expand by graph</span>
        </div>
        {!isMobile && (
          <nav className="tabs" aria-label="Views">
            <button
              className={`tab${view === "graph" ? " active" : ""}`}
              aria-current={view === "graph" ? "page" : undefined}
              onClick={() => setView("graph")}
            >
              <GraphIcon size={15} />
              Graph
            </button>
            <button
              className={`tab${view === "ingest" ? " active" : ""}`}
              aria-current={view === "ingest" ? "page" : undefined}
              onClick={() => setView("ingest")}
            >
              <ActivityIcon size={15} />
              Ingestion
            </button>
            {owner && (
              <button
                className={`tab${view === "database" ? " active" : ""}`}
                aria-current={view === "database" ? "page" : undefined}
                onClick={() => setView("database")}
              >
                <DatabaseIcon size={15} />
                Database
              </button>
            )}
            <button
              className={`tab${view === "provenance" ? " active" : ""}`}
              aria-current={view === "provenance" ? "page" : undefined}
              onClick={() => setView("provenance")}
            >
              <LayersIcon size={15} />
              Provenance
            </button>
            <button
              className={`tab${view === "kinds" ? " active" : ""}`}
              aria-current={view === "kinds" ? "page" : undefined}
              onClick={() => setView("kinds")}
            >
              <NoteIcon size={15} />
              Kinds
            </button>
            <button
              className={`tab${view === "merges" ? " active" : ""}`}
              aria-current={view === "merges" ? "page" : undefined}
              onClick={() => setView("merges")}
            >
              Merges
            </button>
            <button
              className={`tab${view === "retrieval" ? " active" : ""}`}
              aria-current={view === "retrieval" ? "page" : undefined}
              onClick={() => setView("retrieval")}
            >
              <FunnelIcon size={15} />
              Retrieval
            </button>
            <button
              className={`tab${view === "skills" ? " active" : ""}`}
              aria-current={view === "skills" ? "page" : undefined}
              onClick={() => setView("skills")}
            >
              Skills
            </button>
          </nav>
        )}
        <div className="topbar-spacer" />
        {isMobile ? (
          <button
            className={`icon-btn topbar-more-btn${topbarOpen ? " open" : ""}`}
            aria-label="Toggle toolbar"
            title="Toggle toolbar"
            aria-expanded={topbarOpen}
            onClick={() => setTopbarOpen((open) => !open)}
          >
            <ChevronIcon size={15} />
          </button>
        ) : (
          <>
            {topbarStat}
            {view === "graph" && settingsButton}
          </>
        )}
        {isMobile && topbarOpen && (
          <div className="topbar-more">
            {topbarStat}
            {settingsButton}
          </div>
        )}
      </header>

      {owner && <ClaudeAuthBanner />}
      <ReenrichBanner />
      <AnalysisToasts />

      {effectiveView === "skills" ? (
        <SkillsPage />
      ) : effectiveView === "retrieval" ? (
        meKnown && <RetrievalPage isOwner={owner} />
      ) : effectiveView === "merges" ? (
        <MergeSuggestions />
      ) : effectiveView === "provenance" ? (
        <ProvenancePage />
      ) : effectiveView === "kinds" ? (
        <DocKindsPanel />
      ) : effectiveView === "ingest" ? (
        <IngestionPage />
      ) : effectiveView === "database" ? (
        owner && <DatabasePage />
      ) : (
        <div className="workspace">
          {/* Layout-transparent on desktop (display: contents); a slide-in drawer
              over the graph on mobile, closed until the topbar menu opens it. */}
          <div className={`explorer${isMobile && !explorerOpen ? " closed" : ""}`}>
            {graphMode === "entities" ? (
              <div className="entity-pane">
                {owner && <CreateClass onCreated={reloadEntities} />}
                <EntityNav
                  entityData={entityData}
                  selectedClass={entityClass}
                  selectedNodeId={nodeId}
                  onSelectClass={selectClassFromPane}
                  onSelectNode={selectNodeFromPane}
                />
              </div>
            ) : graphMode === "concepts" ? (
              <aside className="concepts-pane">
                <ConceptRebuild onFinished={reloadConcepts} />
                <AgeDecayToggle />
                <ConceptExplorer
                  concepts={conceptsRaw}
                  selectedConceptId={conceptId}
                  error={conceptsError}
                  onSelect={selectConceptFromPane}
                  onRetry={reloadConcepts}
                />
                {owner && (
                  <section className="concept-curation" aria-label="Curate concepts">
                    <div className="pane-head">Curate concepts</div>
                    <ConceptCommandBar onApplied={reloadConcepts} />
                  </section>
                )}
              </aside>
            ) : (
              <TreeView
                roots={roots}
                selectedPath={folderPath}
                selectedNodeId={nodeId}
                onSelectFolder={selectFolderFromPane}
                onSelectRoot={selectRootFromPane}
                onSelectNode={selectNodeFromPane}
              />
            )}
          </div>
          {isMobile && explorerOpen && (
            <button
              className="explorer-scrim"
              aria-label="Close explorer"
              onClick={closeExplorer}
            />
          )}
          <Suspense fallback={<div className="graph" aria-busy="true" />}>
            <GraphView
              mode={graphMode}
              onModeChange={setGraphMode}
              folderPath={folderPath}
              nodes={nodes}
              entityData={entityData}
              entityClass={entityClass}
              showInstances={showInstances}
              onShowInstancesChange={setShowInstances}
              showInactive={showInactive}
              onShowInactiveChange={setShowInactive}
              onSelectClass={setEntityClass}
              settings={settings}
              onSettingsChange={setSettings}
              importanceDomain={impDomain}
              liveVersion={liveVersion}
              concepts={conceptsRaw}
              conceptsError={conceptsError}
              onRetryConcepts={reloadConcepts}
              onSelectConcept={selectConcept}
              onSelectNode={selectNode}
            />
          </Suspense>
          {conceptId ? (
            <ConceptCard
              key={conceptId}
              concept={selectedConcept}
              parentName={selectedConceptParent}
              onClose={() => setConceptId(null)}
              onSelectNode={selectNode}
              onChanged={reloadConcepts}
            />
          ) : (
            nodeId && (
              <DetailPanel
                key={nodeId}
                nodeId={nodeId}
                categoryName={selectedClassName}
                isOwner={owner}
                onClose={() => setNodeId(null)}
                onSelectNode={selectNode}
                onChanged={() => {
                  reloadTree();
                  if (graphMode === "entities") reloadEntities();
                }}
              />
            )
          )}
        </div>
      )}

      {settingsOpen && (
        <SettingsModal
          me={me ?? null}
          settings={settings}
          onChange={setSettings}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </div>
  );
}
