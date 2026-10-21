"""Agent configuration: one TOML file under ~/Library/Application Support/kb-agent.

The same support dir (`home()`, overridable with KB_AGENT_HOME) holds `state.sqlite`.

`claude -p` jobs do NOT run under it: they run in `<per-user temp dir>/kb-agent-<uid>/claude-cwd`
(`kb_agent.jobs.claude_cwd_path`), one fixed, empty, 0700 directory OUTSIDE $HOME. Fixed, because
`claude` files session transcripts per cwd under ~/.claude/projects/, so a fresh tempdir per
process would leave a new project directory behind on every restart. Outside $HOME, because
`claude` loads CLAUDE.md from every ancestor of its cwd, and ~/CLAUDE.md would then shape tenant
prompts as it never does the owner's (whose cwd is a tempdir under /var/folders). Empty, so no
project settings or memory apply. Nothing writes into it.
"""
from __future__ import annotations

import ipaddress
import os
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "kb-agent"
# Requests in flight per tenant = the pusher + one result post per concurrent job, and the
# gateway allows 4.
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 3


class ConfigError(ValueError):
    """The config file is present but unusable; the message says which field and why."""


def check_gateway(url: str) -> str:
    """The gateway URL, refused unless it is https:// (http:// only to a loopback host, for
    a gateway on this machine, e.g. the end-to-end test). Every request carries the
    Cloudflare service-token secret and pushes the whole vault's note text: over plain
    http both would cross the network in cleartext (the HMAC protects integrity, not
    confidentiality)."""
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        raise ConfigError(f"gateway must be a URL like https://kb-agent.example.com (got {url!r})")
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and _is_loopback(host):
        return url
    raise ConfigError(f"gateway must use https:// (got {url!r}): the agent sends your notes "
                      "and its Cloudflare service token to it; plain http is allowed only "
                      "for a gateway on this machine (127.0.0.1 / localhost)")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_concurrency(n: int) -> int:
    if not MIN_CONCURRENCY <= n <= MAX_CONCURRENCY:
        raise ConfigError(f"job_concurrency must be {MIN_CONCURRENCY}-{MAX_CONCURRENCY} (got {n}): the gateway "
                          "allows 4 requests in flight, the pusher plus one per concurrent job")
    return n


@dataclass
class AgentConfig:
    gateway: str
    tenant: str
    secret: str
    vault: Path
    cf_client_id: str = ""
    cf_client_secret: str = ""
    exclude: list[str] = field(default_factory=list)
    scan_seconds: int = 30
    manifest_seconds: int = 600
    job_concurrency: int = 2
    claude_model: str = ""
    # Absolute path of `claude`, resolved by `kb-agent init` from the user's shell PATH: the
    # LaunchAgent's fixed PATH misses nvm, volta, asdf and custom npm-prefix installs.
    claude_path: str = ""
    # Opaque per-install id, sent as X-KB-Agent-Id so the owner can tell installs apart in
    # agent_state. Random, made once and kept in config.toml; never derived from the machine.
    install_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("KB_AGENT_HOME") or DEFAULT_HOME)


def config_path(env=None) -> Path:
    return home(env) / "config.toml"


def state_path(env=None) -> Path:
    return home(env) / "state.sqlite"


def _dump(cfg: AgentConfig) -> str:
    def q(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    d = asdict(cfg)
    lines = []
    for k, v in d.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k} = {v}")
        elif isinstance(v, list):
            lines.append(f"{k} = [" + ", ".join(q(str(x)) for x in v) + "]")
        else:
            lines.append(f"{k} = {q(str(v))}")
    return "\n".join(lines) + "\n"


def save_config(cfg: AgentConfig, path: Path | None = None) -> Path:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    # Create the file 0600 from the first byte: the secret never sits in a umask-default
    # file. fchmod covers a stale .tmp left by an earlier crash, which keeps its old mode.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_dump(cfg))
    tmp.replace(p)
    p.chmod(0o600)
    return p


def load_config(path: Path | None = None) -> AgentConfig:
    p = path or config_path()
    if not p.exists():
        raise FileNotFoundError(f"{p} not found — run `kb-agent init` first")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    data["vault"] = Path(data["vault"])
    install_id = str(data.pop("install_id", "") or "")
    cfg = AgentConfig(**data, install_id=install_id) if install_id else AgentConfig(**data)
    check_concurrency(cfg.job_concurrency)
    check_gateway(cfg.gateway)
    if not install_id:
        save_config(cfg, p)             # a config written before install_id existed: keep the new id
    return cfg
