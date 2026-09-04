from enqueue.authoring import create_starter_graph
from enqueue.graph import Graph
from enqueue.registry import Registry


def test_starter_graph_is_self_contained_and_valid(tmp_path):
    path = create_starter_graph(tmp_path / "tasks.usda", model=tmp_path / "model.gguf", domain=tmp_path)
    graph = Graph(path, Registry.discover())
    assert graph.validate() == []
    assert len(graph.nodes) == 6
    assert (tmp_path / "tasks.schemas" / "core.usda").is_file()
    assert (tmp_path / "tasks.schemas" / "liveshell.usda").is_file()
