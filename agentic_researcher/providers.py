"""Provider adapter contract for non-interactive research agents.

Adapters deliberately expose a small contract: a provider receives one UTF-8
prompt on stdin, works in a specified directory, and returns either a JSON
document or prose on stdout.  Environment variables allow deployments to replace
the conservative default CLI command without changing pipeline code.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SUPPORTED_PROVIDERS = ("claude", "codex", "antigravity", "opencode")
PROVIDER_ALIASES = {
    "open-weight": "opencode",
    "open_weight": "opencode",
    "google-antigravity": "antigravity",
}

DEFAULT_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "-p", "--output-format", "json"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "-"],
    # Antigravity installations can override this experimental CLI spelling with
    # AR_PROVIDER_ANTIGRAVITY_COMMAND.
    "antigravity": ["agy", "-p", "{prompt}"],
    # OpenCode can point at a local OpenAI-compatible vLLM endpoint through its
    # normal configuration or AR_PROVIDER_OPENCODE_COMMAND.
    "opencode": ["opencode", "run", "{prompt}"],
}


class ProviderError(RuntimeError):
    """Raised when a provider cannot be invoked or returns unusable output."""


@dataclass(frozen=True)
class ProviderRun:
    provider: str
    status: str
    command: list[str]
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    parsed: Any | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "parsed": self.parsed,
            "error": self.error,
        }


def normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    normalized = PROVIDER_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_PROVIDERS:
        supported = ", ".join(SUPPORTED_PROVIDERS)
        raise ProviderError(f"unsupported provider {provider!r}; choose one of: {supported}")
    return normalized


def provider_command(
    provider: str,
    override: str | Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    provider = normalize_provider(provider)
    env = os.environ if environ is None else environ
    configured: str | Sequence[str] | None = override
    if configured is None:
        configured = env.get(f"AR_PROVIDER_{provider.upper()}_COMMAND")
    if isinstance(configured, str):
        command = shlex.split(configured, posix=os.name != "nt")
    elif configured is not None:
        command = list(configured)
    else:
        command = list(DEFAULT_COMMANDS[provider])
    if not command:
        raise ProviderError(f"empty command configured for provider {provider}")
    return command


def _expand_command(
    command: Sequence[str],
    prompt: str,
    prompt_path: Path,
    workspace: Path,
    output_path: Path | None,
) -> tuple[list[str], bool, bool]:
    replacements = {
        "prompt": prompt,
        "prompt_file": str(prompt_path),
        "workspace": str(workspace),
        "output_file": str(output_path) if output_path else "",
    }
    consumes_prompt = any(
        "{prompt_file}" in part or "{prompt}" in part for part in command
    )
    uses_output_file = any("{output_file}" in part for part in command)
    expanded: list[str] = []
    for part in command:
        for placeholder, replacement in replacements.items():
            part = part.replace("{" + placeholder + "}", replacement)
        expanded.append(part)
    return expanded, consumes_prompt, uses_output_file


def extract_json_document(text: str) -> Any:
    """Extract JSON from plain, fenced, Claude-wrapper, or JSONL output."""

    stripped = text.strip()
    if not stripped:
        raise ProviderError("provider returned empty stdout")

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        for key in ("result", "content", "output", "message"):
            value = parsed.get(key)
            if isinstance(value, str):
                try:
                    return extract_json_document(value)
                except ProviderError:
                    pass
        return parsed
    if parsed is not None:
        return parsed

    # Codex and some wrappers can emit JSONL events. Prefer a final result-like
    # payload, then walk backwards through all events.
    jsonl: list[Any] = []
    for line in stripped.splitlines():
        try:
            jsonl.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    for event in reversed(jsonl):
        if isinstance(event, dict):
            for key in ("result", "content", "output", "message"):
                value = event.get(key)
                if isinstance(value, (dict, list)):
                    return value
                if isinstance(value, str):
                    try:
                        return extract_json_document(value)
                    except ProviderError:
                        continue

    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise ProviderError("provider stdout did not contain a JSON document")


def invoke_provider(
    provider: str,
    prompt: str,
    workspace: Path,
    *,
    command_override: str | Sequence[str] | None = None,
    timeout_seconds: int = 1800,
    output_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderRun:
    """Invoke a provider under the normalized stdin/stdout adapter contract."""

    provider = normalize_provider(provider)
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    prompt_path = workspace / f".agentic-researcher-{provider}-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    command = provider_command(provider, command_override, environ)
    expanded, consumes_prompt, uses_output_file = _expand_command(
        command, prompt, prompt_path, workspace, output_path
    )
    recorded_command = [
        part.replace(prompt, "<prompt>") if prompt in part else part for part in expanded
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            expanded,
            cwd=workspace,
            input=None if consumes_prompt else prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=dict(os.environ if environ is None else environ),
        )
    except subprocess.TimeoutExpired as exc:
        return ProviderRun(
            provider=provider,
            status="timeout",
            command=recorded_command,
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            parsed=None,
            error=f"provider exceeded {timeout_seconds}s timeout",
        )
    except OSError as exc:
        return ProviderRun(
            provider=provider,
            status="failed",
            command=recorded_command,
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            stdout="",
            stderr="",
            parsed=None,
            error=str(exc),
        )

    parsed: Any | None = None
    parse_error: str | None = None
    parse_source = completed.stdout
    if (
        not parse_source.strip()
        and uses_output_file
        and output_path
        and output_path.is_file()
    ):
        parse_source = output_path.read_text(encoding="utf-8")
    if parse_source.strip():
        try:
            parsed = extract_json_document(parse_source)
        except ProviderError as exc:
            parse_error = str(exc)
    status = "success" if completed.returncode == 0 else "failed"
    return ProviderRun(
        provider=provider,
        status=status,
        command=recorded_command,
        exit_code=completed.returncode,
        duration_seconds=time.monotonic() - started,
        stdout=completed.stdout,
        stderr=completed.stderr,
        parsed=parsed,
        error=parse_error if completed.returncode == 0 else parse_error or completed.stderr.strip(),
    )
