# Contributing to ego-OS

Thanks for taking a look. Issues, bug reports, questions and pull requests are all
welcome. Please read [How this repository works](#how-this-repository-works) first,
because pull requests don't take the usual path. Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md).

## How this repository works

This public repository, **Ego-OpenSource**, is a **generated mirror** of ego-OS's
private upstream repository, where all development happens. Every commit here is a
`sync:` snapshot produced by an allowlist export (see `publish.manifest.toml`); the
`upstream <sha>` in its message refers to the private history.

- **Issues** are the main conversation surface and work normally.
- **Pull requests** are welcome but are never merged with the merge button. See
  [What happens to your PR](#what-happens-to-your-pr).
- History is snapshot-based: `git blame` spans whole sync commits.

## Running the tests

From the repository root (each line is self-contained, so you can paste the block):

```bash
(cd KB_Core && uv run pytest)        # engine + dashboard API
(cd KB_Retrieval && uv run pytest)   # needs its own test DB, see docs/operations.md
(cd KB_MCP && uv run pytest)         # MCP server
(cd KB_Agent && uv run pytest)
(cd KB_Inference && uv run --no-project --with-requirements requirements.txt python -m pytest)
(cd KB_Frontend && npm ci && npm test)
uv run --project KB_MCP python -m pytest evals/retrieval/tests   # one evals/<name>/tests dir per run
```

The Python suites need the Postgres test database that `docker compose up -d` (in
`KB_Core/`) creates on first init. They truncate their tables, so never point
`KB_TEST_DATABASE_URL` at real data. Details, including `KB_Retrieval`'s separate
database: [docs/operations.md](docs/operations.md#tests-and-ci).

CI (`.github/workflows/ci.yml`) runs one script per job, and you can run the same
scripts locally:

| Job | Command |
|---|---|
| Lint | `scripts/ci/lint.sh` (ruff, config in `ruff.toml`) |
| Python suites + golden-set structure check | `KB_TEST_DATABASE_URL=<a disposable test DB> scripts/ci/python.sh` |
| KB_Inference | `scripts/ci/kb-inference.sh` |
| Eval harness tests | `scripts/ci/evals-tests.sh` |
| Frontend | `scripts/ci/frontend.sh` |
| Secret scan | `scripts/ci/gitleaks.sh` |

## Ground rules

- `scripts/ci/lint.sh` must pass, and CI must be green.
- Add a test for new behaviour, and keep to the surrounding style.
- No personal data in code, fixtures, screenshots or docs. Invent names and content
  ([`examples/demo-vault/`](examples/demo-vault/) and the eval goldens use the invented
  "Orchard Labs" world); don't rename real data.
- If you add, move or remove an LLM call, update
  [`docs/llm-inference-map.md`](docs/llm-inference-map.md) in the same change.
- Security issues go through private vulnerability reporting, not issues: see
  [SECURITY.md](SECURITY.md).

## What happens to your PR

1. A maintainer reviews it here, in the PR thread.
2. If it is accepted, the maintainer imports your patch into the private upstream with a
   tool that applies it with `git am`, so the upstream commit keeps you as its author.
   It then lands on the upstream main branch.
3. The next publish ships it in a `sync:` commit here. That commit credits you with a
   `Co-authored-by:` trailer using your GitHub noreply address.
4. Your PR is then **closed, not merged**, with a link to that sync commit.

The import is deliberately strict, because it is the only way outside code reaches the
private repository:

- A PR may only touch files that are part of this public mirror.
- **Control files need a maintainer to re-do the change**: dependencies and lockfiles,
  `pyproject.toml` / `package.json` scripts and entry points, CI workflows,
  `scripts/ci/`, shell scripts (`*.sh`), `conftest.py`, `publish.manifest.toml`, and
  executable-bit changes. Open an issue first, or expect the change to be re-implemented
  rather than imported.
- Symlinks, submodules and binary files (images included) can't be imported.
- If upstream changed the same files since the last publish, you may be asked to rebase
  on this repository's main after the next sync.

[docs/publishing.md](docs/publishing.md) describes the whole mechanism: what gets
published, the checks every sync passes, and the import tool step by step.

Contributions are licensed under the Apache License 2.0, as section 5 of the
[license](LICENSE) describes.
