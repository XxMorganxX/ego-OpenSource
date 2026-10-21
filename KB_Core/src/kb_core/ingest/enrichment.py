"""Two-phase enrichment: a scheduled worker that drains 'pending' nodes off the hot path."""

import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Protocol, runtime_checkable

from kb_core.domain import Node
from kb_core.ingest.inference import ProviderUnavailable
from kb_core.knowledge_base import KnowledgeBase


@runtime_checkable
class Enricher(Protocol):
    name: str

    def enrich(self, node: Node, kb: KnowledgeBase, *,
               only: "set[str] | None" = None) -> "dict | None":
        """Expensive analysis -> derived fields (summary, embedding, …) in place.

        MUST NOT change identity or placement — only enrich an existing node.
        `only` restricts the run to the named passes (recrawl's dependency gate uses
        it to run just the pass whose dependency moved); None runs every configured
        pass.
        May return a report dict — `providers` names the model backends that served this
        node and `passes` names the LLM passes that actually executed; None = nothing to
        report. The worker merges providers into the enrichment_done log detail and uses
        executed passes to stamp only the dependency cursors the node actually saw.
        """
        ...


class EnrichmentWorker:
    """Drains claimed nodes through every enricher.

    `concurrency` > 1 processes the claimed batch on a thread pool. Nodes are independent —
    `claim_enrichment` hands out disjoint sets via `FOR UPDATE SKIP LOCKED` and flips them to
    'processing' before releasing the lock — so the only shared state is the KnowledgeBase's
    connection pool (psycopg_pool is thread-safe) and the enrichers, which hold no per-node
    state. Passes WITHIN a node stay ordered: they form a dependency chain (entity_gate must
    suppress the entity passes before they run; later user templates interpolate earlier pass
    outputs), so only the across-node loop is widened.

    Concurrency is bounded by the pool, not by this number: `open_pool` defaults to
    `max_size=4` and `pool.connection()` BLOCKS ~30s before raising `PoolTimeout`, so a caller
    raising concurrency must raise the pool with it (the ingest daemon does). It is also
    bounded by routing — a pass routed `local` goes to the single-flight inference server, so
    workers serialise there no matter how wide this is (see `inference.toml`).

    A provider outage ends the batch: after the first ProviderUnavailable (or once a provider
    in `gate_providers` is paused), every claimed node not yet started goes back to
    'pending' untouched. Otherwise an offline tenant agent would cost one full agent
    timeout per claimed node, and leave one expired llm_jobs row for each.
    """

    def __init__(self, kb: KnowledgeBase, enrichers: "list[Enricher]", *,
                 concurrency: int = 1, gate_providers: "tuple[str, ...]" = ()) -> None:
        self._kb = kb
        self._enrichers = enrichers
        self._concurrency = max(1, int(concurrency))
        self._gate = tuple(gate_providers)
        self._warned = False

    def _warn_once(self, what: str, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            print(f"[enrichment] {what} failed, proceeding as not paused: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    def _held(self, outage: threading.Event) -> bool:
        """True once this batch hit an outage, or while a gated provider is paused. Fails
        OPEN: the pause read is advisory, and a transient DB error here must not strand
        the claimed batch at 'processing' (only a daemon boot requeues those)."""
        if outage.is_set():
            return True
        try:
            return any(self._kb.provider_paused(p) is not None for p in self._gate)
        except Exception as exc:
            self._warn_once("pause check", exc)
            return False

    def _process(self, node: Node, *, class_hash: str, kind_hash: str,
                 outage: "threading.Event | None" = None) -> bool:
        """One node through every enricher. Never raises: a failure is recorded against that
        node alone so one bad memory can't abort the batch (or, in the pool, its siblings).
        Returns True if the node was tried, False if it was handed back untouched."""
        outage = outage if outage is not None else threading.Event()
        if self._held(outage):
            try:
                self._kb.release_enrichment_claim(node.id)
                return False
            except Exception as exc:
                # Fail open: run the node as before rather than leave it at 'processing'.
                self._warn_once("pause check (hand-back)", exc)
        try:
            providers: "set[str]" = set()
            passes: "set[str]" = set()
            for enricher in self._enrichers:
                report = enricher.enrich(node, self._kb)
                if isinstance(report, dict) and isinstance(report.get("providers"), list):
                    providers.update(p for p in report["providers"]
                                     if isinstance(p, str))
                if isinstance(report, dict) and isinstance(report.get("passes"), list):
                    passes.update(p for p in report["passes"] if isinstance(p, str))
            # also logs enrichment_done (ingest_log), carrying who served it
            self._kb.mark_enriched(
                node.id,
                class_hash=class_hash if "extract_entities" in passes else None,
                kind_hash=kind_hash if "doc_kind" in passes else None,
                gate="entity_gate" in passes,
                detail={"providers": sorted(providers)} if providers else None)
        except ProviderUnavailable as exc:
            # The PROVIDER is down (quota window, auth outage), not the document.
            # mark_enrichment_failed would pin this node terminal; instead hand it
            # back to the queue and pause the lane so the rest of the backlog isn't
            # burned through a dead provider (over a hundred nodes in minutes, once).
            self._kb.defer_enrichment(node.id, provider=exc.provider,
                                      until=exc.until, reason=f"{type(exc).__name__}: {exc}")
            outage.set()                    # the rest of this batch goes back untouched
        except Exception as exc:
            # Capture the reason so the failure is explainable in the ingest log,
            # not just a bare 'failed' status (also logs enrichment_failed).
            self._kb.mark_enrichment_failed(node.id, error=f"{type(exc).__name__}: {exc}")
        return True

    def run(self, *, limit: int = 10) -> int:
        # Capture the vocabularies BEFORE the passes run: those are the inputs the resulting
        # answers actually saw. Stamping a post-run vocabulary could swallow a vocabulary
        # change caused by the run itself (for example a doc-kind crossing support threshold).
        class_hash = self._kb.active_class_hash()
        kind_hash = self._kb.doc_kind_vocab_hash()
        nodes = self._kb.claim_enrichment(limit)
        process = partial(self._process, class_hash=class_hash, kind_hash=kind_hash,
                          outage=threading.Event())
        # Returns the nodes actually TRIED: ones handed back after an outage don't count.
        if self._concurrency == 1 or len(nodes) < 2:
            # Serial path kept exact: the default must not change behaviour or ordering.
            return sum(1 for node in nodes if process(node))
        with ThreadPoolExecutor(max_workers=min(self._concurrency, len(nodes)),
                                thread_name_prefix="enrich") as pool:
            # sum() forces completion before returning, so the caller's count is honest and
            # the next scheduler tick can't overlap this batch with its own claim.
            return sum(1 for tried in pool.map(process, nodes) if tried)
