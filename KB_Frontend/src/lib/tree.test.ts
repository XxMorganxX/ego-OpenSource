import { describe, expect, it } from "vitest";

import type { Concept, EntityGraphData, EntityNodeMeta, NodeMeta } from "../types";
import { buildConceptGraph, buildEntityGraph, buildRootGraph, buildTree, pathDepth } from "./tree";

function meta(over: Partial<NodeMeta> & { id: string; path: string; slug: string }): NodeMeta {
  return { parent_id: null, type: "note", title: null, tags: [], importance: 1, ...over };
}

describe("pathDepth", () => {
  it("counts ltree labels, treating a missing path as root depth", () => {
    expect(pathDepth("a")).toBe(1);
    expect(pathDepth("a.b.c")).toBe(3);
    expect(pathDepth("")).toBe(1);
    expect(pathDepth(null)).toBe(1);
  });
});

describe("buildTree", () => {
  it("nests children under parents and sorts by slug", () => {
    const nodes: NodeMeta[] = [
      meta({ id: "1", path: "projects", slug: "projects", type: "folder" }),
      meta({ id: "3", path: "projects.zeta", slug: "zeta", parent_id: "1" }),
      meta({ id: "2", path: "projects.alpha", slug: "alpha", parent_id: "1" }),
    ];
    const roots = buildTree(nodes);
    expect(roots).toHaveLength(1);
    expect(roots[0].id).toBe("1");
    expect(roots[0].children.map((c) => c.slug)).toEqual(["alpha", "zeta"]);
  });

  it("treats nodes with absent parents as roots", () => {
    const nodes: NodeMeta[] = [meta({ id: "x", path: "x", slug: "x", parent_id: "missing" })];
    expect(buildTree(nodes).map((r) => r.id)).toEqual(["x"]);
  });
});

describe("buildRootGraph", () => {
  it("links each child to its parent as containment, leaving subtrees disconnected", () => {
    const nodes: NodeMeta[] = [
      meta({ id: "a", path: "a", slug: "a", type: "folder" }),
      meta({ id: "a1", path: "a.one", slug: "one", parent_id: "a" }),
      meta({ id: "b", path: "b", slug: "b", type: "folder" }),
      meta({ id: "b1", path: "b.one", slug: "one", parent_id: "b" }),
    ];
    const { nodes: out, links } = buildRootGraph(nodes);
    expect(out).toHaveLength(4);
    expect(links).toEqual([
      { source: "a", target: "a1", kind: "containment" },
      { source: "b", target: "b1", kind: "containment" },
    ]);
    // No link bridges the "a" and "b" subdirectories.
    expect(links.some((l) => [l.source, l.target].includes("a") && [l.source, l.target].includes("b"))).toBe(false);
  });

  it("leaves root-level files as standalone nodes with no links", () => {
    const nodes: NodeMeta[] = [
      meta({ id: "f1", path: "loose", slug: "loose" }),
      meta({ id: "f2", path: "alone", slug: "alone" }),
    ];
    expect(buildRootGraph(nodes).links).toEqual([]);
  });

  it("ignores links to parents that are not in the node set", () => {
    const nodes: NodeMeta[] = [meta({ id: "x", path: "x", slug: "x", parent_id: "missing" })];
    expect(buildRootGraph(nodes).links).toEqual([]);
  });
});

describe("buildEntityGraph", () => {
  const data: EntityGraphData = {
    nodes: [
      ent({ id: "root", path: "entities", slug: "entities", type: "folder", role: "root" }),
      ent({ id: "people", path: "entities.people", slug: "people", parent_id: "root", role: "class", entity_type: "class" }),
      ent({ id: "places", path: "entities.places", slug: "places", parent_id: "root", role: "class", entity_type: "class" }),
      ent({ id: "avery", path: "entities.instances.avery", slug: "avery", parent_id: "root", role: "instance", entity_type: "instance" }),
    ],
    links: [
      { source: "root", target: "people", kind: "containment" },
      { source: "root", target: "places", kind: "containment" },
      { source: "people", target: "avery", kind: "containment" },
    ],
  };

  it("shows the hub and classes but hides instances by default", () => {
    const { nodes, links } = buildEntityGraph(data);
    expect(nodes.map((n) => n.id).sort()).toEqual(["people", "places", "root"]);
    // classes still hang off the hub; the class->instance link is dropped with its instance
    expect(links).toEqual([
      { source: "root", target: "people", kind: "containment" },
      { source: "root", target: "places", kind: "containment" },
    ]);
  });

  it("includes instances when showInstances is set", () => {
    const { nodes, links } = buildEntityGraph(data, { showInstances: true });
    expect(nodes).toHaveLength(4);
    expect(links).toHaveLength(3);
  });

  it("keeps the shared space (hub + all classes) and adds the focused class' instances", () => {
    const { nodes, links } = buildEntityGraph(data, { focusClassId: "people" });
    // Every class and the hub stay present (drill-in hides, not removes, the rest);
    // only the focused class' instance is added.
    expect(nodes.map((n) => n.id).sort()).toEqual(["avery", "people", "places", "root"]);
    expect(links).toEqual([
      { source: "root", target: "people", kind: "containment" },
      { source: "root", target: "places", kind: "containment" },
      { source: "people", target: "avery", kind: "containment" },
    ]);
  });
});

describe("buildEntityGraph showInactive", () => {
  const data: EntityGraphData = {
    nodes: [
      ent({ id: "root", path: "entities", slug: "entities", type: "folder", role: "root" }),
      ent({ id: "act", path: "entities.act", slug: "act", parent_id: "root", role: "class", entity_type: "class", status: "active" }),
      ent({ id: "dorm", path: "entities.dorm", slug: "dorm", parent_id: "root", role: "class", entity_type: "class", status: "inactive" }),
      ent({ id: "i-act", path: "entities.instances.ia", slug: "ia", role: "instance", entity_type: "instance" }),
      ent({ id: "i-dorm", path: "entities.instances.idm", slug: "idm", role: "instance", entity_type: "instance" }),
      ent({ id: "i-free", path: "entities.instances.ifr", slug: "ifr", role: "instance", entity_type: "instance" }),
    ],
    links: [
      { source: "root", target: "act", kind: "containment" },
      { source: "root", target: "dorm", kind: "containment" },
      { source: "act", target: "i-act", kind: "containment" },
      { source: "dorm", target: "i-dorm", kind: "containment" },
    ],
  };

  it("hides inactive classes by default", () => {
    expect(buildEntityGraph(data).nodes.map((n) => n.id).sort()).toEqual(["act", "root"]);
  });

  it("reveals them with showInactive", () => {
    expect(buildEntityGraph(data, { showInactive: true }).nodes.map((n) => n.id).sort())
      .toEqual(["act", "dorm", "root"]);
  });

  it("prunes instances whose every class is hidden, keeps class-less ones", () => {
    const ids = buildEntityGraph(data, { showInstances: true }).nodes.map((n) => n.id).sort();
    expect(ids).toEqual(["act", "i-act", "i-free", "root"]); // dorm + i-dorm gone
  });

  it("keeps a drilled-in inactive class and its instances", () => {
    const ids = buildEntityGraph(data, { focusClassId: "dorm" }).nodes.map((n) => n.id).sort();
    expect(ids).toEqual(["act", "dorm", "i-dorm", "root"]); // focus exempt; act stays in the shared space
  });

  it("treats missing status as active", () => {
    const noStatus: EntityGraphData = {
      nodes: [
        ent({ id: "root", path: "entities", slug: "entities", type: "folder", role: "root" }),
        ent({ id: "c", path: "entities.c", slug: "c", parent_id: "root", role: "class", entity_type: "class" }),
      ],
      links: [{ source: "root", target: "c", kind: "containment" }],
    };
    expect(buildEntityGraph(noStatus).nodes).toHaveLength(2);
  });
});

function ent(
  over: Partial<EntityNodeMeta> & { id: string; path: string; slug: string },
): EntityNodeMeta {
  return {
    parent_id: null,
    type: "entity",
    title: null,
    tags: [],
    importance: 1,
    role: "class",
    entity_type: null,
    ...over,
  };
}

const concept = (over: Partial<Concept> & { id: string }): Concept => ({
  name: over.id,
  kind: "project",
  level: 0,
  parent_id: null,
  description: null,
  member_count: over.members?.length ?? 0,
  members: [],
  label_locked: false,
  status: "active",
  ...over,
});
const member = (id: string, kind: "memory" | "entity", score = 1.0) => ({
  node_id: id,
  member_kind: kind,
  score,
  label: id.toUpperCase(),
});

describe("buildConceptGraph", () => {
  it("dedupes shared members so they bridge concepts", () => {
    const g = buildConceptGraph([
      concept({ id: "c1", members: [member("m1", "memory", 0.9)] }),
      concept({ id: "c2", members: [member("m1", "memory", 0.4)] }),
    ]);
    const memberNodes = g.nodes.filter((n) => n.role === "member");
    expect(memberNodes).toHaveLength(1); // one node, two links -> a bridge
    expect(memberNodes[0].score).toBe(0.9); // max score wins on the shared node
    const links = g.links.filter((l) => l.target === "m1");
    expect(links.map((l) => l.source).sort()).toEqual(["c1", "c2"]);
    expect(links.map((l) => l.weight).sort()).toEqual([0.4, 0.9]);
  });

  it("links children to parents and skips members the children cover", () => {
    const g = buildConceptGraph([
      concept({
        id: "p",
        members: [member("m1", "memory"), member("m2", "memory")],
      }),
      concept({
        id: "k",
        level: 1,
        parent_id: "p",
        members: [member("m1", "memory")],
      }),
    ]);
    const pairs = g.links.map((l) => `${l.source}>${l.target}`);
    expect(pairs).toContain("p>k"); // hierarchy link
    expect(pairs).toContain("k>m1"); // the child is m1's home
    expect(pairs).toContain("p>m2"); // uncovered member stays on the parent
    expect(pairs).not.toContain("p>m1"); // no duplicate edge through the parent
  });

  it("drops parent links whose parent is not in the payload", () => {
    const g = buildConceptGraph([
      concept({ id: "orphan", level: 1, parent_id: "retired-id" }),
    ]);
    expect(g.links).toEqual([]); // no dangling link, no crash
  });

  it("shapes concept hubs and member nodes as NodeMeta-compatible objects", () => {
    const g = buildConceptGraph([
      concept({
        id: "c1",
        name: "KB project",
        kind: "project",
        description: "d",
        members: [member("e1", "entity", 0.7)],
      }),
    ]);
    const hub = g.nodes.find((n) => n.role === "concept")!;
    expect(hub.title).toBe("KB project");
    expect(hub.type).toBe("concept");
    expect(hub.conceptKind).toBe("project");
    expect(hub.memberCount).toBe(1);
    const m = g.nodes.find((n) => n.role === "member")!;
    expect(m.type).toBe("entity");
    expect(m.memberKind).toBe("entity");
    expect(m.title).toBe("E1");
  });

  it("stamps exclusion fields so stale cache values cannot survive", () => {
    const g = buildConceptGraph([
      concept({ id: "c1", members: [member("m1", "memory")] }),
    ]);
    for (const n of g.nodes) {
      expect(n.analysis_excluded).toBe(false);
      expect(n.analysis_excluded_effective).toBe(false);
    }
  });
});
