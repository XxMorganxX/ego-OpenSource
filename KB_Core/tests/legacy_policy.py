"""The owner's pre-policy-layer behaviour, spelled out as a Policy. The `kb` fixture
injects it so the existing suite keeps its expectations (email decays 17 d / 0.2, the
`Projects` label opens a project, the 17 curated categories seed kinds). Tests that need
the shipped defaults construct `Policy()` themselves."""
from types import MappingProxyType

from kb_core.policy import (Category, Concepts, DecayRule, Importance, Match, Policy,
                            Projects, Prompts, Taxonomy)

LEGACY_EMAIL_RULE = DecayRule("email", Match(tags=("email",)), 17.0, 0.2)

LEGACY_CATEGORIES = (
    Category("person", "person", "an individual human"),
    Category("organization", "organization",
             "a non-company institution, group, lab, club, nonprofit, or public body"),
    Category("company", "organization",
             "a commercial company, startup, client, vendor, sponsor, or employer"),
    Category("project", "project",
             "a bounded initiative, build effort, research direction, or engagement"),
    Category("plan", "project", "a named plan, design, proposal, roadmap, or planned initiative "
                                "with stable identity beyond one note"),
    Category("product", "system", "a named software product, model, service, or platform"),
    Category("tool", "system", "a framework, API, library, utility, or reusable technical tool"),
    Category("repository", "system", "a specific repository or codebase"),
    Category("research paper", "artifact", "a paper, publication, or named academic work"),
    Category("documentation", "artifact",
             "a specification, guide, README, API reference, or documentation set"),
    Category("system component", "system",
             "a named subsystem, service, pipeline, database, or architectural unit"),
    Category("dataset", "system", "a dataset, evaluation suite, benchmark, or test collection"),
    Category("ai agent", "system", "an autonomous or semi-autonomous agent, bot, or assistant"),
    Category("concept", "concept", "an abstract idea, method, principle, pattern, or problem area"),
    Category("skill", "concept", "a learned or desired ability or competency"),
    Category("event", "event",
             "a meeting, call, interview, conference, session, or other notable occurrence"),
    Category("location", "place", "a geographic place"),
)

LEGACY_KIND_SEEDS = MappingProxyType({
    "university": "organization", "problem area": "project", "artifact": "artifact",
    "document": "artifact", "pattern": "concept", "design principle": "concept",
    "method": "concept", "process": "concept", "architecture": "concept",
    "software component": "system", "platform": "system", "ai model": "system",
    "technology": "system", "database": "system", "framework": "system", "library": "system",
    "protocol": "system", "pipeline": "system", "command": "system", "interface": "system",
    "test suite": "system", "data format": "system", "benchmark": "system",
})

LEGACY_POLICY = Policy(
    decay=(LEGACY_EMAIL_RULE,),
    projects=Projects("Projects", 1, 182.5),
    importance=Importance(8.0, 0.65, 10.0),
    concepts=Concepts(14, 14.0, 7.0, 0.2),
    taxonomy=Taxonomy(LEGACY_CATEGORIES, LEGACY_KIND_SEEDS),
    prompts=Prompts(overlay_dir=None),          # tests never read prompts.local/
    source_path="<legacy fixture>",
)
