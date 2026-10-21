"""Migration numbers stay unique: one 0048 pair is grandfathered, no new duplicates.

The runner keys on the full file stem (see db.migrate), so a shared prefix still
applies -- but it reads like a merge accident and makes "the next number" ambiguous.
"""
from collections import Counter

from kb_core.db import MIGRATIONS_DIR

# Two branches both took 0048 before merging. Never rename them: the stem is the
# version key every existing database has already recorded.
GRANDFATHERED = {"0048": {"0048_llm_jobs", "0048_mcp_trace_turns"}}


def test_no_new_duplicate_migration_prefixes():
    stems = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    counts = Counter(stem.split("_", 1)[0] for stem in stems)
    for prefix, n in counts.items():
        if n == 1:
            continue
        shared = {s for s in stems if s.split("_", 1)[0] == prefix}
        assert shared == GRANDFATHERED.get(prefix), (
            f"migration prefix {prefix} is used by {sorted(shared)}: give the new "
            f"migration the next free number")


def test_every_migration_has_a_four_digit_prefix():
    for p in MIGRATIONS_DIR.glob("*.sql"):
        prefix = p.stem.split("_", 1)[0]
        assert len(prefix) == 4 and prefix.isdigit(), p.name
