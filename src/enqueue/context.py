from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pxr import Usd

from .graph import Graph
from .model_runtime import ModelPool
from .runstore import RunStore


@dataclass
class RuntimeContext:
    prim: Usd.Prim
    inputs: dict[str, Any]
    graph: Graph
    run_store: RunStore
    model_pool: ModelPool
    model_override: Path | None = None
    agent_override: str | None = None

    @property
    def graph_directory(self) -> str:
        return str(self.graph.file_path.parent)

    def attr(self, name: str, default: Any = None, prim: Usd.Prim | None = None) -> Any:
        attr = (prim or self.prim).GetAttribute(name)
        if not attr:
            return default
        value = attr.Get()
        return default if value is None else value
