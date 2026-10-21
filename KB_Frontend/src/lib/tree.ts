import type {
  Concept,
  ConceptGraphData,
  ConceptNodeMeta,
  EntityGraphData,
  EntityNodeMeta,
  GraphData,
  GraphLink,
  NodeMeta,
  TreeNode,
} from "../types";

/** Number of ltree labels in a path ("a.b.c" → 3); 1 for a root-level node. */
export function pathDepth(path: string | null | undefined): number {
  return path ? path.split(".").length : 1;
}

export function buildTree(nodes: NodeMeta[]): TreeNode[] {
  const byId = new Map<string, TreeNode>();
  for (const n of nodes) byId.set(n.id, { ...n, children: [] });

  const roots: TreeNode[] = [];
  for (const n of nodes) {
    const node = byId.get(n.id)!;
    const parent = n.parent_id ? byId.get(n.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }

  const sortRec = (arr: TreeNode[]) => {
    arr.sort((a, b) => a.slug.localeCompare(b.slug));
    for (const c of arr) sortRec(c.children);
  };
  sortRec(roots);
  return roots;
}

/**
 * Builds a root-level graph from the full node list. Only containment links
 * (parent → child) are emitted, so each top-level folder forms its own
 * disconnected subgraph and nothing crosses between subdirectories. Files that
 * sit directly at the root (no parent) have no links and render as standalone
 * nodes.
 */
export function buildRootGraph(nodes: NodeMeta[]): GraphData {
  const ids = new Set(nodes.map((n) => n.id));
  const links: GraphLink[] = [];
  for (const n of nodes) {
    if (n.parent_id && ids.has(n.parent_id)) {
      links.push({ source: n.parent_id, target: n.id, kind: "containment" });
    }
  }
  return { nodes, links };
}

/**
 * Derives the displayed entity graph from the full entity graph.
 *
 * The `entities` hub and every class are ALWAYS present — they form one stable shared
 * space that both the all-entities and drill-in views live inside (the drill-in just
 * hides the classes it isn't focused on, rather than swapping to a new scene). Instances,
 * by contrast, don't exist in the graph at all until they're wanted:
 *  - With `focusClassId`: that class's instances are added (they appear around it).
 *  - Otherwise, only when `showInstances`: every instance is added.
 *
 * Links are kept only when both endpoints survive the node filter.
 */
/**
 * Instance⇄class membership, derived from the graph's containment links (Phase 2: an instance
 * can belong to several classes, so membership is a link, not the tree parent_id). Returns both
 * directions: `instanceClasses` (instanceId -> set of classIds) and `classInstances`
 * (classId -> instanceIds).
 */
export function entityMembership(data: EntityGraphData): {
  instanceClasses: Map<string, Set<string>>;
  classInstances: Map<string, string[]>;
} {
  const role = new Map(data.nodes.map((n) => [n.id, n.role]));
  const instanceClasses = new Map<string, Set<string>>();
  const classInstances = new Map<string, string[]>();
  for (const l of data.links) {
    if (role.get(l.source) === "class" && role.get(l.target) === "instance") {
      let cs = instanceClasses.get(l.target);
      if (!cs) instanceClasses.set(l.target, (cs = new Set()));
      cs.add(l.source);
      let is = classInstances.get(l.source);
      if (!is) classInstances.set(l.source, (is = []));
      is.push(l.target);
    }
  }
  return { instanceClasses, classInstances };
}

export function buildEntityGraph(
  data: EntityGraphData,
  opts: { focusClassId?: string | null; showInstances?: boolean; showInactive?: boolean } = {},
): EntityGraphData {
  const { focusClassId = null, showInstances = false, showInactive = false } = opts;
  const { instanceClasses } = entityMembership(data);
  // Inactive classes leave the space unless revealed; the drilled-in focus class is exempt
  // (root and drilled views share one simulation space, so the class set must agree).
  const classVisible = (n: EntityNodeMeta) =>
    showInactive || n.status !== "inactive" || n.id === focusClassId;
  const visibleClassIds = new Set(
    data.nodes.filter((n) => n.role === "class" && classVisible(n)).map((n) => n.id),
  );
  const nodes = data.nodes.filter((n) => {
    if (n.role === "class") return classVisible(n);
    if (n.role !== "instance") return true; // the hub
    // drill-in reveals an instance iff it BELONGS TO the focused class (a link, not parent_id)
    if (focusClassId) return instanceClasses.get(n.id)?.has(focusClassId) ?? false;
    if (!showInstances) return false;
    // no orphaned diamonds: an instance follows its classes out of the view — but a
    // class-less instance stays (unclassified ≠ inactive).
    const cls = instanceClasses.get(n.id);
    return !cls || cls.size === 0 || [...cls].some((c) => visibleClassIds.has(c));
  });
  const ids = new Set(nodes.map((n) => n.id));
  const links = data.links.filter((l) => ids.has(l.source) && ids.has(l.target));
  return { nodes, links };
}

/**
 * Builds the concept graph: concept hubs plus their member nodes (real memories and
 * entities). Members are deduped across concepts, so a node in two concepts renders once
 * with a link to each — shared members become visible bridges between communities.
 * Hierarchy: a level-1 child links to its parent, and a parent with children only links
 * the members NOT already covered by one of its children (the child is the finer home;
 * duplicating every member on the parent would double the edges and swallow the children).
 */
export function buildConceptGraph(concepts: Concept[]): ConceptGraphData {
  const conceptIds = new Set(concepts.map((c) => c.id));
  const childrenOf = new Map<string, Concept[]>();
  for (const c of concepts) {
    if (c.parent_id && conceptIds.has(c.parent_id)) {
      let kids = childrenOf.get(c.parent_id);
      if (!kids) childrenOf.set(c.parent_id, (kids = []));
      kids.push(c);
    }
  }
  const nodes: ConceptNodeMeta[] = [];
  const links: GraphLink[] = [];
  const memberNode = new Map<string, ConceptNodeMeta>();
  for (const c of concepts) {
    nodes.push({
      id: c.id,
      path: "",
      parent_id: c.parent_id,
      type: "concept",
      slug: c.name,
      title: c.name,
      tags: [],
      importance: 1,
      role: "concept",
      conceptKind: c.kind,
      memberCount: c.member_count,
      description: c.description,
      analysis_excluded: false,
      analysis_excluded_effective: false,
    });
    if (c.parent_id && conceptIds.has(c.parent_id)) {
      links.push({ source: c.parent_id, target: c.id, kind: "containment" });
    }
  }
  for (const c of concepts) {
    const covered = new Set(
      (childrenOf.get(c.id) ?? []).flatMap((k) => k.members.map((m) => m.node_id)),
    );
    for (const m of c.members) {
      if (covered.has(m.node_id)) continue; // the child is the finer home
      let n = memberNode.get(m.node_id);
      if (!n) {
        n = {
          id: m.node_id,
          path: "",
          parent_id: null,
          type: m.member_kind === "entity" ? "entity" : "note",
          slug: m.label ?? m.node_id.slice(0, 8),
          title: m.label,
          tags: [],
          importance: 0,
          role: "member",
          conceptKind: null,
          memberKind: m.member_kind,
          score: m.score,
          analysis_excluded: false,
          analysis_excluded_effective: false,
        };
        memberNode.set(m.node_id, n);
        nodes.push(n);
      } else if ((n.score ?? 0) < m.score) {
        n.score = m.score;
      }
      links.push({ source: c.id, target: m.node_id, kind: "containment", weight: m.score });
    }
  }
  return { nodes, links };
}
