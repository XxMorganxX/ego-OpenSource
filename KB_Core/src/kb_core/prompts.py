"""Prompt files with a per-install overlay. Pure — no DB, no network.

Names are relative to the shipped `prompts/` directory beside the enrichment config; a file
of the same name under the policy's overlay_dir shadows it. Two markers expand at load
time (once per process — the loaded text is static):
  {{include:NAME}}   the partial NAME through this same resolver (so an overlay can replace
                     one paragraph — the examples — without copying the whole prompt);
  {{taxonomy}}       the policy's fixed-category block (Taxonomy.prompt_block), or nothing.
Runs of blank lines left by an empty expansion collapse to one blank line."""
from __future__ import annotations

import functools
import os
import re
from collections.abc import Mapping
from pathlib import Path

from kb_core.policy import Policy, PolicyError

_INCLUDE = re.compile(r"\{\{include:([^{}]+)\}\}")
_TAXONOMY = "{{taxonomy}}"
_MAX_DEPTH = 5
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def prompts_base_dir(env: Mapping[str, str] | None = None) -> Path:
    """`prompts/` beside enrichment.toml (KB_ENRICHMENT_CONFIG, default ./enrichment.toml)."""
    env = os.environ if env is None else env
    return Path(env.get("KB_ENRICHMENT_CONFIG", "enrichment.toml")).resolve().parent / "prompts"


class PromptResolver:
    def __init__(self, policy: Policy, base_dir: str | os.PathLike = "prompts") -> None:
        self._policy = policy
        self._base = Path(base_dir)
        self._overlay = Path(policy.prompts.overlay_dir) if policy.prompts.overlay_dir else None

    def overlays(self) -> list[str]:
        """Overlay file names present (sorted), [] when no overlay dir is configured."""
        if self._overlay is None or not self._overlay.is_dir():
            return []
        return sorted(p.name for p in self._overlay.iterdir() if p.is_file())

    def path_for(self, name: str) -> Path:
        """The file that serves `name`: the overlay copy when present, else the shipped one.
        A bare file name only — no directories, no `..`."""
        if not _NAME_RE.match(name):
            raise PolicyError(f"prompt name {name!r} must be a bare file name")
        if name in (".", ".."):
            raise PolicyError(f"prompt name {name!r} must be a bare file name")
        if self._overlay is not None and (self._overlay / name).is_file():
            return self._overlay / name
        return self._base / name

    def load(self, name: str, *, _depth: int = 0) -> str:
        if _depth > _MAX_DEPTH:
            raise PolicyError(f"prompt include depth exceeded at {name!r} (cycle?)")
        path = self.path_for(name)
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            where = f"{self._overlay} or {self._base}" if self._overlay else str(self._base)
            raise PolicyError(f"prompt {name!r} not found in {where}") from None
        text = _INCLUDE.sub(lambda m: self.load(m.group(1).strip(), _depth=_depth + 1), text)
        text = text.replace(_TAXONOMY, self._policy.taxonomy.prompt_block())
        return re.sub(r"\n{3,}", "\n\n", text).strip()


@functools.lru_cache(maxsize=None)
def shipped_prompt(name: str) -> str:
    """A shipped prompt with NO overlay, resolved beside the enrichment config and cached
    for the process — for callers that hold no KnowledgeBase (tests, ad-hoc scripts).
    Production paths go through KnowledgeBase.prompt so the overlay applies."""
    from kb_core.policy import DEFAULT_POLICY
    return PromptResolver(DEFAULT_POLICY, prompts_base_dir()).load(name)
