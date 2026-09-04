from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Callable, Protocol

from pxr import Usd


@dataclass(frozen=True)
class PortContract:
    name: str
    required: bool = False


@dataclass
class NodeResult:
    outputs: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    reason: str = ""
    external_session_id: str = ""
    external_command_id: str = ""


class ExecutionContext(Protocol):
    prim: Usd.Prim
    inputs: dict[str, Any]
    graph_directory: str


class NodeHandler(Protocol):
    def execute(self, context: ExecutionContext) -> NodeResult: ...


@dataclass(frozen=True)
class NodeSpec:
    schema_class: str
    category: str
    handler: NodeHandler
    inputs: tuple[PortContract, ...] = ()
    outputs: tuple[str, ...] = ()


class Registry:
    """Runtime behavior keyed by inherited USD schema class."""

    def __init__(self) -> None:
        self._specs: dict[str, NodeSpec] = {}

    def register(self, spec: NodeSpec) -> None:
        if not spec.schema_class.startswith("/"):
            raise ValueError("schema_class must be an absolute class prim path")
        if spec.schema_class in self._specs:
            raise ValueError(f"duplicate node handler for {spec.schema_class}")
        self._specs[spec.schema_class] = spec

    def spec_for(self, prim: Usd.Prim) -> NodeSpec | None:
        candidates = [f"/{prim.GetTypeName()}"] if prim.GetTypeName() else []
        candidates.extend(str(path) for path in prim.GetInherits().GetAllDirectInherits())
        for path in candidates:
            if path in self._specs:
                return self._specs[path]
        return None

    def specs(self) -> tuple[NodeSpec, ...]:
        return tuple(self._specs.values())

    @classmethod
    def discover(cls) -> "Registry":
        registry = cls()
        from .builtins import register

        register(registry)
        try:
            entry_points = metadata.entry_points(group="enqueue.nodes")
        except TypeError:  # pragma: no cover - Python 3.10 compatibility
            entry_points = metadata.entry_points().get("enqueue.nodes", ())
        for entry_point in entry_points:
            if entry_point.name == "enqueue-core":
                continue
            extension: Callable[[Registry], None] = entry_point.load()
            extension(registry)
        return registry
