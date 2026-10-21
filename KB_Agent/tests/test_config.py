import os
from pathlib import Path

from kb_agent.config import AgentConfig, config_path, load_config, save_config


def test_round_trip_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    cfg = AgentConfig(gateway="https://kb-agent.example.com", tenant="alice", secret="s" * 64,
                      cf_client_id="id", cf_client_secret="cs", vault=tmp_path / "Vault",
                      exclude=["Private/**"], scan_seconds=5)
    p = save_config(cfg)
    assert p == config_path() == tmp_path / "config.toml"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    back = load_config()
    assert back == cfg
    assert back.job_concurrency == 2 and back.claude_model == ""


def test_fresh_config_dir_is_0700(tmp_path, monkeypatch):
    home = tmp_path / "kb-agent-home"
    monkeypatch.setenv("KB_AGENT_HOME", str(home))
    save_config(AgentConfig(gateway="https://g", tenant="alice", secret="s" * 64, vault=tmp_path))
    assert oct(home.stat().st_mode & 0o777) == "0o700"


def test_missing_config_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    try:
        load_config()
    except FileNotFoundError as exc:
        assert "kb-agent init" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_config_is_0600_from_the_first_byte(tmp_path, monkeypatch):
    """The secret must never sit in a umask-default (0644) file, even briefly: with the
    after-the-fact chmods disabled, the file must still be created 0600."""
    monkeypatch.setattr(Path, "chmod", lambda self, mode, **kw: None)
    cfg = AgentConfig(gateway="https://g", tenant="alice", secret="s" * 64, vault=tmp_path)
    old = os.umask(0o022)
    try:
        p = save_config(cfg, tmp_path / "config.toml")
    finally:
        os.umask(old)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_job_concurrency_outside_1_to_3_is_refused(tmp_path):
    """In flight per tenant = the pusher + job_concurrency result posts; the gateway caps at 4."""
    import pytest

    from kb_agent.config import ConfigError
    for n in (0, 4):
        p = save_config(AgentConfig(gateway="https://g", tenant="a", secret="s" * 64, vault=tmp_path,
                                    job_concurrency=n), tmp_path / f"c{n}.toml")
        with pytest.raises(ConfigError, match="job_concurrency"):
            load_config(p)
    p = save_config(AgentConfig(gateway="https://g", tenant="a", secret="s" * 64, vault=tmp_path,
                                job_concurrency=3), tmp_path / "ok.toml")
    assert load_config(p).job_concurrency == 3


def test_install_id_is_made_once_and_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    cfg = AgentConfig(gateway="https://g", tenant="alice", secret="s" * 64, vault=tmp_path)
    save_config(cfg)
    assert load_config().install_id == cfg.install_id == load_config().install_id


def test_a_config_without_install_id_gets_one_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_AGENT_HOME", str(tmp_path))
    save_config(AgentConfig(gateway="https://g", tenant="alice", secret="s" * 64, vault=tmp_path))
    p = config_path()
    p.write_text("".join(line + "\n" for line in p.read_text().splitlines()
                         if not line.startswith("install_id")))
    first = load_config()
    assert len(first.install_id) == 32 and "install_id" in p.read_text()
    assert load_config().install_id == first.install_id
    assert oct(p.stat().st_mode & 0o777) == "0o600"
