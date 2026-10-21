import type { GraphLink } from "../types";

/** A node's soft layout target in the radial memories layout. */
export type RadialTarget = {
  /** Rings out from the center; 0 only for a single drilled-in root. */
  depth: number;
  /** Target distance from the origin: depth × ringGap. */
  r: number;
  /** Target angle in radians: the midpoint of the node's angular wedge, in [0, 2π). */
  theta: number;
};

const TWO_PI = Math.PI * 2;

/**
 * Computes each node's radial layout target from the in-view CONTAINMENT links (never
 * absolute paths, so the root view and a drilled-in subtree behave identically).
 *
 * Forest: roots are nodes with no in-view parent; "edge" links and links to absent nodes
 * are ignored. Exactly one root (a drilled-in folder) sits at the center (depth 0) and its
 * children split the full circle; with multiple roots (the root view) the roots share ring 1
 * and the circle splits among them. Wedges subdivide recursively, proportional to subtree
 * node count, children in slug order (matching buildTree) so allocation is deterministic
 * across refetches. A node's theta is its own wedge midpoint; r = depth × ringGap.
 */
export function assignRadialTargets(
  nodes: { id: string; slug?: string }[],
  links: GraphLink[],
  ringGap: number,
): Map<string, RadialTarget> {
  const targets = new Map<string, RadialTarget>();
  if (nodes.length === 0) return targets;

  const byId = new Map(nodes.map((node) => [node.id, node]));
  const children = new Map<string, string[]>();
  const hasParent = new Set<string>();
  for (const l of links) {
    if (l.kind !== "containment") continue;
    if (!byId.has(l.source) || !byId.has(l.target)) continue;
    if (hasParent.has(l.target)) continue; // containment is a tree; first parent wins
    hasParent.add(l.target);
    let kids = children.get(l.source);
    if (!kids) children.set(l.source, (kids = []));
    kids.push(l.target);
  }

  const bySlug = (a: string, b: string) => {
    const sa = byId.get(a)?.slug ?? a;
    const sb = byId.get(b)?.slug ?? b;
    return sa.localeCompare(sb) || a.localeCompare(b);
  };
  const roots = nodes.filter((node) => !hasParent.has(node.id)).map((node) => node.id);
  roots.sort(bySlug);
  for (const kids of children.values()) kids.sort(bySlug);

  // Subtree sizes (self + descendants). The seen-guard makes corrupt (cyclic) data degrade
  // to a partial layout instead of recursing forever.
  const size = new Map<string, number>();
  const seen = new Set<string>();
  const computeSize = (id: string): number => {
    if (seen.has(id)) return 0;
    seen.add(id);
    let s = 1;
    for (const c of children.get(id) ?? []) s += computeSize(c);
    size.set(id, s);
    return s;
  };
  for (const r of roots) computeSize(r);

  const allocate = (id: string, depth: number, a0: number, a1: number) => {
    targets.set(id, { depth, r: depth * ringGap, theta: (a0 + a1) / 2 });
    const kids = children.get(id) ?? [];
    const total = kids.reduce((s, k) => s + (size.get(k) ?? 1), 0);
    if (total === 0) return;
    let a = a0;
    for (const k of kids) {
      const span = ((a1 - a0) * (size.get(k) ?? 1)) / total;
      allocate(k, depth + 1, a, a + span);
      a += span;
    }
  };

  if (roots.length === 1) {
    allocate(roots[0], 0, 0, TWO_PI); // drilled-in view: the root holds the center
  } else {
    const total = roots.reduce((s, r) => s + (size.get(r) ?? 1), 0);
    let a = 0;
    for (const r of roots) {
      const span = (TWO_PI * (size.get(r) ?? 1)) / Math.max(1, total);
      allocate(r, 1, a, a + span);
      a += span;
    }
  }
  return targets;
}

/**
 * Tiny deterministic per-id offset (each axis in [-2, 2]) applied when seeding a node at
 * its radial target, so an ancestor chain sharing a wedge midpoint doesn't stack exactly
 * (the collide force needs a direction to push along) and perfect symmetry can't trap the
 * simulation. Deterministic so live refetches re-seed identically.
 */
export function idJitter(id: string): { dx: number; dy: number } {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return {
    dx: ((h & 0xff) / 255 - 0.5) * 4,
    dy: (((h >> 8) & 0xff) / 255 - 0.5) * 4,
  };
}
