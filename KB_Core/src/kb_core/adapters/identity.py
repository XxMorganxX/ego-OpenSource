"""Central identity strategies: how an adapter assigns a stable `external_id`.

Node identity is already centralized downstream — `node_id(source, external_id)`
(see kb_core.identity) deterministically maps a source + external_id to a node UUID,
and `KnowledgeBase.upsert` keys its `ON CONFLICT` on that UUID. So the *only* identity
decision an adapter makes is **what external_id a record carries**. This module is the
one place that decision lives, so every source picks from the same vetted menu instead
of reinventing it inline.

Three families, by how much an item's identity should survive:

  * PathIdentity        — external_id = the source-relative path. Deterministic and
                          churn-resistant for a stable layout, but a move/rename mints a
                          new identity (the old node is pruned, a new one created). Fine
                          for sources that rarely reorganize.

  * FrontmatterIdentity — prefer a committed, content-embedded id (survives moves);
                          fall back to PathIdentity. The Obsidian default.

  * GitVersionedIdentity — (built on top of these, in kb_core.adapters.git) for sources
                          pulled from a Git repo: mint a UUID per logical file once and
                          carry it forward across renames/moves/edits using Git rename
                          detection + content similarity, so identity is fully decoupled
                          from the current path. See the git package.

Identity (this module, → external_id) is distinct from *persistence* (the `Ingestor`
seam in adapters.base, → how/whether a record is written). A source that needs no
upsert at all overrides the latter, not the former.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Observation:
    """One item seen in a source, as the input to identity resolution.

    Carries every signal a strategy might key on; each strategy reads only what it needs,
    so a simple source can leave the richer fields unset.
    """
    rel_path: str                       # source-relative path (vault-relative for obsidian)
    frontmatter_id: str | None = None   # a committed, content-embedded id, if the item has one
    content: str | None = None          # normalized body, for content-similarity strategies
    blob_sha: str | None = None         # git blob sha, when the source is a git tree


@runtime_checkable
class IdentityStrategy(Protocol):
    """Maps an Observation to the stable `external_id` its record should carry.

    Contract: deterministic for a given source state — re-resolving the same item yields
    the same external_id, so re-syncs upsert in place instead of duplicating.
    """

    def external_id(self, obs: Observation) -> str:
        ...


class PathIdentity:
    """external_id = the source-relative path. Simplest; identity churns on move/rename."""

    def external_id(self, obs: Observation) -> str:
        return obs.rel_path


class FrontmatterIdentity:
    """Prefer a committed frontmatter id (move-proof); else fall back to the path.

    A note that opts into `id:` is immune to reorganization for free; one that doesn't
    still gets a deterministic, churn-resistant path id. The two are composed rather than
    duplicated so the fallback stays identical to PathIdentity.
    """

    def __init__(self, fallback: IdentityStrategy | None = None) -> None:
        self._fallback = fallback or PathIdentity()

    def external_id(self, obs: Observation) -> str:
        return obs.frontmatter_id or self._fallback.external_id(obs)
