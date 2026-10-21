<!--
This repository is a published mirror: pull requests are not merged with the merge
button. A maintainer imports the patch upstream and it ships in the next sync commit,
crediting you with a Co-authored-by trailer; this PR is then closed with a link to it.
Changes to dependencies, lockfiles, CI, shell scripts or conftest.py are re-done by a
maintainer rather than imported. See CONTRIBUTING.md, "What happens to your PR".
-->

## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## How it was tested

<!-- Which suites you ran (see CONTRIBUTING.md, "Running the tests"), and anything you checked by hand. -->

## Checklist

- [ ] Tests pass for every package I touched, and new behaviour has a test.
- [ ] `scripts/ci/lint.sh` is clean for Python changes.
- [ ] No personal data in code, fixtures, screenshots or docs (placeholder identities only).
- [ ] If this adds, moves or removes an LLM inference call, `docs/llm-inference-map.md` is updated.
- [ ] Docs updated where behaviour or configuration changed.
