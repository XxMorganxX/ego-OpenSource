# ego-OS documentation

Start with the [README](../README.md): what ego-OS is, the 5-minute demo and the
Quickstart.

## Setting up and running

- [Agent-driven setup guide](setup/README.md): written for an AI agent (Claude Code
  `/setup-kb`, or any coding agent) to install ego-OS for you, with checks after each step
- Source recipes: [repository docs](setup/recipes/repo-docs.md) ·
  [email](setup/recipes/email.md) · [webhook push](setup/recipes/webhook.md)
- [Personal skill registry](setup/recipes/skill-registry.md): agent skills stored in the
  KB and served over kb-mcp, and the client setup that makes agents use them
- [Operations](operations.md): services and ports, `kb-run`, running at boot, a second
  isolated instance, running without models, retrying failed enrichment, your own
  sources, health checks, backups, tests and CI
- [Exposing ego-OS beyond localhost](exposure.md): the authenticated MCP listener and a
  login-protected dashboard
- [Postgres in Docker](../KB_Core/docs/docker.md)
- [Per-install policy](../KB_Core/docs/policy-setup.md): decay, project dormancy,
  taxonomy, prompt overlays

## How it works

- [Design principles](../KB_Core/docs/PRINCIPLES.md)
- [Ingestion architecture](../KB_Core/docs/ingestion-architecture.md) and the
  [pipeline as built](../KB_Core/docs/ingestion-pipeline-visualization.md)
- [LLM call map](llm-inference-map.md): every place the engine calls a model, with its
  input and prompt source

## Packages

- [KB_Core](../KB_Core/README.md): the engine, its CLIs and migrations
- [KB_MCP](../KB_MCP/README.md): the MCP server and its tools
- [KB_Retrieval](../KB_Retrieval/README.md): retrieval strategies
- [KB_Inference](../KB_Inference/README.md): the local model server
- [KB_Frontend](../KB_Frontend/README.md): the dashboard
- [KB_Core dashboard API](../KB_Core/dashboard/README.md)
- [KB_Agent](../KB_Agent/README.md): the agent for hosted setups

## Evaluation

- [Retrieval evals](../evals/retrieval/README.md) ·
  [MCP-surface evals](../evals/mcp_surface/README.md) ·
  [Extraction evals](../evals/extraction/README.md)
- [Demo vault](../examples/demo-vault/): invented notes for trying ego-OS without your own
  data

## Contributing

[CONTRIBUTING](../CONTRIBUTING.md) · [Code of conduct](../CODE_OF_CONDUCT.md) ·
[Security policy](../SECURITY.md) · [Changelog](../CHANGELOG.md)

[How this repository is published](publishing.md): the allowlist mirror, `kb-publish`'s
checks, and how a pull request travels back upstream with `kb-import-pr`.
