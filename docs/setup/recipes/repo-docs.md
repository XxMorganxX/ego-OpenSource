# Recipe: mirror Markdown docs from your git repos (recommended)

Written to the agent. Implement this in the user's environment; nothing here
ships as code. Read the whole recipe, pick a mechanism with the user, build
it, run the backfill, then run the check.

## Why

The READMEs, design notes, plans, and specs inside the user's own repositories
are among the densest knowledge they produce, and they change as the work
changes. Mirroring them into the notes folder turns every repo into a live
source: the knowledge base sees the doc the moment it is written. Optionally,
its importance can fade while the project goes quiet (see "Project dormancy"
below; off by default).

## Contract

Given a notes folder `NOTES` (the guide's `KB_OBSIDIAN_VAULT`) and a set of
repositories the user chooses:

1. **Scope.** Mirror files matching `*.md` and `*.markdown` that are inside a
   git repository. The git-repo test is the gate that keeps loose scratch
   notes out — a Markdown file outside any repo is never mirrored.
2. **Destination.** `NOTES/Projects/<repo>/<path-relative-to-repo-root>`,
   where `<repo>` is the basename of the repository root. Create directories
   as needed.
3. **Real files, never symlinks.** The ingest daemon reads file content and
   writes an `id:` line into the copy's frontmatter; a symlink would push that
   write back into the repo.
4. **Frontmatter.** Prepend or merge a YAML block with exactly these keys,
   preserving any frontmatter the source already has (other keys untouched;
   these three overwritten):

   ```yaml
   ---
   source: /absolute/path/to/repo/docs/design.md
   project: <repo>
   synced_at: 2026-09-07T14:03:00Z
   ---
   ```

5. **Exclusions.** Skip any path containing a `node_modules`, `dist`, `build`,
   `vendor`, `.venv`, `venv`, `.next`, `target`, `.cache`, `.Trash`, or `.git`
   directory component. Skip a repository whose root is the notes folder or
   contains it (mirroring the notes into themselves loops).
6. **Deletes.** When a source file is deleted or renamed away, delete the
   mirrored copy so the memory is pruned. A rename inside the repo is a delete
   plus a create at the new path.
7. **Logging.** Append one line per mirror, skip, or error to a log file the
   user can inspect (for example `~/.kb-doc-sync.log`).

## Mechanisms — pick with the user

Choose by what the user actually runs. Only one is needed.

- **Agent-side write hook (lowest latency; Claude Code).** A `PostToolUse`
  hook on `Write|Edit|MultiEdit` that reads the hook's JSON on stdin, takes
  `tool_input.file_path`, applies the contract, and exits 0. Register it in
  `~/.claude/settings.json`:

  ```json
  { "hooks": { "PostToolUse": [ { "matcher": "Write|Edit|MultiEdit",
      "hooks": [ { "type": "command", "command": "node ~/.claude/hooks/kb-doc-sync.js" } ] } ] } }
  ```

  Only files the agent writes are mirrored; pair it with the backfill below
  for everything already there, and note that hand edits outside the agent are
  not seen until the next backfill. Hook changes apply to sessions started
  afterwards.
- **Git `post-commit` hook, per repo.** Runs `git diff-tree --no-commit-id
  --name-status -r HEAD -- '*.md' '*.markdown'` and applies the contract to
  each added/modified path, deleting mirrors for `D` entries. Sees every
  committed change regardless of editor; misses uncommitted work.
- **Periodic job.** A script that walks a list of repo paths and runs the
  backfill below, scheduled with cron, a launchd agent, or a systemd timer
  every 5–15 minutes. Simplest to reason about; latency equals the interval.
- **File watcher.** `fswatch`, `watchman`, or `inotifywait` on each repo root,
  applying the contract per event. Real-time and editor-agnostic; one more
  long-running process to keep alive.

Whatever the mechanism, put the contract in **one** function or script and call
it from both the live path and the backfill, so the two can never drift.

## Backfill (run once per repo, before the live mechanism)

For each repo:

```bash
git -C "$REPO" ls-files -z -- '*.md' '*.markdown' | while IFS= read -r -d '' f; do
  mirror_one "$REPO/$f"        # the contract function/script from above
done
```

`git ls-files` lists tracked files only, which already honours the repo's
`.gitignore`. Apply the exclusions in the contract anyway (a tracked
`vendor/README.md` still should not be mirrored).

## Check

```bash
NOTES=/absolute/path/to/notes                               # the guide's KB_OBSIDIAN_VAULT
find "$NOTES/Projects" -name '*.md' | wc -l                 # > 0 after the backfill
head -5 "$(find "$NOTES/Projects" -name '*.md' | head -1)"  # frontmatter with source/project/synced_at
```

Then, with the fleet running, within about a minute:

```bash
grep -E 'pull obsidian: seen=' "$HOME/kb-run.log" | tail -2     # changed > 0 on the tick after the backfill
```

Finally edit a doc in one repo through the live mechanism (write a line, or
commit, depending on what you built) and confirm the mirrored copy updates
within the mechanism's latency and the `id:` line the daemon stamped earlier
is still present in the copy.

## Project dormancy (optional)

The shipped policy does not fade anything. If the user gives the `Projects`
folder a manual importance boost, every mirrored doc inherits it; to have that
inherited boost fade while a project sees no doc changes, add a `[projects]`
section to `KB_Core/policy.toml` (see the
[policy setup guide](../../../KB_Core/docs/policy-setup.md)):

```toml
[projects]
folder_label = "Projects"           # the folder that opens a project subtree
max_depth = 1                       # it sits at depth 0..1, e.g. Obsidian.Projects.<repo>
dormancy_half_life_days = 182.5     # boost halves after ~6 months without doc changes
```

Validate with `uv run kb-policy check` from `KB_Core/`, then restart the
services. A rewrite that changes only `synced_at` is not project activity.

## Gotchas

- **The copy is disposable; the repo file is truth.** The ingest daemon stamps
  an `id:` into the mirrored copy's frontmatter. Never hand-edit anything under
  `NOTES/Projects/` — the next sync overwrites it. Tell the user this once.
- **Re-syncing is cheap.** The knowledge base ingests the Markdown *body*;
  frontmatter is metadata. A rewrite that changes only `synced_at` does not
  change the memory's content and does not trigger re-enrichment, so periodic
  full backfills are safe.
- **Preserve the stamped `id:` when you rewrite.** Merge frontmatter rather
  than replacing it, or the daemon re-stamps a new id and the memory's history
  is cut. Reading the existing copy's `id:` and carrying it forward is the
  simplest way.
- **If the notes folder is itself synced from a remote** (for example a vault
  that is `git pull`ed or hard-reset from another machine), the `Projects/`
  subtree is generated on this machine only — exclude it from that sync (for a
  git-managed folder, add `/Projects/` to `.git/info/exclude`) or every pull
  wipes the mirror.
- **Large repos.** Backfilling a monorepo with thousands of Markdown files
  goes straight into enrichment; with `claude-cli` routing that is quota. Start
  with one small repo.
