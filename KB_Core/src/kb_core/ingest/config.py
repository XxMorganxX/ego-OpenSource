"""Per-source ingestion config: a concise sources.toml of dev-tunable toggles.

Each adapter gets a [section] in sources.toml holding only user settings:
    enabled        run this source's ingest job at all
    is_enrichable  run new/changed nodes through the (LLM) enrichment pipeline
    period_seconds how often the job runs (0 = once per scheduler heartbeat)

Secrets and machine paths (KB_DATABASE_URL, KB_OBSIDIAN_VAULT, KB_INGEST_SECRET)
stay in the environment — this file is behavior only. A source's transform pipeline
is composed from its toggles here (is_enrichable gates the FlagEnrichment stage).
"""

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from kb_core.adapters.base import Adapter, PushDecoder
from kb_core.ingest.pipeline import Pipeline
from kb_core.ingest.transforms import default_pipeline


@dataclass
class SourceConfig:
    source: str
    mode: Literal["pull", "push"]
    period: timedelta
    prune: bool = True
    is_enrichable: bool = True          # run new/changed nodes through enrichment
    # The per-source transform pipeline — a source's separable ingestion transformation.
    pipeline: Pipeline = field(default_factory=default_pipeline)
    adapter: Adapter | None = None      # pull sources supply an Adapter (fetch())
    decoder: PushDecoder | None = None  # push sources supply a PushDecoder (decode())
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.mode == "push":
            self.prune = False              # push sees deltas; pruning would mass-delete


def load_settings(env: dict | None = None) -> dict:
    """Read sources.toml (path: KB_SOURCES_CONFIG env, else ./sources.toml). {} if absent."""
    env = os.environ if env is None else env
    path = env.get("KB_SOURCES_CONFIG", "sources.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_sources(env: dict | None = None, *,
                 settings: dict | None = None) -> "list[SourceConfig]":
    """Build the enabled source set from sources.toml toggles + env connection details."""
    env = os.environ if env is None else env
    settings = load_settings(env) if settings is None else settings
    sources: list[SourceConfig] = []

    obs = settings.get("obsidian", {})
    vault = env.get("KB_OBSIDIAN_VAULT")
    if vault and obs.get("enabled", True):
        from kb_core.adapters.obsidian.adapter import ObsidianAdapter
        enrichable = bool(obs.get("is_enrichable", True))
        # Place the whole vault under one source layer (default "Obsidian"); a sources.toml
        # `layer` or KB_OBSIDIAN_LAYER="" disables it. Placement-only, so it never churns ids.
        layer = obs.get("layer", env.get("KB_OBSIDIAN_LAYER", "Obsidian"))
        # Stamp a stable frontmatter id into unstamped notes on sync; toml `stamp` or
        # KB_OBSIDIAN_STAMP=0 disables it.
        stamp = bool(obs.get("stamp",
                     env.get("KB_OBSIDIAN_STAMP", "1").lower() not in ("0", "", "false", "no")))
        sources.append(SourceConfig(
            source="obsidian", mode="pull",
            period=timedelta(seconds=int(obs.get("period_seconds", 300))),
            is_enrichable=enrichable,
            adapter=ObsidianAdapter(vault, root_label=layer or None, stamp=stamp),
            pipeline=default_pipeline(enrichable=enrichable),
        ))

    wh = settings.get("webhook", {})
    if wh.get("enabled", False):
        from kb_core.adapters.webhook.decoder import WebhookDecoder
        enrichable = bool(wh.get("is_enrichable", True))
        sources.append(SourceConfig(
            source="webhook", mode="push",
            period=timedelta(seconds=int(wh.get("period_seconds", 60))),
            is_enrichable=enrichable,
            decoder=WebhookDecoder(),
            pipeline=default_pipeline(enrichable=enrichable),
        ))

    va = settings.get("vault", {})
    if va.get("enabled", False):
        from kb_core.adapters.vault.decoder import VaultDecoder
        enrichable = bool(va.get("is_enrichable", True))
        layer = va.get("layer", env.get("KB_VAULT_LAYER", "Obsidian"))
        sources.append(SourceConfig(
            source="vault", mode="push",
            period=timedelta(seconds=int(va.get("period_seconds", 20))),
            is_enrichable=enrichable,
            decoder=VaultDecoder(root_label=layer or None),
            pipeline=default_pipeline(enrichable=enrichable),
        ))

    return sources
