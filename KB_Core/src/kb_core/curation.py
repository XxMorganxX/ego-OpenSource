"""User-facing taxonomy curation.

A SQL-free layer over KnowledgeBase: each operation is a small CurationCommand that exposes a
read-only `preview()` and a mutating `apply()`, both returning one shared ImpactReport. The
CurationService holds the registry and dispatches by action name, so new commands need no new
wiring. No raw SQL here — commands only call KnowledgeBase methods.
"""

from dataclasses import dataclass, field

from kb_core.entities import is_valid_class_name, normalize_entity_name
from kb_core.knowledge_base import KnowledgeBase


@dataclass
class ImpactReport:
    """The blast radius of a curation command — returned by both preview (applied=False, 'will')
    and apply (applied=True, 'did'). Counts are best-effort estimates at preview time."""
    action: str
    source: str | None = None
    target: str | None = None
    summary: str = ""
    affected_memories: int = 0
    affected_instances: int = 0
    queued_reenrichment: int = 0
    warnings: list[str] = field(default_factory=list)
    reversible: bool = False
    applied: bool = False


class CurationConflict(Exception):
    """A command cannot proceed against the current state (e.g. target is blocked). -> HTTP 409."""


def _find_class(kb: KnowledgeBase, name: str) -> dict | None:
    """The class-manager row for `name` (incl. blocked), matched on normalized name, or None."""
    norm = normalize_entity_name(name)
    for r in kb.list_entity_classes(include_blocked=True):
        if normalize_entity_name(r["name"]) == norm:
            return r
    return None


class CurationCommand:
    """One curation operation. Subclasses set name/label/reversible/args and implement
    preview/apply. `args` is the ordered tuple of required string argument names."""
    name: str = ""
    label: str = ""
    reversible: bool = True
    args: tuple[str, ...] = ()

    def preview(self, kb: KnowledgeBase, **kwargs) -> ImpactReport:
        raise NotImplementedError

    def apply(self, kb: KnowledgeBase, **kwargs) -> ImpactReport:
        raise NotImplementedError


class CreateClass(CurationCommand):
    name, label, reversible, args = "create", "Create category", True, ("name",)

    def preview(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        if not is_valid_class_name(name):
            raise ValueError(f"not a valid category name: {name!r}")
        rep = ImpactReport(action="create", target=name,
                           summary=f"Create category '{name}'", reversible=True)
        existing = _find_class(kb, name)
        if existing and existing["status"] == "blocked":
            rep.warnings.append(f"'{name}' is blocked; unblock it first")
        elif existing:
            rep.warnings.append(f"'{name}' already exists (no-op)")
        return rep

    def apply(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        rep = self.preview(kb, name=name)
        existing = _find_class(kb, name)
        if existing and existing["status"] == "blocked":
            raise CurationConflict(f"'{name}' is blocked; unblock it first")
        kb.upsert_entity_class(name, active=True)
        kb.set_class_pinned(name, True)
        rep.applied = True
        return rep


class SetPinned(CurationCommand):
    """Pin or unpin a class (one instance per direction)."""
    def __init__(self, *, pinned: bool) -> None:
        self._pinned = pinned
        self.name = "pin" if pinned else "unpin"
        self.label = "Pin" if pinned else "Unpin"
        self.reversible = True
        self.args = ("name",)

    def preview(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        if _find_class(kb, name) is None:
            raise LookupError(f"class not found: {name}")
        return ImpactReport(action=self.name, target=name,
                            summary=f"{self.label} '{name}'", reversible=True)

    def apply(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        rep = self.preview(kb, name=name)
        if kb.set_class_pinned(name, self._pinned) is None:
            raise LookupError(f"class not found: {name}")
        rep.applied = True
        return rep


class PruneClass(CurationCommand):
    name, label, reversible, args = "prune", "Remove category", False, ("name",)

    def preview(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        row = _find_class(kb, name)
        if row is None:
            raise LookupError(f"class not found: {name}")
        rep = ImpactReport(
            action="prune", source=name, summary=f"Remove category '{name}'",
            affected_memories=row["members"], affected_instances=row["instances"],
            queued_reenrichment=len(kb.class_voters(name)), reversible=False)
        if row["pinned"]:
            rep.warnings.append(f"'{name}' is pinned")
        return rep

    def apply(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        rep = self.preview(kb, name=name)
        kb.block_class(name, purge_instances=True, requeue_voters=True)
        kb.reconcile_classes()
        rep.applied = True
        return rep


class SetDescription(CurationCommand):
    name, label, reversible, args = "set_description", "Edit description", True, ("name", "description")

    def preview(self, kb: KnowledgeBase, *, name: str, description: str) -> ImpactReport:
        if _find_class(kb, name) is None:
            raise LookupError(f"class not found: {name}")
        return ImpactReport(action="set_description", target=name,
                            summary=f"Edit description of '{name}'", reversible=True)

    def apply(self, kb: KnowledgeBase, *, name: str, description: str) -> ImpactReport:
        rep = self.preview(kb, name=name, description=description)
        if kb.set_class_description(name, description) is None:
            raise LookupError(f"class not found: {name}")
        rep.applied = True
        return rep


class UnblockClass(CurationCommand):
    name, label, reversible, args = "unblock", "Restore", True, ("name",)

    def preview(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        if _find_class(kb, name) is None:
            raise LookupError(f"class not found: {name}")
        return ImpactReport(action="unblock", target=name,
                            summary=f"Restore '{name}' (returns inactive)", reversible=True)

    def apply(self, kb: KnowledgeBase, *, name: str) -> ImpactReport:
        rep = self.preview(kb, name=name)
        if kb.unblock_class(name) is None:
            raise LookupError(f"class not found: {name}")
        rep.applied = True
        return rep


class MergeClass(CurationCommand):
    """Fold `source` into `target` (verb 'Merge'), or rename `source` to `target` (verb
    'Rename') — the same operation under two action names. Requeues the source's voters,
    pins the target, and reconciles the active set."""
    def __init__(self, *, action: str = "merge", label: str = "Merge into…",
                 verb: str = "Merge") -> None:
        self.name = action
        self.label = label
        self.verb = verb
        self.reversible = False
        self.args = ("source", "target")

    def _report(self, kb: KnowledgeBase, source: str, target: str) -> ImpactReport:
        src = _find_class(kb, source)
        if src is None:
            raise LookupError(f"class not found: {source}")
        if normalize_entity_name(source) == normalize_entity_name(target):
            raise ValueError("cannot merge a class into itself")
        rep = ImpactReport(
            action=self.name, source=source, target=target,
            summary=f"{self.verb} '{source}' into '{target}'",
            affected_memories=src["members"], affected_instances=src["instances"],
            queued_reenrichment=len(kb.class_voters(source)), reversible=False)
        tgt = _find_class(kb, target)
        if tgt and tgt["status"] == "blocked":
            rep.warnings.append(f"target '{target}' is blocked; unblock it first")
        if src["pinned"]:
            rep.warnings.append(f"'{source}' is pinned")
        return rep

    def preview(self, kb: KnowledgeBase, *, source: str, target: str) -> ImpactReport:
        return self._report(kb, source, target)

    def apply(self, kb: KnowledgeBase, *, source: str, target: str) -> ImpactReport:
        rep = self._report(kb, source, target)
        tgt = _find_class(kb, target)
        if tgt and tgt["status"] == "blocked":
            raise CurationConflict(f"target '{target}' is blocked; unblock it first")
        kb.merge_classes(source, target, requeue_voters=True)
        kb.set_class_pinned(target, True)
        kb.reconcile_classes()
        rep.applied = True
        return rep


_COMMANDS: list[CurationCommand] = [
    MergeClass(),
    MergeClass(action="rename", label="Rename", verb="Rename"),
    PruneClass(),
    CreateClass(),
    SetDescription(),
    UnblockClass(),
    SetPinned(pinned=True),
    SetPinned(pinned=False),
]


class CurationService:
    """Dispatches curation commands by action name over a KnowledgeBase."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb
        self._cmds = {c.name: c for c in _COMMANDS}

    def commands(self) -> list[dict]:
        return [{"action": c.name, "label": c.label, "reversible": c.reversible,
                 "args": list(c.args)} for c in self._cmds.values()]

    def _get(self, action: str) -> CurationCommand:
        cmd = self._cmds.get(action)
        if cmd is None:
            raise ValueError(f"unknown curation action: {action}")
        return cmd

    def _bind(self, cmd: CurationCommand, args: dict) -> dict:
        missing = [a for a in cmd.args if not args.get(a)]
        if missing:
            raise ValueError(f"missing args for {cmd.name}: {', '.join(missing)}")
        return {k: args[k] for k in cmd.args}

    def preview(self, action: str, **args) -> ImpactReport:
        cmd = self._get(action)
        return cmd.preview(self._kb, **self._bind(cmd, args))

    def apply(self, action: str, **args) -> ImpactReport:
        cmd = self._get(action)
        return cmd.apply(self._kb, **self._bind(cmd, args))
