from __future__ import annotations

import shutil
from pathlib import Path

from pxr import Sdf, Usd


def schema_directory() -> Path:
    return Path(__file__).with_name("schemas")


def _typed(stage: Usd.Stage, path: str, type_name: str) -> Usd.Prim:
    prim = stage.DefinePrim(path, type_name)
    prim.GetInherits().AddInherit(f"/{type_name}")
    return prim


def create_starter_graph(
    output: str | Path,
    *,
    model: str | Path | None = None,
    domain: str | Path = ".",
) -> Path:
    path = Path(output).resolve()
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    local_schema = path.parent / f"{path.stem}.schemas"
    local_schema.mkdir(parents=True, exist_ok=True)
    for name in ("core.usda", "liveshell.usda"):
        shutil.copy2(schema_directory() / name, local_schema / name)
    stage = Usd.Stage.CreateNew(str(path))
    stage.GetRootLayer().subLayerPaths = [f"{local_schema.name}/liveshell.usda"]
    graph = _typed(stage, "/Tasks", "EnqueueGraph")
    stage.SetDefaultPrim(graph)
    graph.CreateAttribute("enqueue:maxConcurrency", Sdf.ValueTypeNames.Int).Set(1)
    for scope in ("Agents", "Policies", "Sources", "Nodes"):
        stage.DefinePrim(f"/Tasks/{scope}", "Scope")

    policy = _typed(stage, "/Tasks/Policies/RepositoryTools", "EnqueueToolPolicy")
    policy.CreateAttribute("enqueue:allowedTools", Sdf.ValueTypeNames.TokenArray).Set(
        ["read_file", "search_files", "write_file", "replace_in_file", "run_shell"]
    )
    policy.CreateAttribute("enqueue:readableRoots", Sdf.ValueTypeNames.AssetArray).Set([Sdf.AssetPath(str(domain))])
    policy.CreateAttribute("enqueue:writableRoots", Sdf.ValueTypeNames.AssetArray).Set([Sdf.AssetPath(str(domain))])

    agent = _typed(stage, "/Tasks/Agents/Worker", "EnqueueAgentDefinition")
    agent.CreateAttribute("enqueue:instructions", Sdf.ValueTypeNames.String).Set(
        "Complete the assigned task using the available tools. Inspect before editing and verify the result."
    )
    agent.CreateAttribute("enqueue:maxTurns", Sdf.ValueTypeNames.Int).Set(12)
    agent.CreateRelationship("enqueue:toolPolicy").SetTargets([policy.GetPath()])
    graph.CreateRelationship("enqueue:defaultAgent").SetTargets([agent.GetPath()])

    objective = _typed(stage, "/Tasks/Sources/Objective", "EnqueueTextSource")
    objective.CreateAttribute("outputs:value", Sdf.ValueTypeNames.String).Set("Describe the task here.")
    repository = _typed(stage, "/Tasks/Sources/Repository", "EnqueueFileSource")
    repository.CreateAttribute("outputs:value", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(domain)))
    repository.CreateAttribute("enqueue:access", Sdf.ValueTypeNames.Token).Set("readWrite")
    model_source = _typed(stage, "/Tasks/Sources/Model", "EnqueueModelSource")
    if model:
        model_source.CreateAttribute("outputs:value", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(model)))
    model_source.CreateAttribute("enqueue:reasoning", Sdf.ValueTypeNames.Token).Set("off")
    graph.CreateRelationship("enqueue:defaultModel").SetTargets([model_source.GetPath()])

    start = _typed(stage, "/Tasks/Nodes/Start", "EnqueueStart")
    work = _typed(stage, "/Tasks/Nodes/Work", "EnqueueAgent")
    work.CreateAttribute("inputs:task", Sdf.ValueTypeNames.String).SetConnections([objective.GetPath().AppendProperty("outputs:value")])
    work.CreateAttribute("inputs:domain", Sdf.ValueTypeNames.Asset).SetConnections([repository.GetPath().AppendProperty("outputs:value")])
    work.CreateAttribute("inputs:model", Sdf.ValueTypeNames.Asset).SetConnections([model_source.GetPath().AppendProperty("outputs:value")])
    work.CreateAttribute("outputs:result", Sdf.ValueTypeNames.String)
    work.CreateRelationship("enqueue:requires").SetTargets([start.GetPath()])
    end = _typed(stage, "/Tasks/Nodes/Complete", "EnqueueEnd")
    end.CreateRelationship("enqueue:requires").SetTargets([work.GetPath()])
    stage.GetRootLayer().Save()
    return path
