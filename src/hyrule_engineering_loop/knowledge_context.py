"""Optional AS215932 knowledge context-pack consumer.

This integration is intentionally read-only and default-off. Engineering Loop can
load a context pack from a fixture or ask a local checkout of AS215932/knowledge
for one via its CLI. The knowledge service remains transport-independent and no
live telemetry, LLM, or production tools are invoked here.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import anyio


class KnowledgeContextError(RuntimeError):
    """Raised when optional knowledge context loading fails."""


@dataclass(frozen=True)
class KnowledgeContextConfig:
    enabled: bool = False
    repo_path: Path = Path("../knowledge")
    role: str = "engineering_loop"
    risk_level: str = "low"
    budget_tokens: int = 6000
    authority_min: str = "A4"
    timeout_seconds: int = 20
    fixture_path: Path | None = None
    mcp_url: str | None = None
    mcp_transport: str = "streamable-http"

    @classmethod
    def from_env(cls) -> KnowledgeContextConfig:
        return cls(
            enabled=_truthy(os.environ.get("HYRULE_KNOWLEDGE_CONTEXT")),
            repo_path=Path(os.environ.get("HYRULE_KNOWLEDGE_REPO", "../knowledge")),
            role=os.environ.get("HYRULE_KNOWLEDGE_CONTEXT_ROLE", "engineering_loop"),
            risk_level=os.environ.get("HYRULE_KNOWLEDGE_CONTEXT_RISK", "low"),
            budget_tokens=int(os.environ.get("HYRULE_KNOWLEDGE_CONTEXT_BUDGET", "6000")),
            authority_min=os.environ.get("HYRULE_KNOWLEDGE_CONTEXT_AUTHORITY_MIN", "A4"),
            timeout_seconds=int(os.environ.get("HYRULE_KNOWLEDGE_CONTEXT_TIMEOUT", "20")),
            fixture_path=Path(os.environ["HYRULE_KNOWLEDGE_CONTEXT_FIXTURE"]) if os.environ.get("HYRULE_KNOWLEDGE_CONTEXT_FIXTURE") else None,
            mcp_url=os.environ.get("HYRULE_KNOWLEDGE_MCP_URL") or None,
            mcp_transport=os.environ.get("HYRULE_KNOWLEDGE_MCP_TRANSPORT", "streamable-http"),
        )


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def load_knowledge_context(task: str, *, config: KnowledgeContextConfig | None = None) -> dict[str, Any]:
    active = config or KnowledgeContextConfig.from_env()
    if not active.enabled:
        return {"status": "disabled", "enabled": False, "pack": None, "summary": ""}
    try:
        pack = _load_pack(task, active)
    except KnowledgeContextError as exc:
        return {"status": "error", "enabled": True, "pack": None, "summary": "", "error": str(exc)}
    return {
        "status": "ok",
        "enabled": True,
        "pack": pack,
        "summary": render_context_pack_markdown(pack),
        "policy_result": (pack.get("policy_decision") or {}).get("result"),
        "included_ref_count": len(pack.get("included_refs") or []),
    }


def _load_pack(task: str, config: KnowledgeContextConfig) -> dict[str, Any]:
    if config.fixture_path is not None:
        return _read_fixture(config.fixture_path)
    if config.mcp_url:
        return _read_mcp_context_pack(task, config)
    repo_path = config.repo_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise KnowledgeContextError(f"knowledge repo not found: {repo_path}")
    command = [
        "uv",
        "run",
        "hyrule-knowledge",
        "context-pack",
        "--task",
        task,
        "--role",
        config.role,
        "--risk-level",
        config.risk_level,
        "--budget-tokens",
        str(config.budget_tokens),
        "--authority-min",
        config.authority_min,
    ]
    completed = subprocess.run(
        command,
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=config.timeout_seconds,
        env={**os.environ, "OPENROUTER_API_KEY": ""},
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()[:800]
        raise KnowledgeContextError(f"knowledge context-pack command failed: {stderr or completed.returncode}")
    try:
        loaded = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise KnowledgeContextError("knowledge context-pack output was not JSON") from exc
    if not isinstance(loaded, dict):
        raise KnowledgeContextError("knowledge context-pack output was not an object")
    return loaded


def _read_mcp_context_pack(task: str, config: KnowledgeContextConfig) -> dict[str, Any]:
    try:
        return anyio.run(_read_mcp_context_pack_async, task, config)
    except KnowledgeContextError:
        raise
    except Exception as exc:
        raise KnowledgeContextError(f"knowledge MCP context-pack request failed: {exc}") from exc


async def _read_mcp_context_pack_async(task: str, config: KnowledgeContextConfig) -> dict[str, Any]:
    try:
        client_session = getattr(import_module("mcp"), "ClientSession")
        if config.mcp_transport == "sse":
            client_factory = getattr(import_module("mcp.client.sse"), "sse_client")
        elif config.mcp_transport in {"streamable-http", "http"}:
            client_factory = getattr(import_module("mcp.client.streamable_http"), "streamablehttp_client")
        else:
            raise KnowledgeContextError(f"unsupported knowledge MCP transport: {config.mcp_transport}")
    except ModuleNotFoundError as exc:
        raise KnowledgeContextError("optional dependency `mcp` is required for HYRULE_KNOWLEDGE_MCP_URL") from exc

    assert config.mcp_url is not None
    async with client_factory(config.mcp_url, timeout=config.timeout_seconds, sse_read_timeout=config.timeout_seconds) as streams:
        read_stream, write_stream = _mcp_read_write_streams(streams)
        async with client_session(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "knowledge_context_pack",
                {
                    "task": task,
                    "role": config.role,
                    "risk_level": config.risk_level,
                    "budget_tokens": config.budget_tokens,
                },
            )
    return _mcp_tool_result_to_dict(result)


def _mcp_read_write_streams(streams: Any) -> tuple[Any, Any]:
    if isinstance(streams, tuple) and len(streams) >= 2:
        return streams[0], streams[1]
    raise KnowledgeContextError("knowledge MCP client did not return read/write streams")


def _mcp_tool_result_to_dict(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    return loaded
    raise KnowledgeContextError("knowledge MCP context-pack result was not a JSON object")


def _read_fixture(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise KnowledgeContextError(f"knowledge context fixture not found: {resolved}")
    loaded = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise KnowledgeContextError(f"knowledge context fixture must be a JSON object: {resolved}")
    return loaded


def render_context_pack_markdown(pack: dict[str, Any], *, max_chars: int = 18000) -> str:
    lines = [
        "# AS215932 Knowledge Context Pack",
        "",
        f"* Pack: `{pack.get('id', 'unknown')}`",
        f"* Role: `{pack.get('role', 'unknown')}`",
        f"* Snapshot: `{pack.get('knowledge_snapshot', 'unknown')}`",
        f"* Retrieval: `{pack.get('retrieval_version', 'unknown')}`",
        f"* Policy: `{pack.get('policy_version', 'unknown')}` / `{(pack.get('policy_decision') or {}).get('result', 'unknown')}`",
        "",
    ]
    for section in pack.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        lines.append(f"## {section.get('name', 'section')}")
        lines.append(str(section.get("body", "")))
        refs = section.get("refs")
        if isinstance(refs, list) and refs:
            lines.append("Refs: " + ", ".join(f"`{ref}`" for ref in refs[:12]))
        lines.append("")
    refs = pack.get("included_refs")
    if isinstance(refs, list):
        lines.append("## Included references")
        for ref in refs[:20]:
            if isinstance(ref, dict):
                lines.append(f"- `{ref.get('concept_id')}` ({ref.get('authority_tier')}) {ref.get('title', '')}")
    rendered = "\n".join(lines).strip() + "\n"
    if len(rendered) > max_chars:
        return rendered[: max_chars - 32].rstrip() + "\n[truncated]\n"
    return rendered
