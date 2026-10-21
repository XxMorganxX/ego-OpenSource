# Founding Principles — ego-OS

These are the durable convictions behind the knowledge base. They change rarely.
Every design decision and code change, by a human or an agent, must respect them.
When a principle and a convenience conflict, the principle wins — or the principle
gets explicitly revised here first.

1. **One universal contract.**
   Every memory is a *node* obeying the same core schema. No data source writes to
   storage in its own shape — ever.

2. **Adapters translate; the core stays pure.**
   Each source has an adapter that converts its native structure into the contract
   (core fields + an optional typed extension). Adapters never touch the database,
   never compute ids, and never make retrieval decisions.

3. **Deterministic identity, independent of location.**
   `id = uuid_v5(namespace, f"{source}:{external_id}")`. Re-ingesting the same item
   upserts the same node — never a duplicate. Identity is independent of where the
   memory sits in the tree: **moving a memory changes its path, never its id.**
   Adapters must supply a stable `external_id`.

4. **The MCP layer is thin.**
   The server is a transport / tool surface only. All logic lives in a reusable
   `KnowledgeBase` core service, so MCP is just one client among possible others
   (CLI, web, background jobs).

5. **Retrieval-agnostic schema.**
   A node carries searchable text and an `embedding` (`nodes.embedding vector(1024)`,
   produced by a local `qwen3-embedding:0.6b` model). Retrieval grew literal →
   semantic → hybrid without changing the contract, and stays composable with a
   tree-path scope.

6. **Structured over free-form.**
   Typed, per-type extension tables — not a catch-all JSON blob. Extensions are
   optional per type, but when present they are explicit and queryable.

7. **Tree for containment, edges for relationships.**
   The knowledge base is two structures over one set of nodes:
   - a **tree** that gives every memory exactly **one canonical home** (its
     hierarchical path) — this is organization and containment;
   - a **graph** of typed **edges** that connect any two memories regardless of where
     they sit in the tree — this is relationship.

   A memory is never filed in two places. "It also relates to that" is an **edge**,
   not a second home. Containment lives in the path; relationships live in edges.

8. **Path-addressed, traversal-fast.**
   Memories are stored and primarily retrieved by hierarchical path. The path is a
   **materialized `ltree`** with a GiST index, so subtree, ancestor, and
   branch-scoped queries are indexed and recursion-free. Retrieval is: **locate by
   path, then expand across branches by edges.**

9. **Symmetric writers.**
   Agents, adapters, and humans are all first-class writers. Postgres handles the
   concurrency; the contract and upsert path treat every writer the same.

10. **Every increment ships.**
    Each step produces independently useful, testable software. Nodes + tree +
    literal search came first; edges, semantic retrieval, and visualization were each
    delivered separately on top.

11. **Docs are living.**
    These principles and the design docs are Markdown, versioned alongside the code
    and kept in sync with it.
