"""Per-install policy: the owner's scalar knobs, scoped decay rules, and vocabulary, loaded
from policy.toml (path: KB_POLICY_CONFIG env, default ./policy.toml) into one frozen Policy
object that KnowledgeBase carries. A missing DEFAULT file yields the shipped defaults (a
fresh clone must boot); a missing EXPLICIT file, an unknown key, or an out-of-range value
is a PolicyError at process start (the inference_routing pattern). Pure — no DB, no network.

Setup and reference: KB_Core/docs/policy-setup.md."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from kb_core.entities import (KINDS, MAX_CLASS_DESCRIPTION_LEN, is_valid_class_name,
                              normalize_entity_name)

DEFAULT_PATH = "policy.toml"
ENV_PATH = "KB_POLICY_CONFIG"
DEFAULT_OVERLAY_DIR = "prompts.local"

_RULE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_LTREE_LABEL_RE = re.compile(r"^[A-Za-z0-9_]+$")
_LTREE_PATH_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")


class PolicyError(RuntimeError):
    """policy.toml is missing (when explicitly pointed at), unreadable, has an unknown
    section or key, or holds a value out of range."""


@dataclass(frozen=True)
class Match:
    """A decay rule's scope. Given conditions AND together; at least one is required."""
    tags: tuple[str, ...] = ()            # every listed tag must be present (array @>)
    path_prefix: str | None = None        # ltree ancestor-or-self (path <@ prefix)
    source: str | None = None             # nodes.source equality

    def __post_init__(self) -> None:
        if not self.tags and self.path_prefix is None and self.source is None:
            raise PolicyError(
                "a decay rule's match needs at least one of tags, path_prefix, source")


@dataclass(frozen=True)
class DecayRule:
    name: str
    match: Match
    half_life_days: float
    floor: float


@dataclass(frozen=True)
class Projects:
    folder_label: str                     # the ltree label that opens a project subtree
    max_depth: int = 1                    # label may sit at depth 0..max_depth
    dormancy_half_life_days: float = 182.5


@dataclass(frozen=True)
class Importance:
    recency_half_life_days: float = 8.0
    recency_floor: float = 0.65
    access_saturation: float = 10.0

    @property
    def recency_half_life_seconds(self) -> float:
        return self.recency_half_life_days * 86400.0


@dataclass(frozen=True)
class Concepts:
    goal_window_days: int = 14
    data_recency_half_life_days: float = 14.0
    retrieval_recency_half_life_days: float = 7.0
    recency_floor: float = 0.2


@dataclass(frozen=True)
class Category:
    name: str
    kind: str
    description: str


@dataclass(frozen=True)
class Taxonomy:
    categories: tuple[Category, ...] = ()
    kind_seeds: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def kind_for(self, name: str) -> str | None:
        """The structural kind a category name maps to: its own category's kind, else a
        seed, else None (the discover pass assigns it). Names compare normalized."""
        key = normalize_entity_name(name)
        for c in self.categories:
            if normalize_entity_name(c.name) == key:
                return c.kind
        return self.kind_seeds.get(key)

    def prompt_block(self) -> str:
        """The extraction prompt's fixed-category block, or "" when there are none — every
        sentence that refers to the fixed list lives here so an empty taxonomy leaves no
        dangling reference in the prompt."""
        if not self.categories:
            return ""
        lines = "\n".join(f"  {c.name} — {c.description}" for c in self.categories)
        return ("You may also use one of the fixed broad categories below.\n\n"
                "Fixed broad categories:\n\n" + lines)


@dataclass(frozen=True)
class Prompts:
    overlay_dir: str | None = None        # None = no overlay (shipped defaults)


DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category("person", "person", "an individual human"),
    Category("organization", "organization",
             "a company, institution, group, club, nonprofit, or public body"),
    Category("place", "place", "a geographic or physical location"),
    Category("event", "event", "a meeting, call, milestone, or other dated occurrence"),
    Category("project", "project", "a bounded initiative, build effort, or engagement"),
    Category("artifact", "artifact", "a document, paper, titled work, or other produced thing"),
    Category("concept", "concept", "an abstract idea, method, principle, pattern, or problem area"),
    Category("system", "system", "a software system, tool, platform, or technical component"),
)


@dataclass(frozen=True)
class Policy:
    decay: tuple[DecayRule, ...] = ()
    projects: Projects | None = None
    importance: Importance = field(default_factory=Importance)
    concepts: Concepts = field(default_factory=Concepts)
    taxonomy: Taxonomy = field(default_factory=lambda: Taxonomy(DEFAULT_CATEGORIES))
    prompts: Prompts = field(default_factory=Prompts)
    source_path: str | None = None        # None = shipped defaults

    @property
    def is_default(self) -> bool:
        return self.source_path is None

    def to_dict(self) -> dict:
        def _match(m: Match) -> dict:
            out: dict = {}
            if m.tags:
                out["tags"] = list(m.tags)
            if m.path_prefix is not None:
                out["path_prefix"] = m.path_prefix
            if m.source is not None:
                out["source"] = m.source
            return out
        return {
            "decay": [{"name": r.name, "match": _match(r.match),
                       "half_life_days": r.half_life_days, "floor": r.floor}
                      for r in self.decay],
            "projects": None if self.projects is None else {
                "folder_label": self.projects.folder_label,
                "max_depth": self.projects.max_depth,
                "dormancy_half_life_days": self.projects.dormancy_half_life_days},
            "importance": {"recency_half_life_days": self.importance.recency_half_life_days,
                           "recency_floor": self.importance.recency_floor,
                           "access_saturation": self.importance.access_saturation},
            "concepts": {
                "goal_window_days": self.concepts.goal_window_days,
                "data_recency_half_life_days": self.concepts.data_recency_half_life_days,
                "retrieval_recency_half_life_days":
                    self.concepts.retrieval_recency_half_life_days,
                "recency_floor": self.concepts.recency_floor},
            "taxonomy": {"categories": [{"name": c.name, "kind": c.kind,
                                         "description": c.description}
                                        for c in self.taxonomy.categories],
                         "kind_seeds": dict(self.taxonomy.kind_seeds)},
            "prompts": {"overlay_dir": self.prompts.overlay_dir},
        }


DEFAULT_POLICY = Policy()


# ---- parsing / validation -------------------------------------------------------------

def _fail(path: str, where: str, msg: str) -> PolicyError:
    return PolicyError(f"{where} in {path}: {msg}")


def _table(value, path: str, where: str) -> dict:
    if not isinstance(value, dict):
        raise _fail(path, where, "must be a table")
    return value


def _keys(block: dict, allowed: set[str], path: str, where: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise _fail(path, where, f"unknown key(s) {unknown} (allowed: {sorted(allowed)})")


def _number(block: dict, key: str, default, path: str, where: str, *, gt=None, lo=None,
            hi=None, integer: bool = False, required: bool = False):
    if key not in block:
        if required:
            raise _fail(path, where, f"{key} is required")
        return default
    v = block[key]
    ok = isinstance(v, int) and not isinstance(v, bool) if integer \
        else isinstance(v, (int, float)) and not isinstance(v, bool)
    if not ok:
        raise _fail(path, where, f"{key} must be a{'n integer' if integer else ' number'}")
    if gt is not None and not v > gt:
        raise _fail(path, where, f"{key} must be > {gt}")
    if lo is not None and v < lo:
        raise _fail(path, where, f"{key} must be >= {lo}")
    if hi is not None and v > hi:
        raise _fail(path, where, f"{key} must be <= {hi}")
    return int(v) if integer else float(v)


def _string(block: dict, key: str, path: str, where: str, *, required: bool = False,
            pattern: re.Pattern | None = None, what: str = "") -> str | None:
    if key not in block:
        if required:
            raise _fail(path, where, f"{key} is required")
        return None
    v = block[key]
    if not isinstance(v, str) or not v.strip():
        raise _fail(path, where, f"{key} must be a non-empty string")
    if pattern is not None and not pattern.match(v):
        raise _fail(path, where, f"{key} {what or 'has an invalid form'}: {v!r}")
    return v


def _rule(i: int, block, path: str) -> DecayRule:
    where = f"[[decay]] #{i + 1}"
    block = _table(block, path, where)
    _keys(block, {"name", "match", "half_life_days", "floor"}, path, where)
    name = _string(block, "name", path, where, required=True, pattern=_RULE_NAME_RE,
                   what="must match [a-z0-9_-]+")
    match = _table(block.get("match"), path, f"{where}.match")
    _keys(match, {"tags", "path_prefix", "source"}, path, f"{where}.match")
    tags: tuple[str, ...] = ()
    if "tags" in match:
        raw = match["tags"]
        if (not isinstance(raw, list) or not raw
                or not all(isinstance(t, str) and t.strip() for t in raw)):
            raise _fail(path, f"{where}.match", "tags must be a non-empty list of strings")
        tags = tuple(raw)
    path_prefix = _string(match, "path_prefix", path, f"{where}.match",
                          pattern=_LTREE_PATH_RE, what="must be an ltree path like A.B")
    source = _string(match, "source", path, f"{where}.match")
    if not tags and path_prefix is None and source is None:
        raise _fail(path, f"{where}.match", "needs at least one of tags, path_prefix, source")
    hl = _number(block, "half_life_days", None, path, where, gt=0, required=True)
    floor = _number(block, "floor", None, path, where, lo=0, hi=1, required=True)
    return DecayRule(name, Match(tags, path_prefix, source), hl, floor)


def _projects(block, path: str) -> Projects:
    where = "[projects]"
    block = _table(block, path, where)
    _keys(block, {"folder_label", "max_depth", "dormancy_half_life_days"}, path, where)
    label = _string(block, "folder_label", path, where, required=True,
                    pattern=_LTREE_LABEL_RE, what="must be a single ltree label")
    return Projects(label,
                    _number(block, "max_depth", 1, path, where, lo=0, integer=True),
                    _number(block, "dormancy_half_life_days", 182.5, path, where, gt=0))


def _importance(block, path: str) -> Importance:
    where = "[importance]"
    block = _table(block, path, where)
    _keys(block, {"recency_half_life_days", "recency_floor", "access_saturation"}, path, where)
    return Importance(_number(block, "recency_half_life_days", 8.0, path, where, gt=0),
                      _number(block, "recency_floor", 0.65, path, where, lo=0, hi=1),
                      _number(block, "access_saturation", 10.0, path, where, gt=0))


def _concepts(block, path: str) -> Concepts:
    where = "[concepts]"
    block = _table(block, path, where)
    _keys(block, {"goal_window_days", "data_recency_half_life_days",
                  "retrieval_recency_half_life_days", "recency_floor"}, path, where)
    return Concepts(
        _number(block, "goal_window_days", 14, path, where, lo=1, hi=365, integer=True),
        _number(block, "data_recency_half_life_days", 14.0, path, where, gt=0),
        _number(block, "retrieval_recency_half_life_days", 7.0, path, where, gt=0),
        _number(block, "recency_floor", 0.2, path, where, lo=0, hi=1))


def _taxonomy(block, path: str) -> Taxonomy:
    where = "[taxonomy]"
    block = _table(block, path, where)
    _keys(block, {"categories", "kind_seeds"}, path, where)
    cats: list[Category] = []
    seen: dict[str, str] = {}
    raw_cats = block.get("categories", [])
    if not isinstance(raw_cats, list):
        raise _fail(path, where, "categories must be an array of tables")
    for i, c in enumerate(raw_cats):
        cw = f"{where}.categories #{i + 1}"
        c = _table(c, path, cw)
        _keys(c, {"name", "kind", "description"}, path, cw)
        name = _string(c, "name", path, cw, required=True)
        if not is_valid_class_name(name):
            raise _fail(path, cw, f"name {name!r} fails the class-name form guard "
                                  "(1-4 plain words, letters/space/'/- only)")
        kind = _string(c, "kind", path, cw, required=True)
        if kind not in KINDS:
            raise _fail(path, cw, f"kind must be one of {list(KINDS)}, got {kind!r}")
        desc = _string(c, "description", path, cw, required=True)
        if len(desc) > MAX_CLASS_DESCRIPTION_LEN:
            raise _fail(path, cw, f"description longer than {MAX_CLASS_DESCRIPTION_LEN} chars")
        key = normalize_entity_name(name)
        if key in seen:
            raise _fail(path, cw, f"category names must be unique (normalized): {name!r}")
        seen[key] = kind
        cats.append(Category(name, kind, desc))
    seeds: dict[str, str] = {}
    raw_seeds = _table(block.get("kind_seeds", {}), path, f"{where}.kind_seeds")
    for k, v in raw_seeds.items():
        key = normalize_entity_name(k)
        if not isinstance(v, str) or v not in KINDS:
            raise _fail(path, f"{where}.kind_seeds", f"{k!r} must map to one of {list(KINDS)}")
        if key in seen and seen[key] != v:
            raise _fail(path, f"{where}.kind_seeds",
                        f"{k!r} = {v!r} must agree with the category of the same name ({seen[key]!r})")
        seeds[key] = v
    return Taxonomy(tuple(cats), MappingProxyType(seeds))


def _tenant_dir(d: str, path: str) -> str:
    """A tenant's overlay_dir: a relative path resolves beside the tenant's own policy file,
    never against the process cwd (KB_Core, where the OWNER's prompts.local/ lives)."""
    base = os.path.dirname(os.path.abspath(path))
    return os.path.normpath(d if os.path.isabs(d) else os.path.join(base, d))


def _prompts(block, path: str, tenant: bool = False) -> Prompts:
    where = "[prompts]"
    block = _table(block, path, where)
    _keys(block, {"overlay_dir", "overlay"}, path, where)
    overlay = block.get("overlay", True)
    if not isinstance(overlay, bool):
        raise _fail(path, where, "overlay must be true or false")
    if not overlay:
        # `overlay = false`: shipped prompts only (the tenant-neutral policy says this).
        if "overlay_dir" in block:
            raise _fail(path, where, "overlay = false cannot be combined with overlay_dir")
        return Prompts(None)
    if "overlay_dir" not in block:
        return Prompts(_tenant_dir(DEFAULT_OVERLAY_DIR, path) if tenant else DEFAULT_OVERLAY_DIR)
    d = block["overlay_dir"]
    if not isinstance(d, str) or not d.strip():
        raise _fail(path, where, "overlay_dir must be a non-empty string")
    if tenant:
        d = _tenant_dir(d, path)
    if os.path.exists(d) and not os.path.isdir(d):
        raise _fail(path, where, f"overlay_dir {d!r} exists but is not a directory")
    return Prompts(d)


_SECTIONS = {"decay", "projects", "importance", "concepts", "taxonomy", "prompts"}


def _parse(data: dict, path: str, tenant: bool = False) -> Policy:
    """`tenant`: a non-owner tenant's file. No [prompts] section then means NO overlay (the
    owner's default `prompts.local` is cwd-relative, i.e. the owner's own), and a relative
    overlay_dir resolves beside the file."""
    unknown = sorted(set(data) - _SECTIONS)
    if unknown:
        raise PolicyError(f"{path}: unknown section(s) {unknown} "
                          f"(allowed: {sorted(_SECTIONS)})")
    raw_rules = data.get("decay", [])
    if not isinstance(raw_rules, list):
        raise _fail(path, "[[decay]]", "must be an array of tables")
    rules = tuple(_rule(i, r, path) for i, r in enumerate(raw_rules))
    names = [r.name for r in rules]
    if len(set(names)) != len(names):
        raise _fail(path, "[[decay]]", f"rule names must be unique: {names}")
    return Policy(
        decay=rules,
        projects=_projects(data["projects"], path) if "projects" in data else None,
        importance=_importance(data["importance"], path) if "importance" in data else Importance(),
        concepts=_concepts(data["concepts"], path) if "concepts" in data else Concepts(),
        taxonomy=_taxonomy(data["taxonomy"], path) if "taxonomy" in data
        else Taxonomy(DEFAULT_CATEGORIES),
        prompts=_prompts(data["prompts"], path, tenant) if "prompts" in data
        else Prompts(None if tenant else DEFAULT_OVERLAY_DIR),
        source_path=path,
    )


def _tenant_process(env: Mapping[str, str]) -> str | None:
    """The tenant slug this process serves, or None for the owner (KB_TENANT unset or
    "owner" — the kb-svc.sh contract)."""
    slug = (env.get("KB_TENANT") or "").strip()
    return slug if slug and slug != "owner" else None


def load_policy(env: Mapping[str, str] | None = None, path: str | None = None, *,
                tenant: bool = False) -> Policy:
    """Resolve and load the policy file. `path` wins, then KB_POLICY_CONFIG, then
    ./policy.toml. A missing DEFAULT file is the shipped DEFAULT_POLICY (one log line);
    a missing explicit file is a PolicyError.

    A tenant process (KB_TENANT=<slug>, not "owner") never takes the ./policy.toml
    fallback: cwd is KB_Core, where the OWNER's policy and prompts.local/ overlay live, so
    falling back would hand the owner's private prompt text to a tenant's jobs. It needs
    an explicit file (kb-svc.sh exports one) or the registry's policy (tenancy.policy_for).

    `tenant=True` (tenancy.policy_for for a non-owner; implied under KB_TENANT=<slug>) parses
    the file with tenant prompt rules: see _parse."""
    env = os.environ if env is None else env
    explicit = path or env.get(ENV_PATH)
    if not explicit and (slug := _tenant_process(env)) is not None:
        raise PolicyError(f"KB_TENANT={slug} is a tenant process: refusing the ./{DEFAULT_PATH} "
                          f"fallback (the owner's policy). Set {ENV_PATH} or build the "
                          "KnowledgeBase with tenancy.policy_for(tenant)")
    resolved = explicit or DEFAULT_PATH
    try:
        with open(resolved, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            how = "the path argument" if path else ENV_PATH
            raise PolicyError(f"policy file not found: {resolved} (set by {how}) — copy "
                              "policy.toml.example or unset it for the shipped defaults") from None
        print("policy: shipped defaults (no policy.toml)", file=sys.stderr, flush=True)
        return DEFAULT_POLICY
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{resolved}: not valid TOML: {exc}") from None
    return _parse(data, resolved, tenant or _tenant_process(env) is not None)


# ---- kb-policy CLI ---------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kb-policy",
                                description="Show or validate the per-install policy.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the effective policy and where it came from")
    chk = sub.add_parser("check", help="validate a policy file without starting anything")
    chk.add_argument("path", nargs="?", default=None,
                     help="file to validate (default: KB_POLICY_CONFIG or ./policy.toml)")
    args = p.parse_args(argv)
    if args.cmd == "check":
        try:
            pol = load_policy(path=args.path)
            # lazy: prompts imports policy (avoid import cycle)
            from kb_core.ingest.llm import load_passes
            load_passes(policy=pol)
            from kb_core.concept_judge import SYSTEM_FILE as CONCEPT_SYSTEM_FILE
            from kb_core.meaning_judge import SYSTEM_FILE as MEANING_SYSTEM_FILE
            from kb_core.prompts import PromptResolver, prompts_base_dir
            resolver = PromptResolver(pol, prompts_base_dir())
            resolver.load(MEANING_SYSTEM_FILE)
            resolver.load(CONCEPT_SYSTEM_FILE)
            for name in resolver.overlays():
                resolver.load(name)
        except PolicyError as exc:
            print(f"kb-policy: {exc}", file=sys.stderr)
            return 1
        print(f"ok: {pol.source_path or 'shipped defaults (no file)'}")
        return 0
    try:
        pol = load_policy()
    except PolicyError as exc:
        print(f"kb-policy: {exc}", file=sys.stderr)
        return 1
    print(f"source: {pol.source_path or 'shipped defaults (no policy.toml)'}")
    from kb_core.prompts import PromptResolver, prompts_base_dir   # lazy: prompts imports policy
    overlays = PromptResolver(pol, prompts_base_dir()).overlays()
    print(f"overlays: {', '.join(overlays) if overlays else '(none)'}")
    print(json.dumps(pol.to_dict(), indent=2))
    return 0
