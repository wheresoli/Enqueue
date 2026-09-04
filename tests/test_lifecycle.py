from pxr import Sdf

from enqueue.engine import Engine
from enqueue.registry import NodeResult, NodeSpec, Registry
from enqueue.runstore import RunStore

from .test_graph import write_graph


class FailOnce:
    def __init__(self):
        self.calls = 0

    def execute(self, _context):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient")
        return NodeResult(outputs={"outputs:value": "ok"}, result="ok")


class AlwaysFail:
    def execute(self, _context):
        raise RuntimeError("permanent")


class Succeed:
    def execute(self, _context):
        return NodeResult(result="ok")


def test_retry_attempts_and_downstream_completion(tmp_path):
    path = tmp_path / "retry.usda"
    write_graph(path, '''
    def EnqueueRetryPolicy "Retry" (inherits=</EnqueueRetryPolicy>) {
        int enqueue:maxAttempts = 2
    }
    def TestRetry "A" (inherits=</EnqueueExecutable>) {
        rel enqueue:retryPolicy = </Graph/Retry>
        string outputs:value
    }
    def TestSuccess "B" (inherits=</EnqueueExecutable>) {
        rel enqueue:requires = </Graph/A>
    }
''')
    registry = Registry.discover()
    handler = FailOnce()
    registry.register(NodeSpec("/TestRetry", "utility", handler, outputs=("outputs:value",)))
    registry.register(NodeSpec("/TestSuccess", "utility", Succeed()))
    output = tmp_path / "retry.run.usda"
    summary = Engine(registry).run(path, output=output)
    assert summary.succeeded
    assert handler.calls == 2
    store = RunStore.open(output)
    node_run = store.ensure_node(Sdf.Path("/Graph/A"))
    assert node_run.GetAttribute("enqueue:attempt").Get() == 2
    assert store.stage.GetPrimAtPath(node_run.GetPath().AppendPath("Attempts/Attempt_1")).GetAttribute("enqueue:state").Get() == "failed"


def test_failure_blocks_downstream(tmp_path):
    path = tmp_path / "failure.usda"
    write_graph(path, '''
    def TestFailure "A" (inherits=</EnqueueExecutable>) {}
    def TestSuccess "B" (inherits=</EnqueueExecutable>) {
        rel enqueue:requires = </Graph/A>
    }
''')
    registry = Registry.discover()
    registry.register(NodeSpec("/TestFailure", "utility", AlwaysFail()))
    registry.register(NodeSpec("/TestSuccess", "utility", Succeed()))
    summary = Engine(registry).run(path, output=tmp_path / "failure.run.usda")
    assert not summary.succeeded
    assert summary.statuses["/Graph/A"] == "failed"
    assert summary.statuses["/Graph/B"] == "blocked"
