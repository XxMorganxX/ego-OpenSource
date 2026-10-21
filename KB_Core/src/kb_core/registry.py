"""Registry of allowed node types and their optional extension tables."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TypeDef:
    name: str
    extension: str | None = None


class TypeRegistry:
    def __init__(self) -> None:
        self._types: dict[str, TypeDef] = {}

    def register(self, name: str, extension: str | None = None) -> None:
        self._types[name] = TypeDef(name, extension)

    def is_registered(self, name: str) -> bool:
        return name in self._types

    def extension_for(self, name: str) -> str | None:
        return self._types[name].extension


def default_registry() -> TypeRegistry:
    """Phase 1 types: 'note' (agent memories) and 'folder' (directories), plus 'entity'
    (Phase 2 concept nodes). All core-only; 'entity' fields live in the `entities` table."""
    r = TypeRegistry()
    r.register("note")
    r.register("folder")
    r.register("entity")
    return r
