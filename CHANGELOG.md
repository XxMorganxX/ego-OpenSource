# Changelog

## v0.1.0 — first public release

First public snapshot of ego-OS, developed in a private repository since June 2026
(1,280+ commits). This repository is a filtered mirror of that work: one `sync:`
commit per publish (see CONTRIBUTING.md).

Initial open-source cut of ego-OS: KB_Core engine (ingestion, enrichment,
entity/concept layers, dashboard API), KB_Retrieval, KB_MCP server, KB_Inference
(Ollama), KB_Frontend dashboard, host-native `kb-run` supervisor, synthetic eval
sets, cross-platform quickstart.

Skill and agent library: KB_MCP serves your own Claude Code skills and subagent
definitions (search, read, write, hash-bound review; only approved revisions are served),
the dashboard has a Skills | Agents page, and `kb-library sync` keeps each device's
`~/.claude/skills` and `~/.claude/agents` in step with the approved library.

Operations: `kb-backup` (`age`-encrypted `pg_dump`, with restore); `KB_Agent`, a
vault-pushing agent that runs enrichment jobs on its own machine's `claude -p` login.

Agent-driven setup: `docs/setup/README.md` walks an AI agent through standing
up the stack on a folder of Markdown notes (four user decisions, checked
phases); `/setup-kb` skill; recipes for repo-doc mirroring, email, webhook push.

Docs: an invented demo vault (`examples/demo-vault/`) with a no-model, isolated
"Try it in 5 minutes" path; READMEs for KB_Core and KB_MCP; operations and exposure
guides under `docs/`.

Python distributions are named `ego-os-core`, `ego-os-retrieval`, `ego-os-mcp` and
`ego-os-agent` (`kb-mcp` is taken on PyPI, so the `kb-*` names were dropped; nothing is
published to PyPI). Import packages (`kb_core`, `kb_retrieval`, `kb_mcp`, `kb_agent`) and commands
(`kb-run`, `kb-mcp`, `kb-agent`, …) keep their `kb` names. Install from source.
