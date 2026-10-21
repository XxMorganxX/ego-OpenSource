"""ObsidianAdapter: walk a markdown vault, read a stable id, yield NodeRecords.

Identity is churn-resistant:
  external_id = the note's frontmatter `id:` if it has one (so identity survives moves),
                otherwise, with stamping on, the id the stamper would write for its path
                (`stamp.mint_id`) — so a note whose stamp line is missing or was stripped
                resolves to the SAME identity as when it is present,
                otherwise the note's vault-relative path (stable unless moved/renamed).
fetch() never writes to the vault; the opt-in prepare() stamp pass is the only writer, and
its id is a pure function of the path, so an external sync (iCloud / Obsidian Sync / Dropbox)
or a mirror rewrite that reverts the stamp cannot churn identities (prune + recreate). The
fallback is deterministic, so even a note without a frontmatter id keeps the same id every sync.
"""

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from kb_core.adapters.identity import FrontmatterIdentity, IdentityStrategy, Observation
from kb_core.adapters.obsidian.parser import (
    derive_event_at, derive_tags, derive_title, parse_note, placement_for,
)
from kb_core.adapters.obsidian.stamp import current_id, mint_id, stamp_file
from kb_core.domain import NodeRecord
from kb_core.identity import node_id
from kb_core.paths import join_path, sanitize_slug


class ObsidianAdapter:
    source = "obsidian"

    def __init__(self, vault_path: str | Path,
                 identity: IdentityStrategy | None = None,
                 root_label: str | None = None,
                 stamp: bool = False) -> None:
        self._vault = Path(vault_path)
        # Opt-in: write a stable frontmatter `id:` into notes that lack one (see prepare()).
        # Off by default keeps the adapter's read-only guarantee for callers that want it.
        self._stamp = stamp
        # Identity is delegated to a central strategy (kb_core.adapters.identity); the
        # default reproduces the historical behavior — a frontmatter `id:` wins, else the
        # vault-relative path. A Git-backed source swaps in GitVersionedIdentity instead.
        self._identity = identity or FrontmatterIdentity()
        # An optional source layer: every record is placed under this top-level label
        # (e.g. "Obsidian"), so the whole vault hangs off one node instead of dumping its
        # folders at the tree root. Decoupled from identity, so adding it never churns ids.
        self._root_label = sanitize_slug(root_label) if root_label else None
        self.errors: list[str] = []

    def fetch(self) -> Iterator[NodeRecord]:
        if not self._vault.is_dir():
            raise FileNotFoundError(f"vault path is not a directory: {self._vault}")
        self.errors = []
        for path in self._iter_markdown():
            record = self._to_record(path)
            if record is not None:
                yield record

    def prepare(self, kb) -> None:
        """Before fetch: ensure every note carries a stable frontmatter id (opt-in, stamp=True).

        Writes a deterministic id into any unstamped `.md`, and — for a note already ingested
        under its old path-based identity — re-keys its node to the new id in place, so turning
        stamping on never churns an existing note (its enrichment, links and edges follow it).
        Best-effort per file; a malformed note is left untouched for fetch to report. No-op when
        stamping is disabled, so SyncService can call it unconditionally.

        Resets `self.errors` FIRST, before the stamp=False no-op check -- mirroring fetch()'s
        own reset-then-append contract. `SyncService.sync` captures `adapter.errors` right
        after this call (it must, before fetch() resets the list again and wipes prepare()'s
        own errors — see sync.py's comment). Without this reset here too, an adapter that is
        REUSED across multiple sync() calls -- which is exactly what KB_MCP's build_server does,
        holding one Adapter per source for the life of the process -- leaks the PREVIOUS sync's
        fetch()-time errors into this call's prepare-time snapshot, and SyncService double-counts
        them (once from that stale snapshot, again from the post-fetch one). Reproduced with two
        sequential (non-concurrent) `SyncService.sync()` calls on one adapter over a
        persistently-bad vault: call 1 correctly reports N errors, call 2 -- with no fix here --
        reports 2N, no threading involved.
        """
        self.errors = []
        if not self._stamp:
            return
        rekeys: list[tuple[object, str]] = []
        for path in self._iter_markdown():
            try:
                rel = path.relative_to(self._vault).as_posix()
                if current_id(path.read_text(encoding="utf-8")) is not None:
                    continue                                  # already carries an id
                new_id = stamp_file(path, rel)
                old_node = node_id(self.source, rel)          # its pre-stamp, path-based id
                if kb.get(old_node) is not None:
                    rekeys.append((old_node, new_id))         # backfill: re-key in place
            except Exception as exc:                          # noqa: BLE001 — report and skip
                self.errors.append(f"{path}: stamp skipped ({exc})")
        if rekeys:
            kb.reidentify(rekeys)

    def _iter_markdown(self) -> Iterator[Path]:
        # os.walk swallows scandir failures by default (onerror=None), so a directory it
        # cannot list yields nothing and reports nothing -- indistinguishable from the user
        # emptying that directory, which would let a permissions outage drive a
        # mirror-prune. Record it instead: SyncService treats any fetch-time
        # error as "this fetch did not observe the whole vault" and suppresses the prune.
        def _onerror(exc: OSError) -> None:
            self.errors.append(f"{getattr(exc, 'filename', self._vault)}: "
                               f"directory not listed ({exc})")

        # Symlinks are never followed (as in kb_agent's scanner): a linked note can live
        # outside the vault, and stamping would then write an `id:` into that file.
        for root, dirs, files in os.walk(self._vault, onerror=_onerror):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.endswith(".md") and not name.startswith("."):
                    path = Path(root) / name
                    if not os.path.islink(path):
                        yield path

    def _to_record(self, path: Path) -> NodeRecord | None:
        # Any per-file failure is reported and skipped — it must never crash the walk.
        try:
            rel = path.relative_to(self._vault).as_posix()
            parent_path, slug = placement_for(rel)              # may raise on an unsanitizable name
            if self._root_label:
                # A root-level note (empty parent_path) lands directly under the layer;
                # join_path would otherwise leave a trailing-dot, empty-label ltree path.
                parent_path = (join_path(self._root_label, parent_path)
                               if parent_path else self._root_label)
            metadata, body = parse_note(path.read_text(encoding="utf-8"))  # may raise on bad YAML
            # The stable external_id is the identity strategy's call, not the adapter's:
            # the default prefers a committed frontmatter id (survives moves) over the path.
            frontmatter_id = str(metadata["id"]) if metadata.get("id") else None
            if frontmatter_id is None and self._stamp:
                # Stamping is on but this read found no id: the note is new to prepare(), or a
                # rewrite stripped its stamp between prepare() and here (a tool that regenerates
                # the note from another source, or an external sync, can revert one).
                # Either way its identity is the one the stamper writes. Resolving to the raw
                # path instead made the note collide with its own node (PathConflict) and left
                # that node unseen, so the mirror-prune deleted it and the next tick recreated
                # it — losing enrichment, embedding, edges and access counts.
                # prepare() will restore the line; identity must not wait for it.
                frontmatter_id = mint_id(rel)
            external_id = self._identity.external_id(Observation(
                rel_path=rel,
                frontmatter_id=frontmatter_id,
                content=body,
            ))
            title = derive_title(metadata, body, path.stem)
            content = body.strip() or title  # title always falls back to the stem -> non-empty
            st = path.stat()
            return NodeRecord(
                source=self.source, external_id=external_id, type="note",
                content=content, slug=slug, parent_path=parent_path, title=title,
                tags=derive_tags(metadata), uri=path.as_uri(),
                source_created_at=datetime.fromtimestamp(st.st_ctime, tz=timezone.utc),
                source_updated_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                event_at=derive_event_at(metadata, path.stem),
            )
        except Exception as exc:
            self.errors.append(f"{path}: skipped ({exc})")
            return None
