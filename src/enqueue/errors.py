class EnqueueError(RuntimeError):
    """Base error for user-actionable Enqueue failures."""


class GraphValidationError(EnqueueError):
    """The composed USD graph is structurally or semantically invalid."""


class ExecutionError(EnqueueError):
    """A node runtime could not execute its work."""


class ModelRuntimeError(ExecutionError):
    """The local model server could not start or return a usable response."""
