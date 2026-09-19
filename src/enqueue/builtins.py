from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from liveshell import LiveShellClient
from pxr import Sdf, Usd

from .context import RuntimeContext
from .errors import ExecutionError
from .graph import asset_value, inherits
from .model_runtime import visible_text
from .registry import NodeResult, NodeSpec, PortContract, Registry
from .tools import ToolEnvironment


def _outputs(prim: Usd.Prim) -> dict[str, Any]:
    return {
        attr.GetName(): attr.Get()
        for attr in prim.GetAttributes()
        if attr.GetName().startswith("outputs:") and attr.Get() is not None
    }


class SourceHandler:
    def execute(self, context: RuntimeContext) -> NodeResult:
        values = _outputs(context.prim)
        if context.inputs.get("inputs:input") is not None and "outputs:value" in values:
            values["outputs:value"] = context.inputs["inputs:input"]
        return NodeResult(outputs=values, result=str(values.get("outputs:value", "")))


class TaskSourceHandler:
    """Render an authored task contract into the agent's text input."""

    def execute(self, context: RuntimeContext) -> NodeResult:
        prim = context.prim

        def value(name: str, default: Any = None) -> Any:
            attr = prim.GetAttribute(name)
            result = attr.Get() if attr else None
            return default if result is None else result

        title = str(value("enqueue:title", prim.GetName()) or prim.GetName())
        lines = [title]
        task_id = str(value("enqueue:taskId", "") or "").strip()
        if task_id:
            lines.append(f"Task ID: {task_id}")
        for heading, name in (
            ("Goals", "enqueue:goals"),
            ("Blockers", "enqueue:blockers"),
            ("Writable roots", "enqueue:writableRoots"),
            ("Reference paths", "enqueue:referencePaths"),
            ("Verification", "enqueue:verifyCommands"),
            ("Notes", "enqueue:notes"),
        ):
            items = [str(item) for item in (value(name, []) or []) if str(item).strip()]
            if items:
                lines.extend((f"\n{heading}:", *(f"- {item}" for item in items)))
        rendered = "\n".join(lines)
        return NodeResult(outputs={"outputs:value": rendered}, result=rendered)


class IteratorHandler:
    """Expose authored iterator items without inventing scheduler semantics."""

    def execute(self, context: RuntimeContext) -> NodeResult:
        raw = context.inputs.get("inputs:input")
        items = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        rendered = "\n\n".join(str(item) for item in items)
        return NodeResult(
            outputs={"outputs:value": rendered, "outputs:done": True},
            result=rendered,
        )


class TerminalHandler:
    def execute(self, context: RuntimeContext) -> NodeResult:
        return NodeResult(outputs=_outputs(context.prim), result="terminal reached")


class CommandDefinitionHandler:
    def execute(self, context: RuntimeContext) -> NodeResult:
        prim = context.prim
        command = context.inputs.get("inputs:command") or context.attr("outputs:command", "")
        if not str(command).strip():
            raise ExecutionError(f"{prim.GetPath()} has no command")
        cwd = context.inputs.get("inputs:workingDirectory") or context.attr("outputs:workingDirectory")
        timeout = context.inputs.get("inputs:timeoutSeconds") if context.inputs.get("inputs:timeoutSeconds") is not None else context.attr("outputs:timeoutSeconds", 300)
        outputs = {
            "outputs:command": str(command),
            "outputs:workingDirectory": cwd,
            "outputs:timeoutSeconds": float(timeout),
        }
        return NodeResult(outputs=outputs, result=str(command))


def _domain_path(value: Any, graph_directory: str) -> Path:
    raw = asset_value(value)
    path = Path(raw) if raw else Path(graph_directory)
    if not path.is_absolute():
        path = Path(graph_directory) / path
    return path.resolve()


class LiveShellSessionHandler:
    def execute(self, context: RuntimeContext) -> NodeResult:
        command = str(context.inputs.get("inputs:command") or "").strip()
        if not command:
            raise ExecutionError(f"{context.prim.GetPath()} requires inputs:command")
        cwd = _domain_path(context.inputs.get("inputs:workingDirectory"), context.graph_directory)
        timeout = float(context.inputs.get("inputs:timeoutSeconds") or 300)
        kind = str(context.attr("enqueue:shellKind", "auto"))
        if kind == "auto":
            kind = "cmd" if os.name == "nt" else "bash"
        state_dir = context.run_store.path.with_suffix("").with_name(context.run_store.path.stem + ".liveshell")
        with LiveShellClient.stdio(state_dir) as client:
            session = client.create_session(kind, cwd=str(cwd))
            try:
                handle = session.start_command(command, timeout_seconds=timeout)
                result = handle.wait()
                status = str(result.command.status)
                if status != "completed" or result.command.exit_code not in (0, None):
                    raise ExecutionError(f"LiveShell command {status} with exit code {result.command.exit_code}: {result.stderr}")
                outputs = {
                    "outputs:stdout": result.stdout,
                    "outputs:stderr": result.stderr,
                    "outputs:exitCode": int(result.command.exit_code or 0),
                }
                return NodeResult(
                    outputs=outputs,
                    result=result.stdout,
                    external_session_id=str(session.session_id),
                    external_command_id=str(handle.command_id),
                )
            finally:
                session.close()


def _relationship_target(prim: Usd.Prim, name: str) -> Usd.Prim | None:
    relationship = prim.GetRelationship(name)
    targets = relationship.GetTargets() if relationship else []
    return prim.GetStage().GetPrimAtPath(targets[0]) if targets else None


def _model_source_for(context: RuntimeContext, agent_definition: Usd.Prim | None) -> Usd.Prim | None:
    connection = context.prim.GetAttribute("inputs:model").GetConnections()
    if connection:
        return context.graph.stage.GetPrimAtPath(connection[0].GetPrimPath())
    if agent_definition:
        target = _relationship_target(agent_definition, "enqueue:model")
        if target:
            return target
    return _relationship_target(context.graph.prim, "enqueue:defaultModel")


class AgentHandler:
    def execute(self, context: RuntimeContext) -> NodeResult:
        agent_definition = None
        if context.agent_override:
            agent_definition = context.graph.stage.GetPrimAtPath(context.agent_override)
            if not agent_definition:
                raise ExecutionError(f"Agent override does not exist: {context.agent_override}")
            if not inherits(agent_definition, "EnqueueAgentDefinition"):
                raise ExecutionError(f"Agent override is not an EnqueueAgentDefinition: {context.agent_override}")
        if agent_definition is None:
            agent_definition = _relationship_target(context.prim, "enqueue:agent")
        if agent_definition is None:
            agent_definition = _relationship_target(context.graph.prim, "enqueue:defaultAgent")

        source = _model_source_for(context, agent_definition)
        model_value = context.model_override or context.inputs.get("inputs:model")
        if model_value is None and source:
            model_value = source.GetAttribute("outputs:value").Get()
        model_path = _domain_path(model_value, context.graph_directory)

        def setting(name: str, default: Any) -> Any:
            node_value = context.attr(name, None)
            if node_value not in (None, "") and node_value != default:
                return node_value
            if agent_definition:
                value = context.attr(name, None, agent_definition)
                if value not in (None, ""):
                    return value
            return default

        instructions = []
        if agent_definition:
            value = context.attr("enqueue:instructions", "", agent_definition)
            if value:
                instructions.append(str(value))
        node_instructions = context.attr("enqueue:instructions", "")
        if node_instructions:
            instructions.append(str(node_instructions))
        system = "\n\n".join(instructions) or "Complete the assigned task and report the result."
        task = context.inputs.get("inputs:task")
        if isinstance(task, list):
            task = "\n\n".join(str(item) for item in task)
        if not str(task or "").strip():
            raise ExecutionError(f"{context.prim.GetPath()} requires a non-empty inputs:task")
        domain = _domain_path(context.inputs.get("inputs:domain"), context.graph_directory)

        allowed_tools: set[str] | None = None
        readable_roots: list[Path] | None = None
        writable_roots: list[Path] | None = None
        policy = _relationship_target(context.prim, "enqueue:toolPolicy")
        if policy is None and agent_definition:
            policy = _relationship_target(agent_definition, "enqueue:toolPolicy")
        if policy:
            authored = policy.GetAttribute("enqueue:allowedTools").Get() or []
            allowed_tools = {str(item) for item in authored}
            readable_roots = [_domain_path(item, context.graph_directory) for item in (policy.GetAttribute("enqueue:readableRoots").Get() or [])]
            writable_roots = [_domain_path(item, context.graph_directory) for item in (policy.GetAttribute("enqueue:writableRoots").Get() or [])]
        environment = ToolEnvironment(
            domain,
            allowed_tools=allowed_tools,
            readable_roots=readable_roots,
            writable_roots=writable_roots,
            state_dir=context.run_store.path.with_suffix("").with_name(context.run_store.path.stem + ".liveshell"),
        )
        context_size = int(source.GetAttribute("enqueue:contextSize").Get() or 32768) if source else 32768
        gpu_layers = int(source.GetAttribute("enqueue:gpuLayers").Get()) if source and source.GetAttribute("enqueue:gpuLayers").Get() is not None else -1
        parallel_slots = int(source.GetAttribute("enqueue:parallelSlots").Get() or 1) if source else 1
        reasoning = str(source.GetAttribute("enqueue:reasoning").Get() or "off") if source else "off"
        extra_args = list(source.GetAttribute("enqueue:serverArguments").Get() or []) if source else []
        server = context.model_pool.get(model_path, context_size=context_size, gpu_layers=gpu_layers, parallel_slots=parallel_slots, reasoning=reasoning, extra_args=extra_args)

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system + "\n\nYou operate in bounded turns. Choose one available tool action when work is needed. Return a final action only after the task is actually complete. Never describe an unperformed edit as completed.",
            },
            {"role": "user", "content": str(task)},
        ]
        max_turns = int(setting("enqueue:maxTurns", 12))
        max_tokens = int(setting("enqueue:maxTokens", 4096))
        temperature = float(setting("enqueue:temperature", 0.2))
        tool_names = [item["function"]["name"] for item in environment.schemas]
        variants: list[dict[str, Any]] = [{
            "type": "object",
            "properties": {"action": {"const": "final"}, "content": {"type": "string"}},
            "required": ["action", "content"],
            "additionalProperties": False,
        }]
        if tool_names:
            variants.insert(0, {
                "type": "object",
                "properties": {
                    "action": {"const": "tool"},
                    "tool": {"type": "string", "enum": tool_names},
                    "arguments": {"type": "object"},
                },
                "required": ["action", "tool", "arguments"],
                "additionalProperties": False,
            })
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "enqueue_agent_turn",
                "strict": True,
                "schema": {"oneOf": variants},
            },
        }
        tool_description = json.dumps(
            [{"name": item["function"]["name"], "description": item["function"]["description"], "parameters": item["function"]["parameters"]} for item in environment.schemas],
            separators=(",", ":"),
        )
        messages[0]["content"] += "\nAvailable tools: " + tool_description
        for _ in range(max_turns):
            message = server.complete(messages, temperature=temperature, max_tokens=max_tokens, response_format=response_format)
            raw_turn = visible_text(message.get("content") or message.get("reasoning_content"))
            try:
                turn = json.loads(raw_turn)
            except json.JSONDecodeError as exc:
                raise ExecutionError(f"{model_path.name} returned a malformed agent turn: {raw_turn[:500] or repr(message)[:500]}") from exc
            if turn.get("action") == "tool":
                name = str(turn.get("tool") or "")
                arguments = turn.get("arguments")
                if not isinstance(arguments, dict):
                    raise ExecutionError(f"{model_path.name} returned non-object tool arguments")
                try:
                    output = environment.call(name, arguments)
                except Exception as exc:
                    output = f"Tool error: {exc}"
                messages.append({"role": "assistant", "content": raw_turn})
                messages.append({"role": "user", "content": f"Tool result for {name}:\n{output}\nChoose the next tool action or return the final action."})
                continue
            if turn.get("action") != "final":
                raise ExecutionError(f"{model_path.name} returned unknown agent action: {turn!r}")
            answer = visible_text(turn.get("content"))
            if not answer:
                raise ExecutionError(f"{model_path.name} returned no visible answer")
            return NodeResult(outputs={"outputs:result": answer}, result=answer)
        raise ExecutionError(f"Agent exceeded enqueue:maxTurns ({max_turns})")


def register(registry: Registry) -> None:
    source = SourceHandler()
    terminal = TerminalHandler()
    registry.register(NodeSpec("/EnqueueStart", "terminal", terminal, outputs=("outputs:signal",)))
    registry.register(NodeSpec("/EnqueueEnd", "terminal", terminal))
    registry.register(
        NodeSpec(
            "/EnqueueTaskSource",
            "source",
            TaskSourceHandler(),
            outputs=("outputs:value",),
        )
    )
    registry.register(NodeSpec("/EnqueueTextSource", "source", source, outputs=("outputs:value",)))
    registry.register(NodeSpec("/EnqueueFileSource", "source", source, outputs=("outputs:value",)))
    registry.register(NodeSpec("/EnqueueGitSource", "source", source, outputs=("outputs:repository", "outputs:revision")))
    registry.register(NodeSpec("/EnqueueModelSource", "source", source, outputs=("outputs:value",)))
    registry.register(
        NodeSpec(
            "/EnqueueIterator",
            "control",
            IteratorHandler(),
            inputs=(PortContract("inputs:input", True),),
            outputs=("outputs:value", "outputs:done"),
        )
    )
    registry.register(NodeSpec("/EnqueueAgent", "agent", AgentHandler(), inputs=(PortContract("inputs:task", True), PortContract("inputs:model", False), PortContract("inputs:domain", False)), outputs=("outputs:result",)))
    registry.register(NodeSpec("/EnqueueLiveShellCommand", "utility", CommandDefinitionHandler(), outputs=("outputs:command", "outputs:workingDirectory", "outputs:timeoutSeconds")))
    registry.register(NodeSpec("/EnqueueLiveShellSession", "runtime", LiveShellSessionHandler(), inputs=(PortContract("inputs:command", True), PortContract("inputs:workingDirectory", False), PortContract("inputs:timeoutSeconds", False)), outputs=("outputs:stdout", "outputs:stderr", "outputs:exitCode")))
