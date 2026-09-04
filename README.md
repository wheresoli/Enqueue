# Enqueue

Enqueue is a USD-native, extensible task-graph runner. Graph definitions, typed
ports, connections, policies, agents, models, run records, attempts, and artifact
provenance are represented as OpenUSD prims and properties. JSON is not a graph
storage format.

```powershell
enqueue validate examples/maintain.usda
enqueue run examples/maintain.usda --model C:\Models\Qwen.gguf
enqueue status examples/maintain.run.usda
enqueue resume examples/maintain.run.usda
```

Create a starter graph and inspect its composed ports and dependencies:

```powershell
enqueue init tasks.usda --model C:\Models\Qwen.gguf --domain C:\Projects\Target
enqueue inspect tasks.usda
```

The bundled node vocabulary includes sources, agents, LiveShell commands and
sessions, terminal nodes, plus schema placeholders for controls, utilities, and
memory. Third-party packages extend the vocabulary through a schema layer and an
`enqueue.nodes` Python entry point.

See [`docs/GRAPH.md`](docs/GRAPH.md) for the graph and extension contract.
