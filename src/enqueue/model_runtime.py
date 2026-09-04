from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import ModelRuntimeError


_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def visible_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _THINK_BLOCK.sub("", text)
    if "<think" in text.lower() and "</think" not in text.lower():
        text = text[: text.lower().find("<think")]
    return text.strip()


def find_llama_server(explicit: str | None = None) -> Path:
    candidates = [
        explicit,
        os.environ.get("ENQUEUE_LLAMA_SERVER_BIN"),
        os.environ.get("CONCURRO_LLAMA_SERVER_BIN"),
        shutil.which("llama-server"),
        str(Path.home() / "Projects" / "AI" / "llama.cpp" / ("llama-server.exe" if os.name == "nt" else "llama-server")),
        str(Path.home() / ".concurro" / "runtime" / "llama" / ("llama-server.exe" if os.name == "nt" else "llama-server")),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise ModelRuntimeError("llama-server was not found; set ENQUEUE_LLAMA_SERVER_BIN")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LlamaServer:
    def __init__(
        self,
        model: Path,
        *,
        binary: str | None = None,
        context_size: int = 32768,
        gpu_layers: int = -1,
        parallel_slots: int = 1,
        reasoning: str = "off",
        extra_args: list[str] | None = None,
        log_directory: Path | None = None,
    ) -> None:
        if not model.is_file():
            raise ModelRuntimeError(f"GGUF model does not exist: {model}")
        self.model = model.resolve()
        self.binary = find_llama_server(binary)
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        directory = log_directory or Path.cwd()
        directory.mkdir(parents=True, exist_ok=True)
        self.log_path = directory / f"llama-{self.port}.log"
        self._log = self.log_path.open("w", encoding="utf-8")
        command = [
            str(self.binary), "--model", str(self.model), "--host", "127.0.0.1",
            "--port", str(self.port), "--ctx-size", str(context_size),
            "--n-gpu-layers", str(gpu_layers), "--parallel", str(max(1, parallel_slots)),
            "--reasoning", reasoning,
        ]
        command.extend(extra_args or [])
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(command, stdout=self._log, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            self._wait_ready()
        except Exception:
            self.close()
            raise

    def _wait_ready(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._log.flush()
                tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise ModelRuntimeError(f"llama-server exited during startup ({self.process.returncode}):\n{tail}")
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except Exception as exc:  # server is expected to refuse while loading
                last_error = str(exc)
            time.sleep(0.25)
        raise ModelRuntimeError(f"llama-server did not become ready: {last_error}; log: {self.log_path}")

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": str(self.model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ModelRuntimeError(f"llama-server completion failed ({exc.code}): {body}") from exc
        except Exception as exc:
            raise ModelRuntimeError(f"llama-server completion failed: {exc}") from exc
        try:
            return document["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelRuntimeError(f"Malformed completion response: {document!r}") from exc

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._log.close()


class ModelPool:
    def __init__(self, *, binary: str | None = None, log_directory: Path | None = None):
        self.binary = binary
        self.log_directory = log_directory
        self._servers: dict[tuple[str, int, int, int, str, tuple[str, ...]], LlamaServer] = {}
        self._lock = threading.Lock()

    def get(
        self,
        model: Path,
        *,
        context_size: int = 32768,
        gpu_layers: int = -1,
        parallel_slots: int = 1,
        reasoning: str = "off",
        extra_args: list[str] | None = None,
    ) -> LlamaServer:
        key = (str(model.resolve()), context_size, gpu_layers, parallel_slots, reasoning, tuple(extra_args or []))
        with self._lock:
            if key not in self._servers:
                self._servers[key] = LlamaServer(
                    model,
                    binary=self.binary,
                    context_size=context_size,
                    gpu_layers=gpu_layers,
                    parallel_slots=parallel_slots,
                    reasoning=reasoning,
                    extra_args=extra_args,
                    log_directory=self.log_directory,
                )
            return self._servers[key]

    def close(self) -> None:
        for server in self._servers.values():
            server.close()
        self._servers.clear()

    def __enter__(self) -> "ModelPool":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
