from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Iterable

from pxr import Sdf, Usd

from .registry import NodeSpec, Registry


@dataclass(frozen=True)
class ValidationDiagnostic:
    severity: str
    code: str
    path: str
    message: str


def inherited_paths(prim: Usd.Prim) -> set[str]:
    paths = {str(path) for path in prim.GetInherits().GetAllDirectInherits()}
    if prim.GetTypeName():
        paths.add(f"/{prim.GetTypeName()}")
    return paths


def inherits(prim: Usd.Prim, class_name: str) -> bool:
    return f"/{class_name}" in inherited_paths(prim)


def asset_value(value: Any) -> str:
    if isinstance(value, Sdf.AssetPath):
        return value.resolvedPath or value.path
    return "" if value is None else str(value)


class Node:
    def __init__(self, prim: Usd.Prim, spec: NodeSpec):
        self.prim = prim
        self.spec = spec

    @property
    def path(self) -> Sdf.Path:
        return self.prim.GetPath()

    @property
    def enabled(self) -> bool:
        attr = self.prim.GetAttribute("enqueue:enabled")
        value = attr.Get() if attr else True
        return True if value is None else bool(value)

    @property
    def dependencies(self) -> set[Sdf.Path]:
        result = set(self.prim.GetRelationship("enqueue:requires").GetTargets())
        for attr in self.prim.GetAttributes():
            if attr.GetName().startswith("inputs:"):
                for connection in attr.GetConnections():
                    result.add(connection.GetPrimPath())
        result.discard(self.path)
        return result

    def attribute(self, name: str, default: Any = None) -> Any:
        attr = self.prim.GetAttribute(name)
        if not attr:
            return default
        value = attr.Get()
        return default if value is None else value


class Graph:
    def __init__(self, file_path: str | Path, registry: Registry, graph_path: str | None = None):
        self.file_path = Path(file_path).resolve()
        self.registry = registry
        self.stage = Usd.Stage.Open(str(self.file_path))
        if self.stage is None:
            raise ValueError(f"Could not open USD stage: {self.file_path}")
        if graph_path:
            self.prim = self.stage.GetPrimAtPath(graph_path)
        else:
            self.prim = self.stage.GetDefaultPrim()
            if not self.prim:
                self.prim = next(
                    (p for p in self.stage.GetPseudoRoot().GetChildren() if inherits(p, "EnqueueGraph")),
                    Usd.Prim(),
                )
        if not self.prim:
            raise ValueError("No graph prim found; set defaultPrim or pass --graph")
        self.nodes: dict[Sdf.Path, Node] = {}
        self.unsupported_nodes: list[Usd.Prim] = []
        for prim in Usd.PrimRange(self.prim):
            if prim == self.prim:
                continue
            spec = registry.spec_for(prim)
            if spec:
                self.nodes[prim.GetPath()] = Node(prim, spec)
            elif inherits(prim, "EnqueueNode"):
                self.unsupported_nodes.append(prim)

    @property
    def max_concurrency(self) -> int:
        attr = self.prim.GetAttribute("enqueue:maxConcurrency")
        return max(1, int(attr.Get() or 1)) if attr else 1

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for layer in sorted(self.stage.GetLayerStack(includeSessionLayers=False), key=lambda item: item.identifier):
            digest.update(layer.identifier.encode("utf-8"))
            digest.update(b"\0")
            digest.update(layer.ExportToString().encode("utf-8"))
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def validate(self) -> list[ValidationDiagnostic]:
        diagnostics: list[ValidationDiagnostic] = []
        if not inherits(self.prim, "EnqueueGraph"):
            diagnostics.append(ValidationDiagnostic("error", "not_graph", str(self.prim.GetPath()), "Prim does not inherit EnqueueGraph"))
        if not self.nodes:
            diagnostics.append(ValidationDiagnostic("error", "empty_graph", str(self.prim.GetPath()), "Graph contains no executable or source nodes"))
        for prim in self.unsupported_nodes:
            classes = sorted(path for path in inherited_paths(prim) if path != "/EnqueueNode")
            diagnostics.append(ValidationDiagnostic("error", "unsupported_node", str(prim.GetPath()), f"No installed handler for schema classes: {', '.join(classes)}"))
        node_paths = set(self.nodes)
        for node in self.nodes.values():
            for dependency in node.dependencies:
                if dependency not in node_paths:
                    diagnostics.append(ValidationDiagnostic("error", "dangling_dependency", str(node.path), f"Dependency is not a registered node: {dependency}"))
            for contract in node.spec.inputs:
                attr = node.prim.GetAttribute(contract.name)
                if contract.required and (not attr or (attr.Get() is None and not attr.GetConnections())):
                    diagnostics.append(ValidationDiagnostic("error", "missing_input", str(node.path), f"Required port is not authored or connected: {contract.name}"))
            for attr in node.prim.GetAttributes():
                if not attr.GetName().startswith("inputs:"):
                    continue
                for connection in attr.GetConnections():
                    source = self.stage.GetAttributeAtPath(connection)
                    if not source:
                        diagnostics.append(ValidationDiagnostic("error", "dangling_connection", str(node.path), f"Connection target does not exist: {connection}"))
                    elif source.GetTypeName() != attr.GetTypeName():
                        diagnostics.append(ValidationDiagnostic("error", "port_type_mismatch", str(node.path), f"{attr.GetPath()} ({attr.GetTypeName()}) cannot consume {connection} ({source.GetTypeName()})"))
        cycle = self._cycle()
        if cycle:
            diagnostics.append(ValidationDiagnostic("error", "cycle", str(cycle[0]), "Dependency cycle: " + " -> ".join(str(p) for p in cycle)))
        return diagnostics

    def _cycle(self) -> list[Sdf.Path]:
        visiting: set[Sdf.Path] = set()
        visited: set[Sdf.Path] = set()
        trail: list[Sdf.Path] = []

        def visit(path: Sdf.Path) -> list[Sdf.Path]:
            if path in visiting:
                index = trail.index(path)
                return trail[index:] + [path]
            if path in visited:
                return []
            visiting.add(path)
            trail.append(path)
            for dependency in self.nodes[path].dependencies:
                if dependency in self.nodes:
                    found = visit(dependency)
                    if found:
                        return found
            trail.pop()
            visiting.remove(path)
            visited.add(path)
            return []

        for path in self.nodes:
            found = visit(path)
            if found:
                return found
        return []

    def resolve_inputs(self, node: Node, outputs: dict[tuple[str, str], Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for attr in node.prim.GetAttributes():
            name = attr.GetName()
            if not name.startswith("inputs:"):
                continue
            connections = attr.GetConnections()
            if connections:
                connected = []
                for source_path in connections:
                    key = (str(source_path.GetPrimPath()), source_path.name)
                    if key in outputs:
                        connected.append(outputs[key])
                    else:
                        source = self.stage.GetAttributeAtPath(source_path)
                        connected.append(source.Get() if source else None)
                values[name] = connected if len(connected) > 1 else connected[0]
            else:
                values[name] = attr.Get()
        return values

    def dependency_closure(self, paths: Iterable[Sdf.Path]) -> set[Sdf.Path]:
        pending = list(paths)
        result = set(pending)
        while pending:
            for dependency in self.nodes[pending.pop()].dependencies:
                if dependency in self.nodes and dependency not in result:
                    result.add(dependency)
                    pending.append(dependency)
        return result
