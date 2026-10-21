# Per-install policy: setup guide

The KB ships **mechanisms** — content-age decay, project dormancy, ranking recency, an
entity taxonomy, prompt examples — with neutral defaults that make the owner-specific
ones inert. **Your** policy lives in `KB_Core/policy.toml` (gitignored, never exported)
and, for prompt wording, in `KB_Core/prompts.local/` (also gitignored). Nothing you put
there reaches the public mirror or other installs.

## Step 1: Create the file

```bash
cd KB_Core
cp policy.toml.example policy.toml
```

Every section is optional. Delete the ones you do not want; an absent section keeps the
shipped default named below. The daemons read the file once at start.

## Step 2: Edit the sections you want

### `[[decay]]` — which memories fade, and how fast

Each rule scopes a set of memories by `tags` (all listed must be present), `path_prefix`
(the memory's folder path, ancestor-or-self, e.g. `Obsidian.Journal`), and/or `source`
(the ingest source name). The **first** rule that matches a memory wins, in file order.
A matched, dated memory's influence in concept clustering and entity promotion decays
from 1.0 toward `floor` with `half_life_days`. Undated and unmatched memories never
decay. Retrieval is never affected. Shipped default: no rules.

```toml
[[decay]]
name = "newsletters"
match = { tags = ["newsletter"] }
half_life_days = 30
floor = 0.1
```

The dashboard's **Age decay** switch (concepts pane) turns all rules off and on at
runtime without a restart.

### `[projects]` — a folder boost that fades while a project is quiet

If a folder carries a manual importance boost, its descendants inherit it. With this
section, docs under a *project* — the subtree opened by `folder_label` at depth
`0..max_depth`, e.g. `Obsidian.Projects.<repo>` — lose that inherited boost with a
half-life of `dormancy_half_life_days` of no doc changes in the project. Shipped default:
section absent, no dormancy decay.

### `[importance]` — retrieval recency

Search multiplies a memory's boost above 1.0 by a recency factor that decays from 1.0 to
`recency_floor` with `recency_half_life_days` since the memory was last retrieved.
`access_saturation` is the access count at which the access factor reaches 0.5.
Shipped defaults: 8 days, 0.65, 10.

### `[concepts]` — goal priority windows

`goal_window_days` decides which concepts count as *current*; the two half-lives and the
floor shape the priority ordering. Shipped defaults: 14, 14, 7, 0.2.

### `[taxonomy]` — categories offered to the extraction prompt

`categories` are rendered verbatim into the entity-extraction prompt as "fixed broad
categories" and seed each category's structural `kind` (`person`, `organization`,
`place`, `event`, `project`, `artifact`, `concept`, `system`, `other`). `kind_seeds` map
further well-known names to kinds. A category's seed always wins over the model's own
kind guess. Shipped default: the eight structural kinds themselves.

### `[prompts]` — overlay directory

Any file in `overlay_dir` (default `prompts.local` when this file exists) shadows the
shipped `prompts/` file of the same name. Shipped prompts pull their worked examples
from **partials** (`entities.concept_examples.md`, `entities.symbol_examples.md`,
`entities.confidence_examples.md`, `discover_classes.type_examples.md`,
`discover_classes.examples.md`, `meaning_judge.examples.md`), so to change the examples
copy just that partial into `prompts.local/` and edit it; to change a whole prompt copy
the whole file. A prompt may include a partial with `{{include:NAME}}` and render the
taxonomy with `{{taxonomy}}`.

## Step 3: Validate, restart, verify

Run these from `KB_Core/` — `kb-policy` reads `./policy.toml` (or `KB_POLICY_CONFIG`)
relative to the working directory:

```bash
uv run kb-policy check          # validates policy.toml (exit 1 with the problem)
./scripts/kb-dev.sh restart     # macOS launchd fleet; if you run kb-run, stop and restart it instead
uv run kb-policy show           # source, overlays, and the effective policy as JSON
```

The dashboard's **Settings → Policy** tab shows the same view. A bad file stops every
daemon at start with the file, key, and fix in its log — validate first.

## Pointing elsewhere

`KB_POLICY_CONFIG=/path/to/policy.toml` in `KB_Core/.env` overrides the location. When
set and missing, that is an error (an explicit path that does not exist is a
misconfiguration); when unset and `./policy.toml` is missing, the shipped defaults apply.
