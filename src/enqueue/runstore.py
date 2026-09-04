from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pxr import Sdf, Usd


TERMINAL_STATES = {"succeeded", "failed", "blocked", "skipped", "canceled", "timedOut"}
SUCCESS_STATES = {"succeeded", "skipped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def definition_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _attr(prim: Usd.Prim, name: str, type_name: Sdf.ValueTypeName, value: Any) -> None:
    prim.CreateAttribute(name, type_name, custom=True).Set(value)


class RunStore:
    """Single-writer durable USD run record."""

    ROOT = Sdf.Path("/Run")

    def __init__(self, path: Path, stage: Usd.Stage, graph_file: Path, graph_path: Sdf.Path):
        self.path = path
        self.stage = stage
        self.graph_file = graph_file
        self.graph_path = graph_path
        self._lock = threading.RLock()

    @classmethod
    def create(
        cls,
        path: str | Path,
        graph_file: Path,
        graph_path: Sdf.Path,
        *,
        graph_digest: str | None = None,
    ) -> "RunStore":
        output = Path(path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Usd.Stage.CreateNew(str(output))
        relative_graph = Path(graph_file).resolve()
        try:
            relative_graph_text = relative_graph.relative_to(output.parent).as_posix()
        except ValueError:
            relative_graph_text = relative_graph.as_posix()
        stage.GetRootLayer().subLayerPaths = [relative_graph_text]
        root = stage.DefinePrim(cls.ROOT, "EnqueueRun")
        root.GetInherits().AddInherit("/EnqueueRun")
        stage.SetDefaultPrim(root)
        root.CreateRelationship("enqueue:graph", custom=True).SetTargets([graph_path])
        _attr(root, "enqueue:state", Sdf.ValueTypeNames.Token, "waiting")
        _attr(root, "enqueue:definitionDigest", Sdf.ValueTypeNames.String, graph_digest or definition_digest(graph_file))
        _attr(root, "enqueue:createdAt", Sdf.ValueTypeNames.String, utc_now())
        stage.DefinePrim(cls.ROOT.AppendChild("NodeRuns"), "Scope")
        stage.DefinePrim(cls.ROOT.AppendChild("Artifacts"), "Scope")
        stage.GetRootLayer().Save()
        return cls(output, stage, graph_file, graph_path)

    @classmethod
    def open(cls, path: str | Path) -> "RunStore":
        run_path = Path(path).resolve()
        stage = Usd.Stage.Open(str(run_path))
        if stage is None:
            raise ValueError(f"Could not open run stage: {run_path}")
        root = stage.GetPrimAtPath(cls.ROOT)
        if not root:
            raise ValueError(f"Not an Enqueue run stage: {run_path}")
        graph_targets = root.GetRelationship("enqueue:graph").GetTargets()
        if not graph_targets:
            raise ValueError("Run has no enqueue:graph target")
        layers = stage.GetRootLayer().subLayerPaths
        if not layers:
            raise ValueError("Run has no graph sublayer")
        graph_file = Path(layers[0])
        if not graph_file.is_absolute():
            graph_file = run_path.parent / graph_file
        return cls(run_path, stage, graph_file.resolve(), graph_targets[0])

    def node_run_path(self, node_path: Sdf.Path) -> Sdf.Path:
        return Sdf.Path(str(self.ROOT.AppendChild("NodeRuns")) + str(node_path))

    def ensure_node(self, node_path: Sdf.Path) -> Usd.Prim:
        path = self.node_run_path(node_path)
        prim = self.stage.GetPrimAtPath(path)
        if prim:
            return prim
        parent = path.GetParentPath()
        ancestors = []
        while parent != self.ROOT.AppendChild("NodeRuns") and not self.stage.GetPrimAtPath(parent):
            ancestors.append(parent)
            parent = parent.GetParentPath()
        for ancestor in reversed(ancestors):
            self.stage.DefinePrim(ancestor, "Scope")
        prim = self.stage.DefinePrim(path, "EnqueueNodeRun")
        prim.GetInherits().AddInherit("/EnqueueNodeRun")
        prim.CreateRelationship("enqueue:node", custom=True).SetTargets([node_path])
        _attr(prim, "enqueue:state", Sdf.ValueTypeNames.Token, "waiting")
        _attr(prim, "enqueue:attempt", Sdf.ValueTypeNames.Int, 0)
        return prim

    def state(self, node_path: Sdf.Path) -> str:
        prim = self.ensure_node(node_path)
        attr = prim.GetAttribute("enqueue:state")
        return str(attr.Get() or "waiting")

    def set_run_state(self, state: str) -> None:
        with self._lock:
            root = self.stage.GetPrimAtPath(self.ROOT)
            _attr(root, "enqueue:state", Sdf.ValueTypeNames.Token, state)
            if state in TERMINAL_STATES or state == "succeeded":
                _attr(root, "enqueue:finishedAt", Sdf.ValueTypeNames.String, utc_now())
            self.save()

    def begin(self, node_path: Sdf.Path) -> int:
        with self._lock:
            prim = self.ensure_node(node_path)
            attempt = int(prim.GetAttribute("enqueue:attempt").Get() or 0) + 1
            _attr(prim, "enqueue:attempt", Sdf.ValueTypeNames.Int, attempt)
            _attr(prim, "enqueue:state", Sdf.ValueTypeNames.Token, "running")
            _attr(prim, "enqueue:startedAt", Sdf.ValueTypeNames.String, utc_now())
            _attr(prim, "enqueue:finishedAt", Sdf.ValueTypeNames.String, "")
            attempt_scope = self.stage.DefinePrim(prim.GetPath().AppendChild("Attempts"), "Scope")
            attempt_prim = self.stage.DefinePrim(attempt_scope.GetPath().AppendChild(f"Attempt_{attempt}"), "EnqueueAttempt")
            attempt_prim.GetInherits().AddInherit("/EnqueueAttempt")
            attempt_prim.CreateRelationship("enqueue:nodeRun", custom=True).SetTargets([prim.GetPath()])
            _attr(attempt_prim, "enqueue:number", Sdf.ValueTypeNames.Int, attempt)
            _attr(attempt_prim, "enqueue:state", Sdf.ValueTypeNames.Token, "running")
            _attr(attempt_prim, "enqueue:startedAt", Sdf.ValueTypeNames.String, utc_now())
            self.save()
            return attempt

    def finish(
        self,
        node_path: Sdf.Path,
        state: str,
        *,
        result: str = "",
        reason: str = "",
        external_session_id: str = "",
        external_command_id: str = "",
        outputs: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            prim = self.ensure_node(node_path)
            _attr(prim, "enqueue:state", Sdf.ValueTypeNames.Token, state)
            _attr(prim, "enqueue:result", Sdf.ValueTypeNames.String, result)
            _attr(prim, "enqueue:reason", Sdf.ValueTypeNames.String, reason)
            _attr(prim, "enqueue:finishedAt", Sdf.ValueTypeNames.String, utc_now())
            _attr(prim, "enqueue:externalSessionId", Sdf.ValueTypeNames.String, external_session_id)
            _attr(prim, "enqueue:externalCommandId", Sdf.ValueTypeNames.String, external_command_id)
            for name, value in (outputs or {}).items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    value_type = Sdf.ValueTypeNames.Bool
                elif isinstance(value, int):
                    value_type = Sdf.ValueTypeNames.Int
                elif isinstance(value, float):
                    value_type = Sdf.ValueTypeNames.Double
                elif isinstance(value, Sdf.AssetPath):
                    value_type = Sdf.ValueTypeNames.Asset
                else:
                    value_type = Sdf.ValueTypeNames.String
                    value = str(value)
                _attr(prim, name, value_type, value)
            attempt = int(prim.GetAttribute("enqueue:attempt").Get() or 0)
            attempt_prim = self.stage.GetPrimAtPath(prim.GetPath().AppendPath(f"Attempts/Attempt_{attempt}"))
            if attempt_prim:
                _attr(attempt_prim, "enqueue:state", Sdf.ValueTypeNames.Token, state)
                _attr(attempt_prim, "enqueue:finishedAt", Sdf.ValueTypeNames.String, utc_now())
                _attr(attempt_prim, "enqueue:reason", Sdf.ValueTypeNames.String, reason)
            self.save()

    def reset_interrupted(self, node_paths: list[Sdf.Path]) -> None:
        with self._lock:
            for path in node_paths:
                prim = self.ensure_node(path)
                if str(prim.GetAttribute("enqueue:state").Get() or "") == "running":
                    _attr(prim, "enqueue:state", Sdf.ValueTypeNames.Token, "waiting")
                    _attr(prim, "enqueue:reason", Sdf.ValueTypeNames.String, "Interrupted run recovered for retry")
            self.save()

    def retry(self, node_path: Sdf.Path, reason: str) -> None:
        with self._lock:
            prim = self.ensure_node(node_path)
            attempt = int(prim.GetAttribute("enqueue:attempt").Get() or 0)
            attempt_prim = self.stage.GetPrimAtPath(prim.GetPath().AppendPath(f"Attempts/Attempt_{attempt}"))
            if attempt_prim:
                _attr(attempt_prim, "enqueue:state", Sdf.ValueTypeNames.Token, "failed")
                _attr(attempt_prim, "enqueue:finishedAt", Sdf.ValueTypeNames.String, utc_now())
                _attr(attempt_prim, "enqueue:reason", Sdf.ValueTypeNames.String, reason)
            _attr(prim, "enqueue:state", Sdf.ValueTypeNames.Token, "waiting")
            _attr(prim, "enqueue:reason", Sdf.ValueTypeNames.String, reason)
            self.save()

    def write_artifact(self, node_path: Sdf.Path, text: str, name: str = "result") -> Path:
        with self._lock:
            safe_parts = [part for part in str(node_path).split("/") if part]
            directory = self.path.with_suffix("").with_name(self.path.stem + ".artifacts").joinpath(*safe_parts)
            directory.mkdir(parents=True, exist_ok=True)
            content_path = directory / f"{name}.txt"
            content_path.write_text(text, encoding="utf-8")
            artifact_name = "_".join(safe_parts + [name])
            artifact_path = self.ROOT.AppendChild("Artifacts").AppendChild(artifact_name)
            artifact = self.stage.DefinePrim(artifact_path, "EnqueueTextArtifact")
            artifact.GetInherits().AddInherit("/EnqueueTextArtifact")
            artifact.CreateRelationship("enqueue:producer", custom=True).SetTargets([self.node_run_path(node_path)])
            try:
                authored_content = content_path.relative_to(self.path.parent).as_posix()
            except ValueError:
                authored_content = content_path.as_posix()
            _attr(artifact, "enqueue:content", Sdf.ValueTypeNames.Asset, Sdf.AssetPath(authored_content))
            _attr(artifact, "enqueue:mediaType", Sdf.ValueTypeNames.String, "text/plain")
            _attr(artifact, "enqueue:digest", Sdf.ValueTypeNames.String, definition_digest(content_path))
            node_run = self.ensure_node(node_path)
            node_run.CreateRelationship("enqueue:artifacts", custom=True).AddTarget(artifact_path)
            self.save()
            return content_path

    def statuses(self) -> dict[str, str]:
        result: dict[str, str] = {}
        root = self.stage.GetPrimAtPath(self.ROOT.AppendChild("NodeRuns"))
        for prim in Usd.PrimRange(root):
            if prim.GetTypeName() == "EnqueueNodeRun":
                targets = prim.GetRelationship("enqueue:node").GetTargets()
                if targets:
                    result[str(targets[0])] = str(prim.GetAttribute("enqueue:state").Get() or "waiting")
        return result

    def result_for(self, node_path: Sdf.Path) -> str:
        prim = self.ensure_node(node_path)
        return str(prim.GetAttribute("enqueue:result").Get() or "")

    def outputs_for(self, node_path: Sdf.Path) -> dict[str, Any]:
        prim = self.ensure_node(node_path)
        return {
            attr.GetName(): attr.Get()
            for attr in prim.GetAttributes()
            if attr.GetName().startswith("outputs:") and attr.Get() is not None
        }

    def save(self) -> None:
        self.stage.GetRootLayer().Save()
