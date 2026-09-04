from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pxr import Sdf, Tf

from .authoring import create_starter_graph, schema_directory
from .engine import Engine
from .errors import EnqueueError
from .graph import Graph
from .registry import Registry
from .runstore import RunStore


def _event(kind: str, path: str, message: str) -> None:
    print(f"[{kind:9}] {path}: {message}", flush=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="enqueue", description="Run extensible USD task graphs")
    root.add_argument("--llama-server", help="Path to llama-server executable")
    commands = root.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="Validate a composed USD graph")
    validate.add_argument("file")
    validate.add_argument("--graph")

    run = commands.add_parser("run", help="Create and execute a run")
    run.add_argument("file")
    run.add_argument("--graph")
    run.add_argument("--output")
    run.add_argument("--agent", help="Agent-definition prim path overriding graph bindings")
    run.add_argument("--model", help="GGUF path overriding connected model sources")
    run.add_argument("--max-concurrency", type=int)

    resume = commands.add_parser("resume", help="Resume an interrupted run")
    resume.add_argument("file")
    resume.add_argument("--agent")
    resume.add_argument("--model")
    resume.add_argument("--max-concurrency", type=int)

    status = commands.add_parser("status", help="Inspect a run stage")
    status.add_argument("file")

    catalog = commands.add_parser("catalog", help="List installed node handlers")
    catalog.set_defaults(command="catalog")

    inspect = commands.add_parser("inspect", help="Inspect nodes, ports, and dependencies")
    inspect.add_argument("file")
    inspect.add_argument("--graph")

    init = commands.add_parser("init", help="Create an extensible starter USD graph")
    init.add_argument("file")
    init.add_argument("--model")
    init.add_argument("--domain", default=".")

    schema = commands.add_parser("schema", help="Print the bundled schema directory")
    schema.set_defaults(command="schema")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        registry = Registry.discover()
        if args.command == "schema":
            print(schema_directory())
            return 0
        if args.command == "init":
            created = create_starter_graph(args.file, model=args.model, domain=args.domain)
            print(f"created: {created}")
            return 0
        if args.command == "catalog":
            for spec in registry.specs():
                print(f"{spec.schema_class}\t{spec.category}\t{','.join(spec.outputs)}")
            return 0
        if args.command == "inspect":
            graph = Graph(args.file, registry, args.graph)
            print(f"graph {graph.prim.GetPath()} ({len(graph.nodes)} nodes, concurrency {graph.max_concurrency})")
            for node in graph.nodes.values():
                dependencies = ", ".join(str(item) for item in sorted(node.dependencies)) or "-"
                print(f"{node.path} [{node.spec.category}] {node.spec.schema_class}")
                print(f"  requires: {dependencies}")
                for attr in node.prim.GetAttributes():
                    if attr.GetName().startswith(("inputs:", "outputs:")):
                        connections = ", ".join(str(item) for item in attr.GetConnections())
                        value = attr.Get() if not connections else ""
                        detail = f" -> {connections}" if connections else (f" = {value}" if value is not None else "")
                        print(f"  {attr.GetTypeName()} {attr.GetName()}{detail}")
            return 0
        if args.command == "status":
            store = RunStore.open(args.file)
            root = store.stage.GetPrimAtPath(store.ROOT)
            print(f"run: {root.GetAttribute('enqueue:state').Get()}")
            for path, state in sorted(store.statuses().items()):
                reason = store.ensure_node(Sdf.Path(path)).GetAttribute("enqueue:reason").Get() or ""
                print(f"{state:10} {path}{' — ' + reason if reason else ''}")
            return 0 if str(root.GetAttribute("enqueue:state").Get()) == "succeeded" else 1
        engine = Engine(registry, llama_server=args.llama_server, event_callback=_event)
        if args.command == "validate":
            graph = Graph(args.file, registry, args.graph)
            diagnostics = graph.validate()
            for item in diagnostics:
                print(f"{item.severity}: {item.code} {item.path}: {item.message}")
            if any(item.severity == "error" for item in diagnostics):
                return 2
            print(f"valid: {graph.file_path} ({len(graph.nodes)} nodes)")
            return 0
        if args.command == "run":
            summary = engine.run(args.file, graph_path=args.graph, output=args.output, agent=args.agent, model=args.model, max_concurrency=args.max_concurrency)
        else:
            summary = engine.resume(args.file, agent=args.agent, model=args.model, max_concurrency=args.max_concurrency)
        print(f"{summary.state}: {summary.path}")
        return 0 if summary.succeeded else 1
    except (EnqueueError, FileNotFoundError, FileExistsError, ValueError, Tf.ErrorException) as exc:
        print(f"enqueue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
