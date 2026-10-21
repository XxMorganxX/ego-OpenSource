"""The entity-promotion sweep: settle recurring candidates into the entity graph.

A scheduled, cross-memory pass (the per-memory enricher can't judge recurrence). Each tick:
  - INSTANCES: a candidate that recurred across >= threshold memories is promoted into a real
    sub-entity node under its category (memory --mentions--> instance); then `reconcile_instances`
    collects any instance left with zero support by note deletion/exclusion since the last tick.
  - CLASSES:   the whole active set is RECONCILED from votes — `reconcile_classes` activates the
    categories that clear the floor + dominance + cap gates and deactivates those that no longer
    do (hysteretic, so they don't flap). Votes (entity_candidates) are preserved either way.
All tracks are idempotent: re-running is safe.
"""

from kb_core.ingest.inference import ProviderUnavailable
from kb_core.knowledge_base import (
    DEFAULT_CLASS_VOTE_FLOOR, DEFAULT_DOMINANCE_HI, DEFAULT_DOMINANCE_LO,
    DEFAULT_DOMINANCE_MARGIN, DEFAULT_DOMINANCE_SPAN, DEFAULT_MAX_ACTIVE_CLASSES,
    KnowledgeBase,
)


class EntityPromotionWorker:
    def __init__(self, kb: KnowledgeBase, *, threshold: int = 2,
                 inference_client: "object | None" = None,
                 class_floor: int = DEFAULT_CLASS_VOTE_FLOOR,
                 max_classes: int = DEFAULT_MAX_ACTIVE_CLASSES,
                 dominance_hi: float = DEFAULT_DOMINANCE_HI,
                 dominance_lo: float = DEFAULT_DOMINANCE_LO,
                 dominance_span: int = DEFAULT_DOMINANCE_SPAN,
                 dominance_margin: float = DEFAULT_DOMINANCE_MARGIN) -> None:
        self._kb = kb
        self._client = inference_client          # meaning gate; None -> gate off (promote all)
        # Resolved ONCE here, not per candidate: a broken overlay (missing file, bad/cyclic
        # include) is a configuration error and must fail loud at worker construction, not
        # be swallowed candidate-by-candidate by _is_meaningful's fail-open try/except.
        self._system = None
        if inference_client is not None:
            from kb_core.meaning_judge import SYSTEM_FILE
            self._system = self._kb.prompt(SYSTEM_FILE)
        self._threshold = threshold              # instances: looser, accrete on re-crawl
        self._class_floor = class_floor          # classes: minimum votes to be considered
        self._max_classes = max_classes
        self._dominance_hi = dominance_hi
        self._dominance_lo = dominance_lo
        self._dominance_span = dominance_span
        self._dominance_margin = dominance_margin

    def run(self) -> int:
        """Settle instances + reconcile the class set; returns how many entities changed
        (promoted instances + activated + deactivated classes) this sweep."""
        settled = 0
        # promotable_entities is read once, so a name just blocked this sweep is still in the
        # list under a sibling category — track blocks locally so it isn't re-judged and
        # (with a nondeterministic verdict) promoted the same tick.
        blocked_this_sweep: "set[str]" = set()
        # A NEW name needs the judge; while the judge's provider is paused (or once it
        # raises ProviderUnavailable this sweep) those names are left unsettled for a later
        # tick -- neither promoted unjudged nor blocked -- and the judge is not called, so
        # an offline provider costs one timeout per sweep, not one per candidate. Names
        # that already exist, and every reconcile track below, need no model and still run.
        judge_provider = getattr(self._client, "provider", None)
        judge_down = (judge_provider not in (None, "local")
                      and self._kb.provider_paused(judge_provider) is not None)
        for g in self._kb.promotable_entities(threshold=self._threshold):
            try:
                if self._client is not None and not g["entity_exists"]:
                    if judge_down or g["normalized_name"] in blocked_this_sweep:
                        continue
                    if not self._is_meaningful(g):
                        blocked_this_sweep.add(g["normalized_name"])
                        continue                 # judged code symbol -> blocked, skip promotion
                self._kb.promote_entity(g["entity_type"], g["raw_name"])
                settled += 1
            except ProviderUnavailable as exc:
                self._kb.pause_provider(exc.provider, until=exc.until,
                                        reason=f"{type(exc).__name__}: {exc}")
                judge_down = True
            except Exception:
                # One bad group must not abort the sweep; its candidates stay unsettled
                # and are retried on the next tick.
                continue
        # Retraction counterpart: re-derive is_a and mentions from the current candidate ledger,
        # dropping links no candidate still supports (stale type/mention fades after re-crawl).
        # Runs AFTER promotion and BEFORE reconcile_instances so a retracted mention lets GC
        # collect a now-orphaned instance the same tick.
        try:
            settled += self._kb.reconcile_instance_links()
        except Exception:
            pass
        # Downward counterpart of the promotion loop: collect instances orphaned by note
        # deletion/exclusion since the last tick (an instance exists iff >= 1 memory supports it).
        # Runs AFTER promotion so a just-linked instance is never mistaken for an orphan.
        try:
            settled += self._kb.reconcile_instances()
        except Exception:
            pass
        try:
            res = self._kb.reconcile_classes(
                floor=self._class_floor, cap=self._max_classes,
                dominance_hi=self._dominance_hi, dominance_lo=self._dominance_lo,
                dominance_span=self._dominance_span, margin=self._dominance_margin)
            settled += len(res["activated"]) + len(res["deactivated"])
        except Exception:
            pass
        try:
            settled += self._kb.derive_instance_kinds()
        except Exception:
            pass
        return settled

    def _is_meaningful(self, group: dict) -> bool:
        """Judge a NEW candidate group: True to promote, False after recording a block.
        Fail-open — any inference/parse error promotes (never blocks on an outage) —
        except ProviderUnavailable, which propagates: the provider being down says nothing
        about the name, so run() leaves it unsettled rather than promoting it. The
        system prompt (`self._system`) was already resolved once at construction, not
        here — a broken overlay must have failed loud at worker start, not be caught by
        this method's own fail-open except below."""
        from kb_core.meaning_judge import build_meaning_prompt, parse_meaning, MEANING_SCHEMA
        try:
            view = self._kb.candidate_judge_view(group["entity_type"], group["normalized_name"])
            sys_msg, usr_msg = build_meaning_prompt(
                view["name"], group["entity_type"], view["mentions"],
                system=self._system)
            keep, reason = parse_meaning(
                self._client.complete(sys_msg, usr_msg, schema=MEANING_SCHEMA))
        except ProviderUnavailable:
            raise
        except Exception:
            return True
        if not keep:
            self._kb.block_instance_promotion(
                group["normalized_name"], reason, origin="judge")
        return keep
