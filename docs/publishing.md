# How this repository is published

This repository is an **allowlist mirror** of a private development repository. All
development, review and CI happen upstream; every commit here is a `sync:` snapshot
produced by one tool, `kb-publish`. Community pull requests travel the other way through
a second tool, `kb-import-pr`. Both live in this tree, under
[`KB_Core/src/kb_core/publish/`](../KB_Core/src/kb_core/publish/), with their tests in
`KB_Core/tests/test_publish_*.py`, so you can read exactly what decides what is public.

## What gets published

Three layers decide whether a file reaches this repository. A file must pass all three.

1. **The manifest allowlist** ([`publish.manifest.toml`](../publish.manifest.toml)). A
   path is exported only if it matches an `include` pattern and no `exclude` pattern.
   Anything new at the top level is private until the manifest names it. Images, PDFs and
   video need a second, explicit `allow_media` entry, and any other file that is not
   strict UTF-8 needs `allow_binary`.
2. **Rules in code** (`manifest.hard_deny_reason`). Some paths are never exported,
   whatever the manifest says: `private/` directories, scratch and temp directories,
   `.env` and key files, databases and dumps, agent-policy files. An edit to the TOML
   cannot turn these off.
3. **Content scans.** Every exported file is scanned against a denylist of identifiers
   (names, hosts, paths) that lives only upstream, plus structural detectors that refuse
   the *shape* of private data even when no pattern names it: email addresses outside
   reserved domains, real home directories, routable IP addresses, phone numbers,
   archives and other containers (detected by magic bytes, not just suffix), non-UTF-8
   text, and obfuscated forms (homoglyphs, invisible characters, HTML entities, `%XX`
   escapes, base64, split string literals). Path names are scanned too.

The upstream CI runs the same checks on every push with `kb-check-publishable --strict`.
In this repository and in forks there is no denylist, so `kb-check-publishable` reports
a loud **skip**, not a pass.

## Path approval

The manifest selects with coarse globs such as `KB_Core/**`, so on its own any new file
under those directories would export on the next run. A **path lockfile** upstream
records every path a human has approved:

- A path that is not in the lockfile is refused until it is approved. Files whose risk
  is in their data (`*.jsonl`, eval and fixture `*.json`, every exported `*.toml`, and
  anything that is not UTF-8) are pinned by content hash, so changing their bytes needs
  approval again.
- `kb-publish approve` is the only command that writes the lockfile. It shows each new
  or changed path (its size and first lines, or for binary content its detected type
  and printable strings) and asks for confirmation on a terminal. Without a terminal it
  refuses; there is no non-interactive way to approve, so a script or an AI agent
  can't approve paths on a human's behalf.
- The approval that counts is the one committed at the ref being published.

## Gate files and acknowledgements

Some files define the gate itself: `publish.manifest.toml`, the denylist,
`.github/**`, `scripts/ci/**`, `KB_Core/src/kb_core/publish/**`,
`KB_Core/src/kb_core/__init__.py`, `KB_Core/pyproject.toml`, `KB_Core/uv.lock`,
`.gitattributes`, gitleaks config, the upstream pre-push hook, the path lockfile, and
anything Python runs at start-up (`sitecustomize.py`, `usercustomize.py`, `*.pth`).
The lockfile is on the list so that every newly approved path is shown and acknowledged
at publish time, like any other gate change.
A change to any of them could weaken the very checks that judge it, so:

- every dry run prints the gate files changed since the last publish, with an **ack
  token**: a digest of exactly that change. A push must pass it back with
  `--ack-gate-changes <token>`. An ack when nothing changed is refused;
- the gate code that runs a push must be byte-identical to the gate code at the last
  published commit. A change to the gate is therefore judged, and its diff shown, by
  the previous gate that is already public. The publisher runs `kb-publish` from a
  non-editable install of that published commit.

## The publish flow

`kb-publish` is a dry run by default. A publish goes like this:

1. **CI decides.** The commit must be on the upstream `main` branch with every required
   check-run green (see `REQUIRED_CHECKS` in `ci_attest.py`). Each check-run must come
   from the expected workflow, triggered on `main`, for that exact sha. `kb-publish`
   asks GitHub for this itself instead of trusting branch protection.
2. **Dry run.** `kb-publish --ref <sha> --expect-sha <sha>` assembles the export from the
   committed blobs (not `git archive`, so `.gitattributes` cannot change a byte), runs
   every scan, and prints three diffs against what is already public: exported paths
   added and removed, the path-lockfile diff, and the gate-file diff with its ack token.
   It reads public `main` only from an explicit `--remote` or `KB_PUBLISH_REMOTE`, and
   never contacts the manifest's placeholder remote.
3. **Verify.** A copy of the export runs this repository's own CI: the same
   `scripts/ci/<job>.sh` scripts that `.github/workflows/ci.yml` runs, plus `gitleaks`.
   The tests use a dedicated scratch database (`KB_VERIFY_DATABASE_URL`, which must name
   a `kb_verify*` database), never a shared one. Whatever the tests write stays in the
   copy, and the tree that gets committed must still match the export byte for byte.
   An allowlist omission therefore breaks the publish, not this repository.
4. **Push.** `kb-publish --ref <sha> --expect-sha <sha> --push [--tag vX.Y.Z]
   [--ack-gate-changes <token>]` writes only to the repository recorded once with
   `kb-publish pin --repo owner/name`. Pinning records the repository's numeric GitHub
   id. Before anything is committed, the push re-checks that the target is public, has
   that id, is not a rename redirect, and is not the private repository. It then builds
   one `sync:` commit in a scrubbed git environment (no inherited config, hooks or
   signing). The commit is written by a neutral mirror identity and dated 00:00 UTC on
   the upstream commit's day. Its author, committer, message and tag are deny-scanned
   too, and it is pushed over ssh with a dedicated deploy key.
5. **Accept.** `kb-publish accept` makes a fresh read-only clone of this repository's
   branches and tags. It scans every file at `main`, every blob anywhere in history and
   every commit and tag object with the real denylist and the structural detectors.
   Any hit is a failure.

Other subcommands: `kb-publish export --ref REF --out DIR` builds and gates the export
without cloning or pushing, and `kb-publish diff` prints the path and gate diff against
what is public as Markdown for a CI job summary. `kb-publish --help` lists every flag.

## History model

This repository has its own history. It is not a filtered copy of the upstream history.

- Every commit on `main` is a `sync: <date> (upstream <sha7>)` snapshot of the whole
  exported tree, made by the mirror identity. The `upstream <sha7>` names the private
  commit it was built from. That sha is the only link between the two histories.
- A push refuses to build on anything else. If `main` holds a merge, a web edit or a
  commit by another identity, or names the same upstream twice, the push stops instead
  of silently reverting it. A publish may never roll back to an older upstream commit.
- `git blame` therefore spans whole sync commits.

## What happens to a pull request

Pull requests are welcome, but the merge button is never used. The path back upstream is
`kb-import-pr <number>`:

1. A maintainer reviews the PR here.
2. `kb-import-pr` reads the patch and checks it **before** applying anything:
   - every path it touches (both sides of a rename, deletions included) must be part of
     this mirror, judged by the manifest as it was *before* the patch;
   - **control files** are refused unless the maintainer passes `--allow-control-files`,
     which prints their full diff and still asks for confirmation. These are the gate
     files above, CI, agent policy, `conftest.py`, shell scripts, lockfiles, and
     dependency, script or entry-point changes in `pyproject.toml` and `package.json`;
   - symlinks, submodules, binary content and unsafe paths are always refused;
   - the added lines are deny-scanned and structurally scanned.
3. The patch is applied with `git am` (no three-way merge, no hooks) onto a new
   `import/pr-<n>` branch in its own worktree. The result is checked again from git's
   own diff. Nothing from the patch runs until a human has reviewed it.
4. The maintainer merges it upstream, and the next publish ships it. You are credited
   with a `Co-authored-by:` trailer on that sync commit. The name comes from GitHub's
   record of who opened the PR, not the patch's `From:` header, and the email is your
   GitHub noreply address.
5. After the push, `kb-publish` prints a `gh pr close` command for each imported PR. The
   PR is closed with a link to the sync commit that shipped it.

If upstream changed the same files since the last publish, the import says so, and you
may be asked to rebase after the next sync. [CONTRIBUTING](../CONTRIBUTING.md) has the
contributor-side view.

## Using the tooling in a fork

The tooling is generic. To publish your own allowlist mirror, edit
`publish.manifest.toml` and create your own `publish.deny.toml` (it must never be
exported, and the code refuses to export it). Then pin your public repository with
`kb-publish pin` and give the push a remote with `--remote` or `KB_PUBLISH_REMOTE`.
`[publish] remote` in the manifest is a placeholder: a dry run never contacts it, and
`--push` refuses to use it.
