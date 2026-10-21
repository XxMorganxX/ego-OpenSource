"""The re-analysis worker: re-run the LLM analysis over nodes whose inputs have moved.

Decoupled from ingestion in state too: it reads a CRAWLABLE view (due_for_reanalysis),
runs the enrichers, then advances only the re-analysis cursors (mark_analyzed). It never
touches enrichment_status — that belongs to the ingest pipeline. No claim/lock, so there is
no in-flight state to get stuck; an interrupted node is simply still crawlable next time.

Each pass is run only when the one input that can change its answer has moved — see
RECRAWL_PASS_FOR_REASON. A node's own content is not one of those inputs: a content change
is the enrichment lane's job (enriched_hash), not this crawl's.
"""

import traceback

from kb_core.ingest.inference import ProviderUnavailable
from kb_core.knowledge_base import KnowledgeBase

# Which recrawl pass each due-reason can change the answer of. extract_entities can only
# assign ACTIVE types, so the active class vocabulary is its sole moving input; doc_kind is
# steered by the in-use kind list; entity_gate judges staleness against today, so it rides a
# clock instead of a vocabulary.
RECRAWL_PASS_FOR_REASON = {
    "due_gate": "entity_gate",
    "due_class": "extract_entities",
    "due_kind": "doc_kind",
}


class ReanalysisWorker:
    def __init__(self, kb: KnowledgeBase, enrichers: list, sources: "list[str]", *,
                 batch: int = 10, gate_providers: "tuple[str, ...]" = ()) -> None:
        self._kb = kb
        self._enrichers = list(enrichers)
        self._sources = list(sources)
        self._batch = batch
        # Remote providers the recrawl passes ride (the daemon passes them from routing).
        # A provider that raises ProviderUnavailable joins the set, so the gate holds even
        # for one nobody configured.
        self._gate = set(gate_providers)
        self.last_changed = 0

    def _paused(self) -> bool:
        """True (and logged) while any gated provider is paused: the same all-or-nothing
        gate the ingest enrich lane uses, over the same shared provider_pause rows."""
        for prov in sorted(self._gate):
            until = self._kb.provider_paused(prov)
            if until is not None:
                print(f"[reanalyzed] paused: {prov} until {until:%Y-%m-%d %H:%M%z}",
                      flush=True)
                return True
        return False

    @staticmethod
    def _passes_for(reasons) -> "set[str]":
        """The union of passes the due-reasons call for."""
        return {name for reason, name in RECRAWL_PASS_FOR_REASON.items()
                if getattr(reasons, reason)}

    def _process(self, items: list) -> tuple:
        """Run each (node, passes) pair. Returns (processed, changed).

        A ProviderUnavailable stops the batch: the node is left exactly as it was (no
        cursor, no clock), so it is still due once the provider is back, and the provider
        is paused so later ticks skip instead of retrying into the outage."""
        changed = 0
        processed = 0
        for node, passes, cursors in items:
            before = self._kb.recrawl_fingerprint(node.id)
            ok = True
            reported_passes: "set[str] | None" = None
            try:
                for enricher in self._enrichers:
                    report = enricher.enrich(node, self._kb, only=passes)
                    if isinstance(report, dict) and isinstance(report.get("passes"), list):
                        if reported_passes is None:
                            reported_passes = set()
                        reported_passes.update(
                            p for p in report["passes"] if isinstance(p, str))
            except ProviderUnavailable as exc:
                self._kb.pause_provider(exc.provider, until=exc.until,
                                        reason=f"{type(exc).__name__}: {exc}")
                self._gate.add(exc.provider)
                print(f"[reanalyzed] {exc.provider} unavailable: {exc}; "
                      f"{node.id} left due, batch stopped", flush=True)
                return processed, changed
            except Exception as exc:
                # One node's failure must not abort the batch — but it must be SEEN:
                # a bare swallow here turns any systemic breakage (dead CLI, schema
                # drift, missing column) into an indistinguishable silent no-op.
                print(f"[reanalyzed] analysis failed for {node.id}: {exc}", flush=True)
                traceback.print_exc()
                ok = False
            if ok and self._kb.recrawl_fingerprint(node.id) != before:
                changed += 1
            if ok:
                completion = dict(cursors)
                if reported_passes is not None:
                    completion = {}
                    for cursor, attempted, pass_name in (
                            ("class_hash", "attempted_class_hash", "extract_entities"),
                            ("kind_hash", "attempted_kind_hash", "doc_kind")):
                        if cursor in cursors:
                            target = cursor if pass_name in reported_passes else attempted
                            completion[target] = cursors[cursor]
                    if cursors.get("gate") and "entity_gate" in reported_passes:
                        completion["gate"] = True
                self._kb.mark_analyzed(node.id, **completion)
            else:
                # Record which dependency values failed without claiming convergence. This
                # prevents an identical poisoned run every tick; a moved hash makes it due.
                self._kb.mark_analysis_failed(node.id, **cursors)
            processed += 1
        return processed, changed

    def run(self) -> int:
        """One recurring tick: re-analyze the nodes whose dependencies moved."""
        if self._paused():
            self.last_changed = 0
            return 0
        try:
            class_hash = self._kb.active_class_hash()
            kind_hash = self._kb.doc_kind_vocab_hash()
        except Exception as exc:
            print(f"[reanalyzed] dependency hash failed; tick skipped: {exc}", flush=True)
            traceback.print_exc()
            self.last_changed = 0
            return 0
        due = self._kb.due_for_reanalysis(self._sources, class_hash=class_hash,
                                          kind_hash=kind_hash, limit=self._batch)
        items = []
        for node, reasons in due:
            cursors = {}
            if reasons.due_class:
                cursors["class_hash"] = class_hash
            if reasons.due_kind:
                cursors["kind_hash"] = kind_hash
            if reasons.due_gate:
                cursors["gate"] = True
            items.append((node, self._passes_for(reasons), cursors))
        processed, changed = self._process(items)
        self.last_changed = changed
        return processed

    def run_node(self, node_id) -> int:
        """Re-analyze one specific node now — the manual, out-of-band trigger. Honors the
        same crawlable gate as the periodic pass (first-pass-enriched, not retired, a covered
        source) but ignores every dependency cursor: the user asked for this one explicitly,
        so it runs the full recrawl pipeline. Returns 1 if processed, 0 if not crawlable."""
        if self._paused():
            return 0
        node = self._kb.crawlable_node(node_id, self._sources)
        if node is None:
            return 0
        cursors = {"class_hash": self._kb.active_class_hash(),
                   "kind_hash": self._kb.doc_kind_vocab_hash(),
                   "gate": True}
        processed, _ = self._process([(node, None, cursors)])
        return processed

    def backfill(self, before, *, on_progress=None) -> int:
        """One-time: analyze EVERY existing node (as of `before`) once, no dependency gate.
        Loops in batches until the corpus is drained; returns the total analyzed."""
        total = 0
        while True:
            # Checked every batch: an outage leaves its node un-analyzed, so without
            # this the same batch would be re-selected forever.
            if self._paused():
                return total
            nodes = self._kb.due_for_backfill(self._sources, before=before,
                                              limit=self._batch)
            if not nodes:
                return total
            cursors = {"class_hash": self._kb.active_class_hash(),
                       "kind_hash": self._kb.doc_kind_vocab_hash(),
                       "gate": True}
            processed, _ = self._process([(n, None, cursors) for n in nodes])
            total += processed
            if on_progress is not None:
                on_progress(total)
