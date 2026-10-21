"""Layer-3 verify: the assembled export tree must scan clean, build, and pass
its own test suites BEFORE anything is pushed. An allowlist omission therefore
breaks the publish, never the public repo.

cli.run hands verify a COPY of the export tree: builds and tests write
files (node_modules, .pytest_cache, coverage), and none of them may reach the
commit. gitleaks is optional for a dry run on a box without it, but a push
passes require_gitleaks=True: the export-tree secret scan is not skippable.

Every step is one scripts/ci/<job>.sh — the same script the ci.yml job of that
name runs, and that publish-candidate's export-ci job runs inside the export on
a clean runner (the verify of record). Before, STEPS re-typed ci.yml by hand
and had silently lost KB_Agent, the second tenancy database and the eval tests.

The suites TRUNCATE their database, and they used to run against the shared
kb_mcp_test that every other test run on the machine also uses. verify now refuses unless
KB_VERIFY_DATABASE_URL names a database called kb_verify[_<suffix>] — one that
exists only for this — and hands the scripts that database (plus `<name>_b`,
which python.sh creates) instead of whatever KB_TEST_DATABASE_URL says.

Two verifies on the SAME scratch database would still corrupt each other, so
verify holds an exclusive lock named after that database for its whole run
and refuses if another holds it. It used to refuse whenever ANY pytest ran on
the machine — on a busy development machine, nearly always — although
nothing else may use a kb_verify* database.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

SCRATCH_DB_VAR = "KB_VERIFY_DATABASE_URL"
_SCRATCH_NAME = re.compile(r"^kb_verify(?:_[a-z0-9_]+)?$")

# The push session's credential and target. The suites are export code and run
# as CI runs them, without these: a test that resolved KB_PUBLISH_REMOTE fetched
# the real public repo with the deploy key (a passphrase prompt per test), and
# tests that expect no remote failed, so a --push could never pass verify.
PUBLISH_ONLY_VARS: tuple[str, ...] = (
    "GIT_SSH_COMMAND", "GIT_SSH", "SSH_AUTH_SOCK", "KB_PUBLISH_REMOTE",
    "KB_PUBLISH_IDENTITY_NAME", "KB_PUBLISH_IDENTITY_EMAIL",
)


class VerifyError(RuntimeError):
    def __init__(self, step: str, output: str):
        super().__init__(f"verify failed at [{step}]: {output[-2000:]}")
        self.step = step


# (label, relative cwd, argv). Label == ci.yml job id == script stem; cheap
# and secret-scan first, the long suites after. Order is pinned by tests.
JOBS: tuple[str, ...] = ("gitleaks", "lint", "python", "kb-inference", "evals-tests",
                         "frontend")
STEPS: list[tuple[str, str, list[str]]] = [
    (job, ".", ["bash", f"scripts/ci/{job}.sh"]) for job in JOBS
]


def _run_steps(tree: Path, runner=subprocess.run, *, require_gitleaks: bool = False,
               which=shutil.which, env: dict | None = None) -> None:
    for label, rel_cwd, argv in STEPS:
        if label == "gitleaks" and which("gitleaks") is None:
            if require_gitleaks:
                raise VerifyError(label, "gitleaks not installed — a push requires the "
                                         "export-tree secret scan")
            print("verify: gitleaks not installed — skipping for this dry run "
                  "(--push refuses without it)")
            continue
        result = runner(argv, cwd=str(tree / rel_cwd), capture_output=True, env=env)
        if result.returncode != 0:
            err = getattr(result, "stderr", b"") or b""
            detail = err.decode(errors="replace") if isinstance(err, (bytes, bytearray)) else str(err)
            # Lead with the failing command so the tail (truncated in VerifyError)
            # still identifies what ran, not just the step label.
            raise VerifyError(label, f"$ {' '.join(argv)}\n{detail}")
        print(f"verify: [{label}] ok")


def scratch_env() -> dict:
    """The environment the scripts run in: KB_TEST_DATABASE_URL is the scratch
    database from KB_VERIFY_DATABASE_URL, KB_TEST_DATABASE_URL_B is unset so
    python.sh derives `<scratch>_b` — never an inherited shared one — and the
    PUBLISH_ONLY_VARS are withheld."""
    return _scratch()[0]


def _scratch() -> tuple[dict, str]:
    url = os.environ.get(SCRATCH_DB_VAR, "").strip()
    if not url:
        raise VerifyError("preflight", f"{SCRATCH_DB_VAR} is not set — verify runs the suites, "
                          "which TRUNCATE their database; point it at a database named "
                          "kb_verify[_<suffix>] that exists only for this (python.sh creates "
                          "it and <name>_b from template0 if missing)")
    try:
        name = conninfo_to_dict(url).get("dbname") or ""
    except Exception:  # noqa: BLE001 — any parse failure is a refusal
        name = ""
    if not _SCRATCH_NAME.match(name):
        raise VerifyError("preflight", f"{SCRATCH_DB_VAR} must name a database called "
                          f"kb_verify[_<suffix>], got {name or 'none'!r} — never the shared "
                          "test database or a live one")
    env = {k: v for k, v in os.environ.items() if k not in PUBLISH_ONLY_VARS}
    env.pop("KB_TEST_DATABASE_URL_B", None)
    env["KB_TEST_DATABASE_URL"] = url
    return env, name


@contextlib.contextmanager
def _exclusive(name: str) -> Iterator[None]:
    """Hold the per-scratch-database lock, or refuse (never wait)."""
    path = Path(tempfile.gettempdir()) / f"kb-verify-{name}.lock"
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VerifyError("preflight", f"another kb-publish verify is using the scratch "
                              f"database {name} ({path}) — wait for it, or point "
                              f"KB_VERIFY_DATABASE_URL at kb_verify_<other>") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# The public repo's CI runs in a git checkout and some tests use git
# (check-ignore, ls-files), so the copy is committed once, as publish-candidate's
# export-ci does. Without it those tests failed here and nowhere else, and no
# --push could pass verify. Hooks off: the user's global git config still applies.
_GIT = ("git", "-c", "user.name=kb-publish-verify", "-c", "user.email=verify@invalid",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")


def _as_public_checkout(tree: Path, env: dict) -> None:
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-q", "--allow-empty", "-m", "export")):
        result = subprocess.run([*_GIT, *args], cwd=str(tree), capture_output=True, env=env)
        if getattr(result, "returncode", 0) != 0:
            err = getattr(result, "stderr", b"") or b""
            raise VerifyError("preflight", f"$ git {' '.join(args)}\n"
                                           f"{err.decode(errors='replace')}")


def verify(tree: Path, runner=subprocess.run, *, require_gitleaks: bool = False,
           which=shutil.which) -> None:
    env, name = _scratch()
    with _exclusive(name):
        _as_public_checkout(tree, env)
        _run_steps(tree, runner, require_gitleaks=require_gitleaks, which=which, env=env)
