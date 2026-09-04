from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pxr import Sdf

from .context import RuntimeContext
from .errors import GraphValidationError
from .graph import Graph, Node
from .model_runtime import ModelPool
from .registry import NodeResult, Registry
from .runstore import SUCCESS_STATES, TERMINAL_STATES, RunStore


@dataclass(frozen=True)
class RunSummary:
    path: Path
    state: str
    statuses: dict[str, str]

    @property
    def succeeded(self) -> bool:
        return self.state == "succeeded"


class Engine:
    def __init__(
        self,
        registry: Registry | None = None,
        *,
        llama_server: str | None = None,
        event_callback: Any | None = None,
    ) -> None:
        self.registry = registry or Registry.discover()
        self.llama_server = llama_server
        self.event_callback = event_callback or (lambda _event, _path, _message: None)

    def validate(self, graph_file: str | Path, graph_path: str | None = None) -> Graph:
        graph = Graph(graph_file, self.registry, graph_path)
        diagnostics = graph.validate()
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            detail = "\n".join(f"{item.code} {item.path}: {item.message}" for item in errors)
            raise GraphValidationError(detail)
        return graph

    def run(
        self,
        graph_file: str | Path,
        *,
        graph_path: str | None = None,
        output: str | Path | None = None,
        agent: str | None = None,
        model: str | Path | None = None,
        max_concurrency: int | None = None,
    ) -> RunSummary:
        graph = self.validate(graph_file, graph_path)
        output_path = Path(output).resolve() if output else graph.file_path.with_suffix(".run.usda")
        if output_path.exists():
            raise FileExistsError(f"Run file already exists: {output_path}; use enqueue resume or --output")
        store = RunStore.create(output_path, graph.file_path, graph.prim.GetPath(), graph_digest=graph.digest)
        return self._drive(graph, store, agent=agent, model=model, max_concurrency=max_concurrency)

    def resume(
        self,
        run_file: str | Path,
        *,
        agent: str | None = None,
        model: str | Path | None = None,
        max_concurrency: int | None = None,
    ) -> RunSummary:
        store = RunStore.open(run_file)
        graph = self.validate(store.graph_file, str(store.graph_path))
        expected = store.stage.GetPrimAtPath(store.ROOT).GetAttribute("enqueue:definitionDigest").Get()
        actual = graph.digest
        if expected != actual:
            raise GraphValidationError(f"Graph changed since this run was created ({expected} != {actual})")
        store.reset_interrupted(list(graph.nodes))
        return self._drive(graph, store, agent=agent, model=model, max_concurrency=max_concurrency)

    def _drive(
        self,
        graph: Graph,
        store: RunStore,
        *,
        agent: str | None,
        model: str | Path | None,
        max_concurrency: int | None,
    ) -> RunSummary:
        concurrency = max(1, max_concurrency or graph.max_concurrency)
        model_override = Path(model).resolve() if model else None
        statuses = {str(path): store.state(path) for path in graph.nodes}
        outputs: dict[tuple[str, str], Any] = {}
        for path, node in graph.nodes.items():
            if statuses[str(path)] in SUCCESS_STATES:
                for name, value in store.outputs_for(path).items():
                    outputs[(str(path), name)] = value
                if (str(path), "outputs:result") not in outputs:
                    result = store.result_for(path)
                    if result:
                        outputs[(str(path), "outputs:result")] = result
        store.set_run_state("running")
        self.event_callback("run", str(graph.prim.GetPath()), f"running with concurrency {concurrency}")
        log_directory = store.path.with_suffix("").with_name(store.path.stem + ".runtime")
        try:
            with ModelPool(binary=self.llama_server, log_directory=log_directory) as model_pool, ThreadPoolExecutor(max_workers=concurrency) as pool:
                while True:
                    progressed = False
                    for path, node in graph.nodes.items():
                        key = str(path)
                        if statuses[key] != "waiting":
                            continue
                        dependencies = [str(dep) for dep in node.dependencies if dep in graph.nodes]
                        if any(statuses[dep] in TERMINAL_STATES - SUCCESS_STATES for dep in dependencies):
                            reason = "Blocked by failed dependency"
                            store.finish(path, "blocked", reason=reason)
                            statuses[key] = "blocked"
                            self.event_callback("blocked", key, reason)
                            progressed = True
                        elif not node.enabled:
                            store.finish(path, "skipped", reason="Node disabled")
                            statuses[key] = "skipped"
                            progressed = True
                    ready = [
                        node for path, node in graph.nodes.items()
                        if statuses[str(path)] == "waiting"
                        and all(statuses[str(dep)] in SUCCESS_STATES for dep in node.dependencies if dep in graph.nodes)
                    ]
                    if ready:
                        batch = ready[:concurrency]
                        futures: dict[Future[NodeResult], Node] = {}
                        for node in batch:
                            store.begin(node.path)
                            statuses[str(node.path)] = "running"
                            inputs = graph.resolve_inputs(node, outputs)
                            context = RuntimeContext(
                                prim=node.prim,
                                inputs=inputs,
                                graph=graph,
                                run_store=store,
                                model_pool=model_pool,
                                model_override=model_override,
                                agent_override=agent,
                            )
                            self.event_callback("started", str(node.path), node.spec.schema_class)
                            futures[pool.submit(node.spec.handler.execute, context)] = node
                        for future in as_completed(futures):
                            node = futures[future]
                            key = str(node.path)
                            try:
                                result = future.result()
                                store.finish(
                                    node.path,
                                    "succeeded",
                                    result=result.result,
                                    reason=result.reason,
                                    external_session_id=result.external_session_id,
                                    external_command_id=result.external_command_id,
                                    outputs=result.outputs,
                                )
                                if result.result and node.spec.category in {"agent", "runtime"}:
                                    store.write_artifact(node.path, result.result)
                                statuses[key] = "succeeded"
                                for name, value in result.outputs.items():
                                    outputs[(key, name)] = value
                                self.event_callback("succeeded", key, result.reason or "complete")
                            except Exception as exc:
                                attempts = int(store.ensure_node(node.path).GetAttribute("enqueue:attempt").Get() or 1)
                                maximum, delay = self._retry_policy(node)
                                if attempts < maximum:
                                    store.retry(node.path, str(exc))
                                    statuses[key] = "waiting"
                                    self.event_callback("retry", key, f"attempt {attempts}/{maximum}: {exc}")
                                    if delay:
                                        time.sleep(delay)
                                else:
                                    store.finish(node.path, "failed", reason=str(exc))
                                    statuses[key] = "failed"
                                    self.event_callback("failed", key, str(exc))
                        progressed = True
                    if all(state in TERMINAL_STATES for state in statuses.values()):
                        break
                    if not progressed:
                        for path, state in list(statuses.items()):
                            if state == "waiting":
                                store.finish(Sdf.Path(path), "blocked", reason="No satisfiable execution frontier")
                                statuses[path] = "blocked"
                        break
        except KeyboardInterrupt:
            store.set_run_state("paused")
            raise
        final_state = "succeeded" if all(value in SUCCESS_STATES for value in statuses.values()) else "failed"
        store.set_run_state(final_state)
        self.event_callback("run", str(graph.prim.GetPath()), final_state)
        return RunSummary(store.path, final_state, statuses)

    @staticmethod
    def _retry_policy(node: Node) -> tuple[int, float]:
        relationship = node.prim.GetRelationship("enqueue:retryPolicy")
        targets = relationship.GetTargets() if relationship else []
        if not targets:
            return 1, 0
        policy = node.prim.GetStage().GetPrimAtPath(targets[0])
        maximum = int(policy.GetAttribute("enqueue:maxAttempts").Get() or 1)
        delay = float(policy.GetAttribute("enqueue:initialDelaySeconds").Get() or 0)
        return max(1, maximum), max(0, delay)
