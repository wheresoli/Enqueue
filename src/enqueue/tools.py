from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from liveshell import LiveShellClient

from .errors import ExecutionError


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file in the task domain.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search text files in the task domain using a regular expression.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a UTF-8 text file in the writable task domain.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "replace_in_file", "description": "Replace one exact text occurrence in a file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "run_shell", "description": "Run a shell command in the task domain through LiveShell.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "number"}}, "required": ["command"]}}},
]


class ToolEnvironment:
    def __init__(
        self,
        root: Path,
        *,
        allowed_tools: set[str] | None = None,
        readable_roots: list[Path] | None = None,
        writable_roots: list[Path] | None = None,
        state_dir: Path | None = None,
    ):
        self.root = root.resolve()
        self.allowed_tools = ({item["function"]["name"] for item in TOOL_SCHEMAS} if allowed_tools is None else allowed_tools)
        self.readable_roots = [item.resolve() for item in ([self.root] if readable_roots is None else readable_roots)]
        self.writable_roots = [item.resolve() for item in ([self.root] if writable_roots is None else writable_roots)]
        self.state_dir = state_dir or (self.root / ".enqueue-liveshell")

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [item for item in TOOL_SCHEMAS if item["function"]["name"] in self.allowed_tools]

    def _path(self, value: str, *, write: bool = False) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        roots = self.writable_roots if write else self.readable_roots
        if not any(_is_within(candidate, root) for root in roots):
            raise ExecutionError(f"Path is outside the {'writable' if write else 'readable'} task domain: {candidate}")
        return candidate

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self.allowed_tools:
            raise ExecutionError(f"Tool is not allowed: {name}")
        if name == "read_file":
            path = self._path(str(arguments["path"]))
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            offset = max(0, int(arguments.get("offset", 0)))
            limit = min(2000, max(1, int(arguments.get("limit", 400))))
            return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[offset:offset + limit], offset))
        if name == "search_files":
            executable = shutil_which("rg")
            if not executable:
                raise ExecutionError("search_files requires ripgrep (rg)")
            search_roots = [str(root) for root in self.readable_roots] or [str(self.root)]
            command = [
                executable,
                "-n",
                "--hidden",
                "-g",
                "!.git/**",
                "-g",
                str(arguments.get("glob") or "*"),
                str(arguments["pattern"]),
                *search_roots,
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            return (result.stdout or result.stderr or "No matches")[-50000:]
        if name == "write_file":
            path = self._path(str(arguments["path"]), write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(arguments["content"]), encoding="utf-8")
            return f"Wrote {path}"
        if name == "replace_in_file":
            path = self._path(str(arguments["path"]), write=True)
            text = path.read_text(encoding="utf-8")
            old = str(arguments["old"])
            count = text.count(old)
            if count != 1:
                raise ExecutionError(f"Expected exactly one occurrence in {path}; found {count}")
            path.write_text(text.replace(old, str(arguments["new"]), 1), encoding="utf-8")
            return f"Updated {path}"
        if name == "run_shell":
            self.state_dir.mkdir(parents=True, exist_ok=True)
            kind = "cmd" if os.name == "nt" else "bash"
            with LiveShellClient.stdio(self.state_dir) as client:
                session = client.create_session(kind, cwd=str(self.root))
                try:
                    result = session.run(str(arguments["command"]), timeout_seconds=float(arguments.get("timeout_seconds", 300)))
                    return json.dumps({"status": result.command.status, "exit_code": result.command.exit_code, "stdout": result.stdout[-30000:], "stderr": result.stderr[-10000:]})
                finally:
                    session.close()
        raise ExecutionError(f"Unknown tool: {name}")


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False
