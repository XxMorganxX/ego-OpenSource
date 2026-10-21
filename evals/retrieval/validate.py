#!/usr/bin/env python3
"""Validate golden.jsonl against the schema and the live kb_mcp database.

Hard failures (exit 1): malformed JSON, missing/invalid required fields, duplicate
item ids, referenced memory/entity uuids that don't exist, entity names that don't
match their id's canonical_name, concepts named that aren't in the active catalog,
doc_kind filter values that don't exist in the corpus.
Warnings: path/title drift on memories (id still valid — the note moved or was
retitled since authoring).

Two sets live here and they are validated under different rules, selected by
filename: a name containing "package" is the SET-PACKAGE set (`golden_package.jsonl`)
and gets `PACKAGE_ARCHETYPES` plus the package authoring requirements (`exhaustive`,
`provenance`); anything else is the flat set and is checked exactly as before.
A third rulebook exists for `evals/mcp_surface/golden.jsonl`, selected by parent
directory name (`mcp_surface`): the flat checks plus `mcp_surface_checks` (ms-NNN
ids, per-item negatives, the new-content and retired-note floors).

Usage: [KB_DATABASE_URL=…] python3 validate.py [golden.jsonl] [--against other.jsonl] [--static]
`--against` adds the cross-set checks (disjoint ids/requests, the against-the-grain
sampling floor, dead-note coverage). `--static` skips every DB-backed check (id
existence, name/path/doc_kind drift, negatives-exist, mcp_surface coverage floors,
--against) and validates shape only — for environments with no live kb_mcp DB, e.g.
CI validating the synthetic golden sets. Run with KB_Core's venv python (needs psycopg).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ARCHETYPES = {"solved-problem", "entity-activity", "meetings", "org-activity",
              "goals", "stakeholders", "person-facts", "concept-browse", "facet"}
# SET-PACKAGE: questions about what BELONGS TOGETHER rather than
# what matches a phrasing. Deliberately disjoint from ARCHETYPES so a mis-filed item
# fails loudly in whichever file it lands in.
PACKAGE_ARCHETYPES = {"aboutness", "current-state", "entity-package", "relational"}
# evals/mcp_surface/golden.jsonl:
# flat-style items graded on documents-collected by a Claude Code session. Starts as
# a copy of ARCHETYPES; authoring may add shapes here without touching the flat set's.
MCP_SURFACE_ARCHETYPES = set(ARCHETYPES)
MCP_SURFACE_CUTOFF = datetime(2026, 7, 19, tzinfo=timezone.utc)
MCP_SURFACE_ID_RE = re.compile(r"^ms-\d{3}$")
DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb_app@localhost:5433/kb_mcp")


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


def _must_ids(item: dict) -> list:
    """Distinct memory ids the item says a correct package MUST contain."""
    return sorted({m["id"] for m in item.get("expected", {}).get("memories", [])
                   if m.get("id") and m.get("must", True)})


def mcp_surface_checks(items, meta, static=False):
    """Set rules for the mcp_surface set. Pure — testable without a DB.

    meta maps every referenced expected-memory id to {"ingested_at": dt|None,
    "dead_at": dt|None} (from the same fetch the drift checks use). Floors count
    must-gold only. `static=True` (validate.py's --static) skips the two
    DB-derived coverage floors below and is expected to be called with
    `meta={}` there — id-format, negatives-required and the per-item
    retired_ok check are pure and unaffected by an empty meta (every item's
    `metas` comes back [], so `dead` is always [] too — a synthetic set just
    should not set `retired_ok` on any item, since that per-item check needs
    real dead-note metadata to satisfy). Returns (errors, warnings).
    """
    errors, warnings = [], []
    new_items = retired_items = 0
    for it in items:
        iid = it.get("id", "<no id>")
        if not MCP_SURFACE_ID_RE.match(iid or ""):
            errors.append(f"{iid}: id must match ms-NNN")
        if not it.get("negatives"):
            errors.append(f"{iid}: mcp_surface items require >=1 negative")
        metas = [meta[m] for m in _must_ids(it) if m in meta]
        if any(m["ingested_at"] and m["ingested_at"] > MCP_SURFACE_CUTOFF
               for m in metas):
            new_items += 1
        dead = [m for m in metas if m["dead_at"] is not None]
        if it.get("retired_ok") is True:
            if dead:
                retired_items += 1
            else:
                errors.append(f"{iid}: retired_ok but no retired must-gold")
        elif dead:
            warnings.append(f"{iid}: gold memory retired after authoring and item "
                            f"lacks retired_ok — re-author or mark deliberate")
    if static:
        return errors, warnings
    if items and new_items / len(items) < 0.40:
        errors.append(f"only {new_items}/{len(items)} items carry "
                      f"post-{MCP_SURFACE_CUTOFF:%Y-%m-%d} must-gold — this set exists to cover new "
                      f"content (floor 40%)")
    if retired_items < 3:
        errors.append(f"only {retired_items} retired-note items (retired_ok with "
                      f"retired must-gold) — the set requires >=3")
    return errors, warnings


def cross_checks(items, against_path, conn) -> tuple[list, list]:
    """Disjointness + the package authoring rules, run only when --against is passed.

    ERRORS: overlapping item ids or duplicate request text (case/space-normalized)
    across the two sets; a must-gold pool that hugs the concept layer; a set with no
    dead-note gold anywhere. WARN: an item whose must-set *is* an active concept's
    membership — such an item scores the catalog, not retrieval.
    """
    errors, warnings = [], []
    other = [json.loads(line) for line in Path(against_path).read_text().splitlines()
             if line.strip()]
    mine, theirs = {i["id"] for i in items}, {i["id"] for i in other}
    if mine & theirs:
        errors.append(f"item ids overlap with {against_path}: {sorted(mine & theirs)}")

    def norm(s):
        return " ".join((s or "").lower().split())

    dupes = {norm(i.get("request")) for i in items} & \
            {norm(i.get("request")) for i in other}
    if dupes:
        errors.append(f"request text duplicated across sets: {sorted(dupes)[:3]}…")

    must_ids = sorted({mid for it in items for mid in _must_ids(it)})
    covered = {str(r["node_id"]) for r in conn.execute(
        "SELECT DISTINCT cm.node_id FROM concept_members cm "
        "JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active' "
        "WHERE cm.member_kind = 'memory' AND cm.node_id = ANY(%s::uuid[])",
        (must_ids,))} if must_ids else set()
    outside = len(must_ids) - len(covered)
    # Keep this pooled floor exactly as-is (the rule as written): it does not distinguish
    # *why* an id sits outside the concept layer, only that enough of them do.
    if must_ids and outside / len(must_ids) < 0.30:
        errors.append(
            f"only {outside}/{len(must_ids)} must-gold ids sit outside the active "
            f"concept layer — the package rules require sampling against the grain; re-author")
    must_set = set(must_ids)
    dead = {str(r["id"]) for r in conn.execute(
        "SELECT id FROM nodes WHERE dead_at IS NOT NULL AND id = ANY(%s::uuid[])",
        (must_ids,))} if must_ids else set()
    if not dead:
        errors.append("no dead-note gold anywhere in the set — the package rules require it "
                      "(the crawl's concept arm is structurally blind to retired "
                      "notes; that blindness must be measurable)")
    outside_ids = must_set - covered
    live_outside = outside_ids - dead
    dead_outside = outside_ids & dead
    print(f"cross-set: {outside}/{len(must_ids)} distinct must-gold ids outside the "
          f"active concept layer ({100 * outside / max(len(must_ids), 1):.0f}%), "
          f"{len(dead)} dead")
    print(f"outside active concepts: {outside}/{len(must_ids)} total "
          f"(live_outside={len(live_outside)}, dead_outside={len(dead_outside)})")
    if len(live_outside) < 5:
        warnings.append(
            "against-the-grain floor is carried almost entirely by retired notes "
            f"(live_outside={len(live_outside)}) — the two blindnesses (unclustered "
            "vs retired) cannot substitute for each other; noted for the report")

    # Flag (WARN) any item whose must-set equals an active concept's member list —
    # such an item scores the catalog, not retrieval.
    for it in items:
        must = _must_ids(it)
        if not must:
            continue
        hit = conn.execute(
            "SELECT c.name FROM concepts c WHERE c.status='active' AND NOT EXISTS ("
            " SELECT 1 FROM concept_members cm WHERE cm.concept_id=c.id "
            "  AND cm.member_kind='memory' AND cm.node_id::text != ALL(%s)) "
            "AND (SELECT count(*) FROM concept_members cm2 WHERE cm2.concept_id=c.id "
            "     AND cm2.member_kind='memory') = %s",
            (must, len(must))).fetchone()
        if hit:
            warnings.append(f"{it['id']}: must-gold equals active concept "
                            f"{hit['name']!r}'s member list — flagged per the package rules")
    return errors, warnings


def main(path: str, against: str | None = None, static: bool = False) -> int:
    items = []
    for i, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"FAIL line {i}: bad JSON ({exc})")
            return 1

    # The file decides the rulebook. `run.py`/`bench.py` call this with a bare path
    # and no flags, so the flat set must keep behaving exactly as it did.
    is_package = "package" in Path(path).name
    is_mcp_surface = Path(path).resolve().parent.name == "mcp_surface"

    errors, warnings = [], []
    ids = [it.get("id") for it in items]
    if len(ids) != len(set(ids)):
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        errors.append(f"duplicate item ids: {dupes}")

    mem_refs, ent_refs, concept_refs, kind_refs = [], [], set(), set()
    neg_refs = []   # negatives may live in ANY id space (memory, entity, concept)
    for it in items:
        iid = it.get("id", "<no id>")
        for field in ("id", "archetype", "request", "expected", "judgment"):
            if not it.get(field):
                errors.append(f"{iid}: missing {field}")
        table = (PACKAGE_ARCHETYPES if is_package
                 else MCP_SURFACE_ARCHETYPES if is_mcp_surface else ARCHETYPES)
        if it.get("archetype") not in table:
            errors.append(f"{iid}: unknown archetype {it.get('archetype')!r}")
        if is_package:
            # Precision is scored on this set, and precision only means something
            # if the labels claim to cover every note that belongs.
            if it.get("exhaustive") is not True:
                errors.append(f"{iid}: SET-PACKAGE labels must be exhaustive — "
                              f"precision is scored here")
            prov = it.get("provenance")
            if not isinstance(prov, dict):
                errors.append(f"{iid}: missing provenance")
            else:
                # Gold built from the concept catalog would grade consolidation,
                # not retrieval — and would hand the crawl its own answer key.
                if prov.get("method") != "psql":
                    errors.append(f"{iid}: provenance.method must be 'psql', got "
                                  f"{prov.get('method')!r}")
                if prov.get("concepts_consulted") is not False:
                    errors.append(f"{iid}: provenance.concepts_consulted must be "
                                  f"false — gold is authored from note content")
        exp = it.get("expected", {})
        for m in exp.get("memories", []):
            mem_refs.append((iid, m))
        for e in exp.get("entities", []):
            ent_refs.append((iid, e))
        for c in exp.get("concepts", []):
            concept_refs.add((iid, c.get("name"), c.get("kind")))
        for n in it.get("negatives", []):
            if n.get("id"):
                neg_refs.append((iid, n))
        for step in it.get("tool_plan", []):
            k = (step.get("args") or {}).get("kind")
            if k:
                kind_refs.add((iid, k))
        if not exp.get("memories") and not exp.get("entities") and not exp.get("concepts"):
            errors.append(f"{iid}: expected is empty")

    if static:
        if is_mcp_surface:
            se, sw = mcp_surface_checks(items, {}, static=True)
            errors.extend(se)
            warnings.extend(sw)
        if against:
            warnings.append("--against skipped under --static (needs a live DB)")
        by_arch: dict = {}
        for it in items:
            by_arch[it.get("archetype")] = by_arch.get(it.get("archetype"), 0) + 1
        print(f"{len(items)} items | archetypes: "
              + ", ".join(f"{k}={v}" for k, v in sorted(by_arch.items())))
        print(f"{len(mem_refs)} memory refs, {len(ent_refs)} entity refs, "
              f"{len(concept_refs)} concept refs")
        print("STATIC MODE: DB-backed checks skipped (id existence, name/path/doc_kind "
              "drift, negatives-exist, mcp_surface coverage floors, --against)")
        for w in warnings:
            print(f"WARN  {w}")
        for e in errors:
            print(f"ERROR {e}")
        print(f"{len(warnings)} warnings, {len(errors)} errors")
        return 1 if errors else 0

    retired_pkg: list = []   # package set only: retirement is a requirement, not drift
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        mem_ids = list({m["id"] for _, m in mem_refs if m.get("id")})
        found = {str(r["id"]): r for r in conn.execute(
            "SELECT id, path::text AS path, title, dead_at, ingested_at "
            "FROM nodes WHERE id = ANY(%s::uuid[])",
            (mem_ids,))} if mem_ids else {}
        for iid, m in mem_refs:
            row = found.get(m.get("id"))
            if row is None:
                errors.append(f"{iid}: memory {m.get('id')} not in DB")
                continue
            # Gold authored before a note was retired keeps demanding it, which reads
            # as a recall regression when it is really the lifecycle working. Real and
            # recurring: an item whose gold memories are retired after authoring shows an
            # apparent drop that is entirely the lifecycle, not retrieval.
            if row["dead_at"] is not None:
                if is_package:
                    # The package rules order dead notes into this set on purpose (a package
                    # question about what "was part of X" is unanswerable without
                    # them). Telling the author to re-author each one would be
                    # advice against the rules, so summarise instead of nagging.
                    retired_pkg.append(iid)
                elif is_mcp_surface:
                    pass
                else:
                    warnings.append(
                        f"{iid}: gold memory {m.get('id')} was RETIRED "
                        f"({row['dead_at']:%Y-%m-%d}) — re-author the item or expect "
                        f"it to score as a miss for current-state questions")
            if m.get("path") and m["path"] != row["path"]:
                warnings.append(f"{iid}: path drift {m['id']}: {m['path']} -> {row['path']}")
            if m.get("title") and m["title"] != (row["title"] or ""):
                warnings.append(f"{iid}: title drift {m['id']}")

        # Entities are referenced by canonical_name, like concepts and for the same
        # reason: ids churn. kb-reset-entities rebuilds the layer wholesale, so an id
        # recorded at authoring time says nothing a week later.
        #
        # A missing entity is a WARNING, not an error. The entity layer is emergent —
        # instances promote on recurrence (>= 3 distinct memories) and curation can
        # block one outright — so a low-frequency person legitimately absent today is
        # the gate working, not the fixture rotting. Memory ids are different: they
        # are uuid5 of source:external_id and survive moves and re-syncs, so a missing
        # one really does mean the gold is stale. Only that fails the check.
        live = {r["canonical_name"] for r in conn.execute(
            "SELECT canonical_name FROM entities WHERE status = 'active'")}
        live_ci = {n.lower(): n for n in live}
        for iid, e in ent_refs:
            name = e.get("name")
            if not name:
                errors.append(f"{iid}: entity ref has no name")
            elif name in live:
                continue
            elif name.lower() in live_ci:
                errors.append(f"{iid}: entity case drift {name!r} -> "
                              f"{live_ci[name.lower()]!r}")
            else:
                warnings.append(f"{iid}: no active entity named {name!r} "
                                f"(below the recurrence gate, blocked, or merged away)")

        active = {(r["name"], r["kind"]) for r in conn.execute(
            "SELECT name, kind FROM concepts WHERE status = 'active'")}
        # Same rule as entities: concepts are a DERIVED layer, recomputed by every
        # consolidation run (com.kb.consolidate is on a timer), so drift here is
        # expected churn rather than stale gold. NOTHING about a concept is an error.
        #
        # `kind` is not an error even though a surviving name under a new kind looks
        # like a cheap mechanical fix: consolidation can reclassify a concept's kind
        # between runs, so pinning it just makes the suite fail on a schedule. Scoring matches on NAME alone and
        # kind is advisory — see run.py.
        by_name = {}
        for n, k in active:
            by_name.setdefault(n, set()).add(k)
        for iid, name, kind in concept_refs:
            if name not in by_name:
                warnings.append(
                    f"{iid}: concept {name!r} no longer in the active catalog "
                    f"(renamed or absorbed by a consolidation run)")
            elif kind not in by_name[name]:
                warnings.append(f"{iid}: concept {name!r} reclassified {kind!r} -> "
                                f"{sorted(by_name[name])} (kind is advisory)")

        kinds = {r["doc_kind"] for r in conn.execute(
            "SELECT DISTINCT doc_kind FROM nodes WHERE doc_kind IS NOT NULL")}
        for iid, k in kind_refs:
            if k not in kinds:
                errors.append(f"{iid}: doc_kind {k!r} not present in corpus")

        # Negatives: valid if the id exists in nodes (memory/entity) OR concepts —
        # a distractor can be a retired duplicate concept just as well as a memory.
        neg_ids = list({n["id"] for _, n in neg_refs})
        if neg_ids:
            node_hits = {str(r["id"]) for r in conn.execute(
                "SELECT id FROM nodes WHERE id = ANY(%s::uuid[])", (neg_ids,))}
            concept_hits = {str(r["id"]) for r in conn.execute(
                "SELECT id FROM concepts WHERE id = ANY(%s::uuid[])", (neg_ids,))}
            for iid, n in neg_refs:
                if n["id"] not in node_hits and n["id"] not in concept_hits:
                    errors.append(f"{iid}: negative {n['id']} in neither nodes "
                                  "nor concepts")

        if retired_pkg:
            n, items_with = len(retired_pkg), sorted(set(retired_pkg))
            print(f"{n} retired gold memories across {len(items_with)} items "
                  f"({', '.join(items_with)}) — expected: the package rules require the set to "
                  f"reach notes the lifecycle has retired")

        if is_mcp_surface:
            meta = {mid: {"ingested_at": row["ingested_at"], "dead_at": row["dead_at"]}
                    for mid, row in found.items()}
            se, sw = mcp_surface_checks(items, meta)
            errors.extend(se)
            warnings.extend(sw)

        if against:
            xe, xw = cross_checks(items, against, conn)
            errors.extend(xe)
            warnings.extend(xw)

    by_arch: dict = {}
    for it in items:
        by_arch[it.get("archetype")] = by_arch.get(it.get("archetype"), 0) + 1
    print(f"{len(items)} items | archetypes: "
          + ", ".join(f"{k}={v}" for k, v in sorted(by_arch.items())))
    print(f"{len(mem_refs)} memory refs, {len(ent_refs)} entity refs, "
          f"{len(concept_refs)} concept refs")
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"{len(warnings)} warnings, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    # Hand-rolled rather than argparse: the existing callers (run.py, bench.py) pass
    # a bare path and depend on this staying a positional-only interface.
    argv = sys.argv[1:]
    _static = "--static" in argv
    if _static:
        argv = [a for a in argv if a != "--static"]
    _against = None
    if "--against" in argv:
        i = argv.index("--against")
        try:
            _against = argv[i + 1]
        except IndexError:
            sys.stderr.write("--against needs a path\n")
            sys.exit(2)
        argv = argv[:i] + argv[i + 2:]
    sys.exit(main(argv[0] if argv else str(_eval_data(
                      "retrieval/golden.jsonl", Path(__file__).parent / "golden.jsonl")),
                  _against, static=_static))
