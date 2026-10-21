"""claude_auth: level derivation, and the two degradation paths that must never raise."""
import json
import subprocess

import pytest

from kb_core import claude_auth

NOW = 1_800_000_000.0            # fixed clock so day maths is exact
DAY = 86_400.0


def _creds(tmp_path, days_from_now):
    """Write a credentials file whose refresh token expires `days_from_now` from NOW."""
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "SECRET-ACCESS", "refreshToken": "SECRET-REFRESH",
        "refreshTokenExpiresAt": int((NOW + days_from_now * DAY) * 1000)}}))
    return p


@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setattr(claude_auth, "_cli_status",
                        lambda: {"loggedIn": True, "email": "m@example.com"})


@pytest.mark.parametrize("days,expected", [(10.0, "ok"), (5.0, "warn"), (1.0, "critical")])
def test_level_follows_days_remaining(tmp_path, logged_in, days, expected):
    st = claude_auth.claude_auth_status(_creds(tmp_path, days), now=NOW)
    assert st["level"] == expected
    assert st["days_left"] == pytest.approx(days)
    assert st["logged_in"] is True


def test_signed_out_is_out_regardless_of_expiry(tmp_path, monkeypatch):
    # A far-future expiry must not mask a signed-out CLI: the token is valid on
    # disk and useless in practice.
    monkeypatch.setattr(claude_auth, "_cli_status", lambda: {"loggedIn": False})
    st = claude_auth.claude_auth_status(_creds(tmp_path, 30.0), now=NOW)
    assert st["level"] == "out"
    assert st["logged_in"] is False


@pytest.mark.parametrize("body", ["", "not json", '{"other": 1}'])
def test_unreadable_credentials_degrade_without_raising(tmp_path, logged_in, body):
    p = tmp_path / ".credentials.json"
    p.write_text(body)
    st = claude_auth.claude_auth_status(p, now=NOW)
    assert st["days_left"] is None and st["expires_at"] is None
    assert st["logged_in"] is True        # the CLI still answered
    assert st["level"] == "ok"            # cannot measure -> do not cry wolf


def test_missing_credentials_file_degrades(tmp_path, logged_in):
    st = claude_auth.claude_auth_status(tmp_path / "nope.json", now=NOW)
    assert st["days_left"] is None and st["level"] == "ok"


def test_cli_timeout_is_out_and_does_not_raise(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5)
    monkeypatch.setattr(claude_auth.subprocess, "run", boom)
    st = claude_auth.claude_auth_status(_creds(tmp_path, 30.0), now=NOW)
    assert st["level"] == "out"           # a CLI that cannot answer is not healthy


def test_cli_nonzero_exit_is_out(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_auth.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "boom"))
    assert claude_auth.claude_auth_status(_creds(tmp_path, 30.0), now=NOW)["level"] == "out"


def test_result_never_contains_a_token(tmp_path, logged_in):
    st = claude_auth.claude_auth_status(_creds(tmp_path, 5.0), now=NOW)
    blob = json.dumps(st)
    assert "SECRET-ACCESS" not in blob and "SECRET-REFRESH" not in blob
    assert set(st) == {"level", "logged_in", "days_left", "expires_at", "email"}


def _routing(tmp_path, claude_tasks=()):
    from kb_core.inference_routing import TASKS
    p = tmp_path / "inference.toml"
    p.write_text("".join(
        f'[task.{n}]\nprovider = "{"claude-cli" if n in claude_tasks else "local"}"\n'
        for n in TASKS))
    return str(p)


def test_claude_cli_in_use_follows_the_routing(tmp_path):
    assert claude_auth.claude_cli_in_use(
        {"KB_INFERENCE_ROUTING": _routing(tmp_path)}) is False
    assert claude_auth.claude_cli_in_use(
        {"KB_INFERENCE_ROUTING": _routing(tmp_path, {"extract_entities"})}) is True


def test_claude_cli_in_use_fails_toward_watching(tmp_path):
    assert claude_auth.claude_cli_in_use(
        {"KB_INFERENCE_ROUTING": str(tmp_path / "missing.toml")}) is True
