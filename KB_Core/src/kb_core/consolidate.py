"""One concept-consolidation pass: build the memory<->entity mention graph plus
embedding-kNN edges, detect Leiden communities, label each, and replace the derived
concept layer. Pure orchestration over KnowledgeBase — no I/O of its own (mirrors
entity_resolve.py).

Concepts are derived and disposable: every pass recomputes the set, carries ids for
communities that overlap a previous concept (>= match_overlap Jaccard on memory members),
and retires the rest. The graph decides membership; the LLM only names/describes or
discards. Labelling is fail-open for a bad reply (a carried label, or a placeholder from the
strongest entity names), so a garbled answer degrades naming, never structure. A provider
OUTAGE (ProviderUnavailable) is not fail-open: it aborts the pass before any concept is
written, pauses the provider and propagates (see run_once)."""
from __future__ import annotations

import time
from contextlib import contextmanager

from kb_core.concept_judge import (CONCEPT_SCHEMA, SYSTEM_FILE as CONCEPT_SYSTEM_FILE,
                                   build_concept_prompt, fallback_label,
                                   is_fallback_name, parse_concept)
from kb_core.concepts import (ENTITY, MEMORY, attach_entities, build_graph,
                              detect_communities, enforce_constraints,
                              hub_capped_entities, match_concepts, resolve_separations)
from kb_core.ingest.inference import ProviderUnavailable


def build_consolidation_clients(env, *, pool=None) -> tuple[object, object]:
    """Concept-label completion + local embeddings, resolved through inference.toml
    ([task.concept_label] / [task.embed]). The name survives as the seam the
    consolidate daemon/CLI tests monkeypatch; the KB_CONCEPT_LABEL_* env vars it
    used to read are retired (the router warns when they are still set). `pool` is
    needed only when concept_label routes to `agent` (tenant daemons)."""
    from kb_core.inference_routing import InferenceRouter

    router = InferenceRouter(env, pool=pool)
    return router.client_for("concept_label"), router.embedder()


def should_consolidate(signal: dict, *, quiet_s: float,
                       backstop_s: float) -> "tuple[bool, str]":
    """Decide whether a consolidation pass should fire, from one
    KnowledgeBase.consolidation_signal() probe. Two paths:

      churn    — inputs changed since the last completed run, writes have been
                 quiet for quiet_s, and the enrichment backlog has drained. Also
                 serves a virgin DB (no completed run yet), so a fresh deployment
                 still gates on quiet+drain instead of firing on start.
      backstop — a completed run exists and is older than backstop_s: fire
                 unconditionally (max-staleness guarantee; covers a permanently
                 stuck 'processing' backlog). Deliberately NOT taken when no run
                 has ever completed — that would reintroduce the
                 fire-on-start-mid-drain bug this trigger exists to kill.

    All timestamps are DB-clock (`db_now` from the same probe); the host clock is
    never consulted. Returns (fire, reason); reason feeds the daemon's
    transition-only logging."""
    now, last_change = signal["db_now"], signal["last_change"]
    last_run = signal["last_run_finished"]
    if last_run is not None and (now - last_run).total_seconds() >= backstop_s:
        return True, "backstop"
    if last_change is None:
        return False, "no-content"
    if last_run is not None and last_change <= last_run:
        return False, "no-change"
    if (now - last_change).total_seconds() < quiet_s:
        return False, "not-quiet"
    if signal["backlog"]:
        return False, "backlog"
    return True, "churn"


def run_once(kb, client, *, embedding_client=None,
             knn_k: int = 8, knn_min_sim: float = 0.55,
             resolution: float = 1.0, min_memories: int = 3, split_cohesion: float = 0.5,
             match_overlap: float = 0.5, entity_share: float = 0.25,
             hub_share: "float | None" = 0.10,
             min_coherence_lift: "float | None" = None,
             entity_alpha: float = 1.0,
             trigger_reason: str = "churn",
             apply: bool = True) -> dict:
    """One consolidation pass. `apply=False` previews and writes nothing at all — no
    audit row, no lock. An applying pass opens its audit row FIRST (so the pass is
    observable while it runs) under an advisory lock (so a daemon pass and a
    `kb-consolidate --apply` pass cannot interleave).

    The concept labeler's system prompt is resolved ONCE here, before any DB write —
    a broken overlay (missing file, bad/cyclic include) is a configuration error and
    must fail loud before an audit row or advisory lock is ever created, never mid-pass
    per community (any PolicyError aborts before partial work, it is never
    swallowed by the per-community fail-open path).

    A ProviderUnavailable from the labeler is NOT fail-open: it aborts the pass before
    any concept is written (labelling precedes apply_concept_run; an applying run's row
    is marked 'failed'), pauses the provider (provider_pause, shared with every daemon
    on this database) and re-raises for the caller to wait the pause out. A dead
    provider would otherwise cost one full timeout per community and persist fallback
    names for the whole layer."""
    params = {"knn_k": knn_k, "knn_min_sim": knn_min_sim, "resolution": resolution,
              "min_memories": min_memories, "split_cohesion": split_cohesion,
              "match_overlap": match_overlap, "entity_share": entity_share,
              "hub_share": hub_share, "min_coherence_lift": min_coherence_lift,
              "entity_alpha": entity_alpha}
    system = kb.prompt(CONCEPT_SYSTEM_FILE)
    if not apply:
        with _pause_on_outage(kb):
            return _pass(kb, client, embedding_client, params, None, system=system)
    with kb.consolidation_lock():
        run_id = kb.begin_concept_run(params, trigger_reason=trigger_reason)
        # `_pass` fills this in as it goes (right after it builds its own `stats` dict,
        # which is otherwise out of scope here). On a crash we store whatever got that
        # far plus the exception text: a failed row that stores nothing is
        # undiagnosable -- there is no way to tell a graph-build crash from a labeling
        # crash from an apply crash after the fact, and (unlike a successful run) there
        # is no returned dict for a caller to read the reason from either.
        progress: dict = {}
        try:
            with _pause_on_outage(kb):
                return _pass(kb, client, embedding_client, params, run_id, progress,
                             system=system)
        except Exception as exc:
            # Never leave 'running' behind: a stuck row pins the dashboard on
            # "Consolidating…" and blocks the daemon's startup orphan sweep from
            # telling a dead pass from a live one.
            kb.finish_concept_run(run_id, status="failed",
                                  stats={**progress, "error": str(exc)})
            raise


@contextmanager
def _pause_on_outage(kb):
    """Record a labeler ProviderUnavailable as a provider pause, then let it propagate."""
    try:
        yield
    except ProviderUnavailable as exc:
        kb.pause_provider(exc.provider, until=exc.until,
                          reason=f"{type(exc).__name__}: {exc}")
        raise


def _pass(kb, client, embedding_client, params: dict, run_id: "int | None",
         progress: "dict | None" = None, *, system: "str | None" = None) -> dict:
    """The pass itself. `run_id is None` means preview: nothing is written and no phase
    is stamped. `progress`, when given, is updated with the run's `stats` as soon as
    they exist so a caller watching from outside (namely `run_once`'s except clause)
    can see how far a pass got even though it never returned. `system` is the concept
    labeler's system prompt, resolved once by `run_once` before any DB write — never
    re-resolved here, so a per-community `_label` call never touches `kb.prompt`."""
    def phase(name: str) -> None:
        if run_id is not None:
            kb.set_concept_run_phase(run_id, name)

    phase("graph")
    mention = kb.concept_mention_edges()
    # Which entities are clustering stopwords. With `min_coherence_lift` set we judge
    # them on measured coherence — whether an entity's memories are more alike than two
    # drawn at random — instead of on raw frequency, which was measured cutting the most
    # informative entity in the corpus while keeping the least (see hub_capped_entities).
    # The set is computed ONCE and handed to both build_graph (which excludes them) and
    # attach_entities (which re-admits them to rosters afterwards).
    hub_share = params["hub_share"]
    min_coherence_lift = params["min_coherence_lift"]
    if min_coherence_lift is not None:
        judged = kb.entity_coherence()
        hubs = hub_capped_entities(mention, hub_share, coherence=judged["coherence"],
                                   baseline=judged["baseline"],
                                   min_lift=min_coherence_lift)
    else:
        hubs = hub_capped_entities(mention, hub_share)
    hub_dropped = len(hubs)
    # memory_knn_edges is O(n^2) -- every eligible memory probes every other, and its
    # LATERAL scans a CTE, so no ANN index can serve it: well under a second at a few
    # hundred vectors, tens of seconds projected at a few thousand. Timed separately because it is a single step
    # inside a pass whose total duration the concept_runs row now covers.
    _knn_t0 = time.perf_counter()
    knn_edges = kb.memory_knn_edges(k=params["knn_k"], min_sim=params["knn_min_sim"])
    knn_ms = round((time.perf_counter() - _knn_t0) * 1000, 1)
    graph = build_graph(mention, knn_edges, hub_share=hub_share, hubs=hubs,
                        entity_alpha=params["entity_alpha"])
    comms = detect_communities(graph, resolution=params["resolution"],
                               min_memories=params["min_memories"],
                               split_cohesion=params["split_cohesion"])
    # Concept-pair separations compile to node-anchored cannot_link rows here, so the
    # note flow (labeler grounding, below) and the split machinery treat them alike.
    constraints = kb.concept_constraints()
    constraints += resolve_separations(kb.concept_separation_rules())
    if constraints:
        comms = enforce_constraints(graph, comms, constraints,
                                   resolution=params["resolution"],
                                   min_memories=params["min_memories"])
    # Entity membership is derived once structure is final: an entity joins every
    # community holding >= entity_share of its mention weight (a person spanning two
    # projects belongs to both concepts). cannot_link anchors are excluded so
    # multi-assignment can't reunite a pair a human split. Hub-capped entities are
    # handed back HERE: the cap keeps them from steering the partition, not from being
    # listed on the concepts their memories formed (they run the same share gate).
    comms = attach_entities(graph, comms, min_share=params["entity_share"],
                            exclude={str(c[k]) for c in constraints
                                     if c["kind"] == "cannot_link"
                                     for k in ("left_id", "right_id")},
                            capped_edges=[e for e in mention
                                          if str(e["entity_id"]) in hubs])
    previous = kb.matchable_concepts()
    matched = match_concepts(comms, previous, min_overlap=params["match_overlap"])
    prev_by_id = {p["id"]: p for p in previous}

    phase("labeling")
    upserts: "list[dict]" = []
    discarded = 0
    dismissed_hits = 0
    label_errors: "list[Exception]" = []
    for c in comms:
        prev_id = matched.get(c["key"])
        prev = prev_by_id.get(prev_id)
        if prev is not None and prev["status"] == "dismissed":
            dismissed_hits += 1
            continue
        member_ids = {m[0] for m in c["members"]}
        notes = [con["note"] for con in constraints
                 if con["note"] and (str(con["left_id"]) in member_ids or
                                     str(con["right_id"]) in member_ids)]
        mem_ids = [nid for nid, kind, _ in c["members"] if kind == MEMORY]
        ent_ids = [nid for nid, kind, _ in c["members"] if kind == ENTITY]
        view = kb.concept_member_view(mem_ids, ent_ids)
        if prev is not None and prev.get("label_locked"):
            label = {"coherent": True, "name": prev["name"], "kind": prev["kind"],
                     "description": prev["description"] or ""}
        else:
            label = _label(client, view, prev, notes=notes, errors=label_errors,
                           system=system)
        if not label["coherent"]:
            discarded += 1
            continue
        upserts.append({"key": c["key"], "prev_id": prev_id,
                        "parent_key": c["parent_key"], "level": c["level"],
                        "name": label["name"], "kind": label["kind"],
                        "description": label["description"],
                        "members": c["members"], "embedding": None})
    embed_errors: "list[Exception]" = []
    embed_failures = _embed_labels(
        client if embedding_client is None else embedding_client, upserts,
        errors=embed_errors)
    kept_prev = {u["prev_id"] for u in upserts if u["prev_id"] is not None}
    retire = [p["id"] for p in previous
             if p["status"] == "active" and p["id"] not in kept_prev]
    stats = {"vertices": graph.vcount(), "edges": graph.ecount(),
             "communities": len(comms), "kept": len(upserts),
             "discarded": discarded, "retired": len(retire),
             "dismissed_hits": dismissed_hits,
             "label_failures": len(label_errors), "embed_failures": embed_failures,
             "hub_dropped": hub_dropped, "knn_ms": knn_ms}
    if progress is not None:
        progress.update(stats)
    # A sample error per failing half travels out beside its count: a bare
    # `embed_failures: N` is undiagnosable, which cost a wasted run when an unset
    # KB_INFERENCE_URL pointed at the dashboard port. Derived once so the preview and
    # applied returns cannot drift apart.
    failures = {"label_error": str(label_errors[0]) if label_errors else None,
                "embed_error": str(embed_errors[0]) if embed_errors else None}
    if run_id is None:
        return {**stats, "preview": upserts, "retire": retire, **failures}
    phase("applying")
    res = kb.apply_concept_run(run_id, upserts, retire)
    # The failure samples are stored, not just returned: a fail-open pass degrades to
    # placeholder names, and without the reason on the row that is undiagnosable later.
    kb.finish_concept_run(run_id, status="done", stats={**stats, **res, **failures})
    return {**stats, **res, "run_id": run_id, **failures}


def _label(client, view: dict, prev: "dict | None",
          notes: "list[str] | None" = None, errors: "list | None" = None,
          system: "str | None" = None) -> dict:
    """LLM label for one community; fail-open, and label-durable for carried concepts.

    The previous concept's name is offered as a naming-stability hint ONLY when it is
    not itself a fallback placeholder — a placeholder offered back would anchor via the
    prompt's 'keep the previous name' rule and never heal. On a failed call (or no
    client) a carried concept KEEPS its previous label (name/kind/description) rather
    than degrading to a placeholder; only a concept with no usable previous label gets
    the placeholder. A bad tick never loses structure — and no longer loses names.
    `notes` are owner-authored curation notes (from constraints touching this community's
    members) passed through verbatim as extra grounding for the labeler."""
    prev_name = (prev or {}).get("name")
    hint = prev_name if prev_name and not is_fallback_name(prev_name) else None
    if client is not None:
        try:
            sys_msg, usr_msg = build_concept_prompt(view["entities"], view["memories"],
                                                    previous_name=hint,
                                                    notes=notes or [],
                                                    system=system)
            return parse_concept(client.complete(sys_msg, usr_msg, schema=CONCEPT_SCHEMA))
        except ProviderUnavailable:
            raise                     # a dead provider is not a bad label: abort the pass
        except Exception as exc:                     # fail-open, but no longer silent
            if errors is not None:
                errors.append(exc)
    if hint is not None:
        return {"coherent": True, "name": hint,
                "kind": (prev or {}).get("kind") or "other",
                "description": (prev or {}).get("description") or ""}
    return fallback_label(view["entities"])


def _embed_labels(client, upserts: "list[dict]", errors: "list | None" = None) -> int:
    """Embed each kept concept's name+description (its semantic handle for the read
    path — `concept_candidates` routes on it). Best-effort: on any failure — including a
    completion-only client with no .embed — vectors stay NULL and the next run refreshes
    them. Returns the number of concepts left without an embedding (0 on success).

    Fail-open, but no longer silent: the exception is appended to `errors` so the caller
    can report WHY, the same contract `_label` has. A bare count cost a wasted
    consolidation run — every concept reported unembedded with nothing to diagnose from,
    the cause being a KB_INFERENCE_URL default that resolved to the dashboard port."""
    if client is None or not upserts:
        return 0
    try:
        texts = [f"{u['name']}\n{u['description']}".strip() for u in upserts]
        for u, vec in zip(upserts, client.embed(texts)):
            u["embedding"] = vec
        return 0
    except Exception as exc:
        if errors is not None:
            errors.append(exc)
        return len(upserts)
