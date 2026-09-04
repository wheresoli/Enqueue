# Enqueue graph contract

Enqueue executes the composed OpenUSD stage directly. There is no portable JSON
document, database graph projection, or hidden compiled graph.

## Vocabulary

Bundled class prims live in `enqueue/schemas/core.usda` and
`enqueue/schemas/liveshell.usda`. Graph assets sublayer the schema assets and
instantiate concrete node classes. A registered schema plugin may eventually
replace the fallback class prims while retaining the same names and properties.

The category hierarchy follows Concurro's engine roles: terminal, control,
agent, runtime, source, utility, and memory. Concrete bundled classes include
start/end, text/file/git/model sources, agent, LiveShell command/session,
conditional, and iterator. Validation rejects a concrete node for which no
runtime handler is installed.

## Ports and edges

Properties named `inputs:*` and `outputs:*` are ports. Their Sdf value type is
their payload type. A USD attribute connection from an input to an output is a
data edge and implicitly makes the source node an execution prerequisite.

`enqueue:requires` is a control-only dependency relationship. Use it when a node
must follow another node but does not consume one of its values. Stateful edge
semantics such as turns may be represented by `EnqueueEdge` prims; the MVP
scheduler executes acyclic sequence and data dependencies only.

## Definitions and runs

A graph asset contains reusable definitions and no execution status. `enqueue
run` creates a separate USD stage whose root layer sublayers the graph. It stores
an `EnqueueRun`, one `EnqueueNodeRun` per visited definition node, child
`EnqueueAttempt` records, and `EnqueueArtifact` provenance. The graph file digest
is recorded; resume refuses to combine a run with a changed definition.

Only the scheduler authors a run root layer. Executors may read the composed
stage but return results to the scheduler rather than editing the shared stage.
This is the single-writer rule that avoids pretending USD is a transactional
multi-writer queue database.

## Runtime extensions

An extension ships:

1. A schema layer defining a class derived from an Enqueue category class.
2. A Python package exposing an `enqueue.nodes` entry point.

The entry point accepts an `enqueue.Registry` and registers a `NodeSpec` keyed by
the absolute schema class path. Runtime selection therefore follows USD schema
inheritance, not a centralized string-kind conditional. Missing handlers are
reported during graph validation while their prims remain inspectable by USD
clients.

## LiveShell and models

LiveShell owns shell processes, durable command events, cancellation, and command
results. Enqueue owns readiness, agent turns, tool authorization, and propagation
through the graph. Raw stdout/stderr events stay in LiveShell; Enqueue records
the external identifiers, terminal values, and artifacts needed for provenance.

GGUF files are served through `llama-server` and called through its
OpenAI-compatible transport. That transport uses JSON internally; JSON is not a
persisted Enqueue graph or run representation.
