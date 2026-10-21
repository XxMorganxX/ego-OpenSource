"""One entity-resolution pass: describe blank entities, re-embed dirty ones, scan for near
same-kind pairs, judge, and stage confirmed merge suggestions. Pure orchestration over
KnowledgeBase — no I/O of its own.

Describe runs FIRST so a freshly drafted description (which sets embed_dirty) is picked up
by the re-embed in the same tick."""
from __future__ import annotations

from kb_core.ingest.inference import ProviderUnavailable


def _paused_provider(kb, client) -> "str | None":
    """The client's remote provider if a pause is in force for it, else None. Local (and
    provider-less test stubs) are never gated: local has no quota."""
    provider = getattr(client, "provider", None)
    if provider in (None, "local"):
        return None
    return provider if kb.provider_paused(provider) is not None else None


def run_once(kb, client=None, *, describe_client=None, judge_client=None,
             embed_client=None, max_distance: float = 0.20, top_k: int = 5,
             batch: int = 32, describe_batch: int = 16) -> dict:
    """One resolve tick: describe -> re-embed -> judge, so a fresh description is
    embedded the same tick. Each role takes its own client (inference.toml routes
    describe_entity / merge_judge / embed independently); `client` is the
    all-three fallback kept for tests and simple callers.

    Provider outages are gated PER ROLE. Describe is skipped while its own provider is
    paused, and the judge while its own is. A ProviderUnavailable from either pauses that
    provider (provider_pause, shared with every daemon on this database) and ends only
    that step. Re-embed and the candidate scan always run (embed is pinned local), so a
    claude-cli session limit costs descriptions, never embeddings or merge suggestions.
    `paused` in the result names each role skipped this tick and the provider that
    caused it."""
    describe_client = describe_client or client
    judge_client = judge_client or client
    embed_client = embed_client or client
    paused: "dict[str, str]" = {}

    def _outage(role: str, exc: ProviderUnavailable) -> None:
        kb.pause_provider(exc.provider, until=exc.until,
                          reason=f"{type(exc).__name__}: {exc}")
        paused[role] = exc.provider

    described = 0
    held = _paused_provider(kb, describe_client)
    if held is not None:
        paused["describe"] = held
    else:
        try:
            described = kb.describe_missing_entities(describe_client, limit=describe_batch)
        except ProviderUnavailable as exc:
            _outage("describe", exc)
    reembedded = kb.reembed_dirty_entities(embed_client, batch=batch)
    candidates = kb.find_merge_candidates(max_distance=max_distance, top_k=top_k)
    res = {"staged": 0, "blocked": 0}
    held = _paused_provider(kb, judge_client)
    if held is not None:
        paused["judge"] = held
    else:
        try:
            res = kb.judge_and_stage_candidates(judge_client, candidates)
        except ProviderUnavailable as exc:
            _outage("judge", exc)
    return {"described": described, "reembedded": reembedded, "candidates": len(candidates),
            "staged": res["staged"], "blocked": res["blocked"], "paused": paused}
