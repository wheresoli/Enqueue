from pathlib import Path

from pxr import Sdf

from enqueue.graph import Graph
from enqueue.registry import Registry


SCHEMA = Path(__file__).parents[1] / "src" / "enqueue" / "schemas" / "liveshell.usda"


def write_graph(path: Path, body: str) -> None:
    path.write_text(
        f'''#usda 1.0
(
    subLayers = [@{SCHEMA.as_posix()}@]
    defaultPrim = "Graph"
)
def EnqueueGraph "Graph" (inherits=</EnqueueGraph>) {{
{body}
}}
''', encoding="utf-8")


def test_connections_are_dependencies(tmp_path):
    path = tmp_path / "graph.usda"
    write_graph(path, '''
    def EnqueueTextSource "A" (inherits=</EnqueueTextSource>) {
        string outputs:value = "work"
    }
    def EnqueueAgent "B" (inherits=</EnqueueAgent>) {
        string inputs:task.connect = </Graph/A.outputs:value>
        string outputs:result
    }
''')
    graph = Graph(path, Registry.discover())
    assert graph.validate() == []
    assert Sdf.Path("/Graph/A") in graph.nodes[Sdf.Path("/Graph/B")].dependencies


def test_type_mismatch_is_rejected(tmp_path):
    path = tmp_path / "graph.usda"
    write_graph(path, '''
    def EnqueueModelSource "A" (inherits=</EnqueueModelSource>) {
        asset outputs:value = @model.gguf@
    }
    def EnqueueAgent "B" (inherits=</EnqueueAgent>) {
        string inputs:task.connect = </Graph/A.outputs:value>
        string outputs:result
    }
''')
    diagnostics = Graph(path, Registry.discover()).validate()
    assert any(item.code == "port_type_mismatch" for item in diagnostics)


def test_cycle_is_rejected(tmp_path):
    path = tmp_path / "graph.usda"
    write_graph(path, '''
    def EnqueueStart "A" (inherits=</EnqueueStart>) {
        rel enqueue:requires = </Graph/B>
    }
    def EnqueueEnd "B" (inherits=</EnqueueEnd>) {
        rel enqueue:requires = </Graph/A>
    }
''')
    diagnostics = Graph(path, Registry.discover()).validate()
    assert any(item.code == "cycle" for item in diagnostics)


def test_known_schema_without_handler_is_rejected(tmp_path):
    path = tmp_path / "graph.usda"
    write_graph(path, '''
    def EnqueueConditional "Choice" (inherits=</EnqueueConditional>) {
        string inputs:input = "a"
        string inputs:compare = "b"
    }
''')
    diagnostics = Graph(path, Registry.discover()).validate()
    assert any(item.code == "unsupported_node" for item in diagnostics)
