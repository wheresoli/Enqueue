from pathlib import Path

import pytest
from pxr import Sdf

from enqueue.context import RuntimeContext
from enqueue.errors import ExecutionError
from enqueue.graph import Graph
from enqueue.registry import Registry
from enqueue.runstore import RunStore
from enqueue.tools import ToolEnvironment

from .test_graph import write_graph


class FakeServer:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": '{"action":"tool","tool":"replace_in_file","arguments":{"path":"target.txt","old":"broken","new":"fixed"}}',
            }
        assert messages[-1]["role"] == "user"
        return {"role": "assistant", "content": '{"action":"final","content":"Fixed target.txt and verified the requested change."}'}


class FakePool:
    def __init__(self):
        self.server = FakeServer()

    def get(self, *_args, **_kwargs):
        return self.server


def test_agent_uses_bounded_tool_loop(tmp_path):
    (tmp_path / "target.txt").write_text("broken", encoding="utf-8")
    (tmp_path / "model.gguf").write_bytes(b"fake")
    graph_file = tmp_path / "agent.usda"
    write_graph(graph_file, f'''
    def EnqueueAgent "Work" (inherits=</EnqueueAgent>) {{
        string inputs:task = "Fix target.txt"
        asset inputs:domain = @{tmp_path.as_posix()}@
        asset inputs:model = @{(tmp_path / "model.gguf").as_posix()}@
        string outputs:result
    }}
''')
    registry = Registry.discover()
    graph = Graph(graph_file, registry)
    node = graph.nodes[Sdf.Path("/Graph/Work")]
    store = RunStore.create(tmp_path / "agent.run.usda", graph_file, graph.prim.GetPath())
    context = RuntimeContext(
        prim=node.prim,
        inputs=graph.resolve_inputs(node, {}),
        graph=graph,
        run_store=store,
        model_pool=FakePool(),  # type: ignore[arg-type]
    )
    result = node.spec.handler.execute(context)
    assert result.outputs["outputs:result"].startswith("Fixed")
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "fixed"


def test_explicit_empty_tool_policy_denies_everything(tmp_path):
    environment = ToolEnvironment(tmp_path, allowed_tools=set())
    assert environment.schemas == []
    with pytest.raises(ExecutionError, match="not allowed"):
        environment.call("read_file", {"path": "anything"})
