import type { EntityGraphData } from "../types";

/** The canonical class NAME to drive the /api/classes/{name} endpoints for the selected node,
 *  or undefined when the node isn't a class. Uses the entity node's `title` (the canonical
 *  category name) rather than its `slug` (a path-sanitized form where spaces become
 *  underscores), so multi-word categories like "data stores" resolve correctly. */
export function categoryNameForNode(
  entityData: EntityGraphData | null,
  nodeId: string | null,
): string | undefined {
  if (!entityData || !nodeId) return undefined;
  const n = entityData.nodes.find((x) => x.id === nodeId);
  return n && n.role === "class" ? (n.title ?? n.slug) : undefined;
}
