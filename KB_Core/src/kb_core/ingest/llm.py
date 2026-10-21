"""Config-driven, multi-pass LLM enrichment.

The pipeline of LLM passes is defined in `enrichment.toml` — an ordered list of [[pass]]
entries, each one stateless call with its own editable system prompt and user prompt
template. A pass's user template can reference {title}, {content}, {path}, {location} (the
path as a readable breadcrumb), {source}, and the output of any earlier pass by its name
(e.g. {summary}). An optional `target` writes a
pass's output to a node field. The LLMEnricher implements the Enricher protocol, so the
existing EnrichmentWorker runs it over `pending` nodes off the hot path.
"""

import json
import os
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kb_core.doc_kind import normalize_doc_kind
from kb_core.domain import Node, NodePatch
from kb_core.entities import (
    BELONGS_TO, ENTITY_CLASS, ROLES, UNTYPED_CATEGORY, is_valid_class_name,
    is_valid_instance_name, normalize_entity_name,
)
from kb_core.ingest.inference import InferenceClient, client_identity
from kb_core.paths import humanize_path
from kb_core.policy import DEFAULT_POLICY, Policy
from kb_core.prompts import PromptResolver

if TYPE_CHECKING:
    from kb_core.knowledge_base import KnowledgeBase

@dataclass
class LLMPass:
    name: str
    system: str
    user: str
    target: str | None = None       # 'summary' | 'tags' | 'entities' | None (feeds later passes)
    schema: dict | None = None      # JSON Schema enforcing the reply's structure (optional)
    phase: str = "enrich"           # 'enrich' (first-time) | 'recrawl' (re-analysis)


def _date_str(dt) -> str:
    """A datetime as a plain ISO date for the gate prompt's temporal reasoning; '' if absent."""
    return dt.date().isoformat() if dt is not None else ""


def render_template(template: str, context: dict) -> str:
    """Substitute {key} tokens for known keys; unknown {tokens} are left untouched.

    A plain replace (not str.format) so stray braces in note content can't break it.
    """
    out = template
    for key, value in context.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _parse_json(text: str):
    """Best-effort JSON parse of a model reply; tolerates ```json fences. None on failure.

    With schema-enforced output the reply is already clean JSON; the fence handling is a
    fallback for engines/models that don't honor structured output.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        return json.loads(t.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def load_passes(env: dict | None = None, policy: "Policy | None" = None) -> "list[LLMPass]":
    """Read the enrichment pipeline from enrichment.toml (path: KB_ENRICHMENT_CONFIG env,
    else ./enrichment.toml). Returns [] if the file is absent (enrichment then no-ops).

    Prompts and schemas live in their own files (resolved relative to the config), so they
    are edited directly, not embedded in the TOML:
      system_file / user_file -> a prompt file (else inline `system` / `user`)
      schema_file             -> a JSON-Schema file, loaded into `LLMPass.schema`
    A prompt under prompts/ is read through PromptResolver: the policy's
    overlay dir shadows it by name, and its {{include:…}} / {{taxonomy}} markers expand
    here, once. `policy` defaults to the shipped DEFAULT_POLICY (no overlay); daemons pass
    kb.policy.
    """
    env = os.environ if env is None else env
    policy = policy or DEFAULT_POLICY
    path = env.get("KB_ENRICHMENT_CONFIG", "enrichment.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    base = Path(path).resolve().parent
    resolver = PromptResolver(policy, base / "prompts")

    def _text(inline: str | None, file_ref: str | None) -> str:
        # A referenced file wins; otherwise the inline string; stripped either way.
        if file_ref:
            head, _, name = file_ref.partition("/")
            if head == "prompts" and name and "/" not in name:
                return resolver.load(name)
            return (base / file_ref).read_text(encoding="utf-8").strip()
        return (inline or "").strip()

    passes: list[LLMPass] = []
    for p in data.get("pass", []):
        schema = None
        if p.get("schema_file"):
            with open(base / p["schema_file"], encoding="utf-8") as sf:
                schema = json.load(sf)
        passes.append(LLMPass(
            name=p["name"],
            system=_text(p.get("system"), p.get("system_file")),
            user=_text(p.get("user"), p.get("user_file")),
            target=p.get("target"), schema=schema,
            phase=p.get("phase", "enrich"),
        ))
    return passes


def passes_for_phase(passes: "list[LLMPass]", phase: str) -> "list[LLMPass]":
    """The subset of passes that run in a given phase ('enrich' or 'recrawl'), order kept."""
    return [p for p in passes if p.phase == phase]


def clients_for_passes(router, passes: "list[LLMPass]") -> "dict[str, InferenceClient]":
    """Resolve every configured pass through the InferenceRouter (inference.toml).

    Full coverage by construction: a [[pass]] whose name is not a registered inference
    task raises RoutingError here at daemon start, forcing the TASKS row + toml block +
    llm-inference-map update in the same change that adds the pass.
    """
    return {p.name: router.client_for(p.name) for p in passes}


# Pass targets that make a memory influence the entity system (vote, create, link). The
# 'entity_gate' pass can suppress all of these for a noise memory; they're skipped when the
# node is ineligible. The gate pass MUST run before them (it's ordered first in enrichment.toml).
_ENTITY_TARGETS = {"entities", "entity_classes", "classify"}

# NOTE: there is deliberately no confidence floor here any more. Previously anything at or
# below the rubric's bottom band was dropped before staging, which made that band unobservable —
# it held zero rows, so nobody could tell whether the model used it correctly. Measured against
# the promotion judge's own verdicts, extraction confidence scored at or below chance
# and judged-bad anchors averaged HIGHER confidence than judged-good ones, because the old rubric
# graded "is it written in the text?" — a near-constant, since you can only extract what is there.
# The rubric now grades anchor DURABILITY (see prompts/entities.system.md) and the bottom band is
# staged rather than deleted: it is damped in concept clustering (concepts.mention_edge_weight)
# and barred from creating new entities (knowledge_base.promotable_entities). An anchor the model
# itself flags as implementation-only is the most useful thing this pass produces; discarding it
# threw that away and left the expensive meaning judge to rediscover it.

# A discover reply proposes multiple concept candidates, but only the high-signal ones —
# 3-4 max (see prompts/discover_classes.system.md). The schema enforces this for engines
# that honor structured output; this is the defense-in-depth cap so a non-enforcing engine
# or the fence-fallback parse can't stage a runaway list.
MAX_DISCOVER_CANDIDATES = 4


class LLMEnricher:
    """Runs the configured LLM passes over a node and writes the resulting derived fields."""
    name = "llm"

    def __init__(self, client: InferenceClient, passes: "list[LLMPass]", *,
                 max_classes: int | None = None,
                 clients_by_pass: "dict[str, InferenceClient] | None" = None) -> None:
        self._client = client
        self._clients_by_pass = dict(clients_by_pass or {})
        self._passes = passes
        self._max_classes = max_classes   # cap on offered categories (None -> KB default)

    def enrich(self, node: Node, kb: "KnowledgeBase", *,
               only: "set[str] | None" = None) -> "dict | None":
        # The passes this run will actually execute, in declaration order. Recrawl passes
        # `only` to run just the pass whose dependency moved.
        effective = [p for p in self._passes if only is None or p.name in only]
        if not effective:
            return None
        # Computed from the EFFECTIVE passes, not every configured one: the per-memory
        # refresh must clear exactly what this run re-stages, or a subset run wipes a
        # contribution it will not rebuild.
        regen_classes = any(p.target in ("entity_classes", "classify") for p in effective)
        # NOTE: refresh_entity_contribution's `instances` parameter is currently a no-op
        # (instance reconciliation is owned by _stage_entities / prune_memory_instances) —
        # kept here so the call stays correct the moment that changes back.
        regen_instances = any(p.target == "entities" for p in effective)
        context = {"title": node.title or "", "content": node.content,
                   "path": node.path, "source": node.source,
                   "location": humanize_path(node.path),
                   # Temporal signal for the gate's staleness judgment.
                   "created": _date_str(node.source_created_at or node.ingested_at),
                   "updated": _date_str(node.source_updated_at or node.updated_at),
                   "today": date.today().isoformat(),
                   # The instances this memory already produced, so the extract pass MAINTAINS
                   # them (keep/re-type/drop) instead of re-extracting blind. Empty at first
                   # enrich -> the prompt then just extracts fresh.
                   "existing_instances": kb.memory_instance_summary(node.id)}
        patch: dict = {}
        used: "set[str]" = set()   # provider:model identities of passes that actually ran
        executed: "list[str]" = []
        # Whether this memory may participate in the entity system. We start from its persisted
        # state (noise OR dead excludes it), so an already-excluded node stays excluded even on a
        # run with no gate pass; the 'entity_gate' pass (ordered before the entity passes) can
        # flip it for this run.
        # Human exclusion (this node or an ancestor folder) suppresses ALL entity work: the gate
        # pass too, so its verdict can't flip `eligible` back on. summary/tags still run normally.
        excluded = kb.analysis_excluded(node)
        eligible = node.entity_eligible and node.dead_at is None and not excluded
        refreshed = False
        for p in effective:
            if excluded and p.target == "entity_gate":
                continue                       # excluded: skip the noise/stale triage entirely
            if p.target in _ENTITY_TARGETS and not eligible:
                continue                       # noise/excluded: cast no votes, create/link nothing
            if p.target in _ENTITY_TARGETS and not refreshed:
                # Replace, don't stack: before the first entity pass re-stages, clear this memory's
                # prior unpromoted contributions of the kind this run regenerates, so re-analysis
                # can't accumulate drifted votes/links across crawls.
                kb.refresh_entity_contribution(
                    node.id, classes=regen_classes, instances=regen_instances)
                refreshed = True
            # Refresh the category list each pass, so a later (classify) pass sees the
            # categories an earlier (discover) pass just created.
            context["entity_classes"] = self._class_list(kb)
            if p.target == "doc_kind":
                # Refreshed per pass like entity_classes: a recrawl batch's earlier
                # memories should influence the labels offered to later ones.
                context["doc_kinds"] = self._kind_list(kb)
            pass_client = self._clients_by_pass.get(p.name, self._client)
            reply = pass_client.complete(
                p.system, render_template(p.user, context), schema=p.schema).strip()
            executed.append(p.name)
            ident = client_identity(pass_client)
            if ident:
                used.add(ident)
            parsed = _parse_json(reply) if p.schema else None
            context[p.name] = reply                       # available to later passes
            if p.target == "summary":
                patch["summary"] = reply
            elif p.target == "tags":
                patch["tags"] = (parsed if isinstance(parsed, list)
                                 else [t.strip() for t in reply.split(",") if t.strip()])
            elif p.target == "doc_kind":
                kind = normalize_doc_kind(parsed.get("kind")
                                          if isinstance(parsed, dict) else None)
                # Idempotence: recrawl re-asks every cycle, so a repeat classification that
                # agrees with the memory's current kind must NOT count as a write -- no
                # updated_at churn, no spurious NOTIFY, staleness-gate semantics preserved.
                if kind is not None and kind != node.doc_kind:
                    patch["doc_kind"] = kind
            elif p.target == "entity_gate":
                eligible = self._gate(kb, node, parsed)
            elif p.target == "entities":
                self._stage_entities(kb, node, parsed)
            elif p.target == "entity_classes":
                self._upsert_classes(kb, node, parsed)
            elif p.target == "classify":
                self._classify(kb, node, parsed)
        if patch:
            kb.update(node.id, NodePatch(**patch))
        # An empty list is still meaningful: the requested passes existed but were skipped by
        # the gate. The enrichment worker must not stamp a dependency cursor for them.
        report: dict = {"passes": executed}
        if used:
            report["providers"] = sorted(used)
        return report

    @staticmethod
    def _gate(kb: "KnowledgeBase", node: Node, parsed) -> bool:
        """Triage gate: persist the model's verdict and return whether this memory may participate
        in the entity system this run (entity passes are skipped when it may not). Two verdicts:

          - NOISE — content with no extractable meaning (mashed characters, accidental input,
            empty filler). The memory is excluded AND any influence it already cast is retracted
            (set_entity_eligible). NOT a richness test: a brief but genuine note (a one-line to-do,
            a name, a single fact) is low-signal yet not noise, so it stays eligible.
          - STALE — content no longer chronologically relevant (a passed event, an overdue one-off,
            an outdated status). The memory is RETIRED (mark_dead): frozen, influence KEPT, never
            recrawled — until a human revives it.

        An explicit 'neither' verdict auto-recovers a memory from a prior NOISE flag (its content
        now reads as meaningful); a dead memory is NOT auto-revived — staleness is a deliberate
        retirement a human undoes. Fail-open: a missing/garbled verdict (no usable flags) leaves
        the memory exactly as-is, so a parse blip never silently changes its state. Writes only on
        an actual transition, so re-analysis of a settled node is cheap.
        Expects {"noise": bool, "stale": bool, "reason"?}."""
        if not isinstance(parsed, dict) or ("noise" not in parsed and "stale" not in parsed):
            return node.entity_eligible and node.dead_at is None   # no verdict -> leave as-is
        reason = parsed.get("reason")
        if parsed.get("noise") is True:
            if node.entity_eligible:                               # transition -> exclude + retract
                kb.set_entity_eligible(node.id, False, reason=reason)
            return False
        if parsed.get("stale") is True:
            if node.dead_at is None:                               # transition -> retire (freeze)
                kb.mark_dead(node.id, reason=reason)
            return False
        if not node.entity_eligible:                              # explicit 'ok' -> un-noise
            kb.set_entity_eligible(node.id, True)
        return node.dead_at is None        # a (still) dead node remains excluded; revive is manual

    def _class_list(self, kb: "KnowledgeBase") -> str:
        """The 'these are the categories we have' block injected as {entity_classes} — the top
        categories (capped by max_classes), strongest first. Active categories carry their
        description (grounding what belongs in each); the strongest not-yet-active candidates are
        listed name-only, so the model reuses an emerging name rather than minting a duplicate."""
        pairs = kb.entity_classes_for_prompt(limit=self._max_classes)
        if not pairs:
            return "(none yet)"
        return "\n".join(f"- {name} — {desc}" if desc else f"- {name}"
                         for name, desc in pairs)

    @staticmethod
    def _kind_list(kb: "KnowledgeBase") -> str:
        """The 'kinds already in use' block injected as {doc_kinds} — support-ranked so
        the model reuses an emerging label instead of minting a near-duplicate."""
        kinds = kb.doc_kinds_for_prompt()
        if not kinds:
            return "(none yet)"
        return "\n".join(f"- {k}" for k in kinds)

    @staticmethod
    def _upsert_classes(kb: "KnowledgeBase", node: Node, parsed) -> None:
        """Discover pass: for each concept the memory implies, ensure an (initially INACTIVE)
        class placeholder and stage one per-memory vote toward activation. A class is only
        flipped active once it recurs across enough distinct memories — until then it isn't
        offered as a concept, so a one-off/hallucinated name never sticks. Names that fail
        `is_valid_class_name` (placeholder/meta artifacts like NEW_CATEGORY_NAME, which a
        weak model emits *consistently* and would otherwise clear the recurrence gate) are
        dropped here, before they ever cast a vote. A well-formed name is then folded onto an
        existing class when it's a spelling variant (`resolve_class_name`), so a
        typo votes for the real category instead of minting a fragment. Idempotent (the model
        is told the active list so it reuses names instead of duplicating). Caps at
        MAX_DISCOVER_CANDIDATES to enforce the high-signal, 3-4 max policy (schema enforces for
        structured-output engines; this is the defense-in-depth code-level cap for
        non-enforcing engines).
        Expects {"classes": [{"name", "status": "new"|"existing", "kind"?}, …]}."""
        if not isinstance(parsed, dict):
            return
        for item in parsed.get("classes", [])[:MAX_DISCOVER_CANDIDATES]:
            if isinstance(item, dict):
                name, desc, kind = item.get("name"), item.get("description"), item.get("kind")
            else:
                name, desc, kind = item, None, None
            if name and is_valid_class_name(name):
                name = kb.resolve_class_name(name)
                kb.upsert_entity_class(name, active=False, description=desc, kind=kind)
                kb.stage_entity_candidate(node.id, ENTITY_CLASS, name)        # one vote

    @staticmethod
    def _classify(kb: "KnowledgeBase", node: Node, parsed) -> None:
        """Classify pass: group the memory under each active concept it was assigned to, via
        a belongs_to edge. Inactive (not-yet-recurring) classes are skipped — their voters get
        belongs_to backfilled when the class activates. Expects {"classes": ["name", …]}."""
        if not isinstance(parsed, dict):
            return
        for name in parsed.get("classes", []):
            cls = kb.get_entity_class(name) if isinstance(name, str) else None
            if cls is not None and cls.active:
                kb.link_entity(node.id, cls.node.id, BELONGS_TO)

    @staticmethod
    def _stage_entities(kb: "KnowledgeBase", node: Node, parsed) -> None:
        """Extract/maintain pass: stage each named thing under the type(s) the model names (open
        vocabulary — a coined type need not already be active), then reconcile — prune the memory's
        instance votes the extraction did NOT return. Upserts are idempotent, so an unchanged vote
        keeps its promoted state and mentions (no churn); only the delta (added / re-typed / dropped)
        mutates. Expects {"entities": [{"types": [...], "name", "confidence", "occurred_at"?,
        "role"?}, …]} (legacy single "category"/"type" accepted). `occurred_at` and
        `role` are tolerant, best-effort fields — parsed leniently and dropped to None
        on anything malformed, never rejecting the whole anchor."""
        if not isinstance(parsed, dict):
            return
        kept: "set[tuple[str, str]]" = set()
        for item in parsed.get("entities", []):
            name = item.get("name")
            confidence = item.get("confidence")
            types = item.get("types")
            if types is None:
                single = item.get("category") or item.get("type")
                types = [single] if single else []
            if not isinstance(types, list):
                types = []
            occurred_at = _parse_occurred_at(item.get("occurred_at"))
            role = item.get("role")
            if role not in ROLES:
                role = None
            if not (name and is_valid_instance_name(name)):
                continue
            # Open vocabulary: keep any well-formed type the model names. is_valid_class_name still
            # drops artifacts (SCREAMING_SNAKE, "n/a", slashes/underscores, hyphen-glued run-on
            # sentences, repetition spirals), but a real category no longer has to already be
            # active to survive. A well-formed name is then folded onto an existing class when
            # it's a spelling variant (resolve_class_name).
            fit = [kb.resolve_class_name(t) for t in types
                   if isinstance(t, str) and is_valid_class_name(t)]
            targets = fit if fit else [UNTYPED_CATEGORY]
            for t in targets:
                kb.stage_entity_candidate(node.id, t, name, confidence=confidence,
                                          occurred_at=occurred_at, role=role)
                kept.add((normalize_entity_name(t), normalize_entity_name(name)))
            # Each coined type also votes for itself as a class (the same two calls the discover
            # pass makes), so it accumulates recurrence and can activate via reconcile_classes —
            # otherwise it would create an instance link but stay a permanently-inactive category.
            for t in fit:
                kb.upsert_entity_class(t, active=False)
                kb.stage_entity_candidate(node.id, ENTITY_CLASS, t)
        kb.prune_memory_instances(node.id, kept)


def _parse_occurred_at(value) -> datetime | None:
    """Tolerant ISO date/datetime parse of the model's occurred_at; None on anything odd."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
