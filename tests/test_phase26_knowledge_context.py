from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from hyrule_engineering_loop.feature import build_feature_state
from hyrule_engineering_loop.knowledge_context import (
    KnowledgeContextConfig,
    _mcp_read_write_streams,
    _mcp_tool_result_to_dict,
    _read_mcp_context_pack_async,
    load_knowledge_context,
)


FIXTURE_PACK = {
    "id": "ctx_test",
    "role": "engineering_loop",
    "knowledge_snapshot": "fixture",
    "retrieval_version": "retrieval_v1",
    "policy_version": "knowledge_policy_v1",
    "policy_decision": {"result": "allow"},
    "sections": [
        {"name": "target_repo_source_truth", "body": "Use source truth first.", "refs": ["generated/services/hyrule-cloud"]},
        {"name": "forbidden_actions", "body": "No production mutation without humans.", "refs": []},
    ],
    "included_refs": [
        {
            "concept_id": "generated/services/hyrule-cloud",
            "authority_tier": "A0",
            "title": "Hyrule Cloud",
            "source_refs": [{"repo": "AS215932/hyrule-cloud", "path": "README.md"}],
            "retrieval_scores": {"exact": 1.0, "graph": None, "fts": None, "vector": None},
        }
    ],
}


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Hyrule Cloud\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_knowledge_context_disabled_by_default() -> None:
    result = load_knowledge_context("task", config=KnowledgeContextConfig(enabled=False))
    assert result["status"] == "disabled"
    assert result["pack"] is None


def test_knowledge_context_fixture_is_rendered(tmp_path: Path) -> None:
    fixture = tmp_path / "pack.json"
    fixture.write_text(json.dumps(FIXTURE_PACK), encoding="utf-8")
    result = load_knowledge_context(
        "Engineer a Hyrule Cloud change",
        config=KnowledgeContextConfig(enabled=True, fixture_path=fixture),
    )
    assert result["status"] == "ok"
    assert result["policy_result"] == "allow"
    assert "AS215932 Knowledge Context Pack" in result["summary"]
    assert "generated/services/hyrule-cloud" in result["summary"]


def test_mcp_tool_result_text_content_is_parsed() -> None:
    result = SimpleNamespace(content=[SimpleNamespace(text=json.dumps(FIXTURE_PACK))])

    assert _mcp_tool_result_to_dict(result)["id"] == "ctx_test"


def test_mcp_read_write_streams_accepts_sse_and_streamable_shapes() -> None:
    assert _mcp_read_write_streams(("read", "write")) == ("read", "write")
    assert _mcp_read_write_streams(("read", "write", "session")) == ("read", "write")


def test_mcp_context_pack_request_forwards_authority_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeClientSession:
        def __init__(self, read_stream: object, write_stream: object) -> None:
            self.read_stream = read_stream
            self.write_stream = write_stream

        async def __aenter__(self) -> FakeClientSession:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, object]) -> SimpleNamespace:
            calls.append((name, arguments))
            return SimpleNamespace(structuredContent=FIXTURE_PACK)

    class FakeStreamContext:
        async def __aenter__(self) -> tuple[str, str]:
            return "read", "write"

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_streamablehttp_client(url: str, *, timeout: int, sse_read_timeout: int) -> FakeStreamContext:
        assert url == "http://knowledge.local/mcp"
        assert timeout == 20
        assert sse_read_timeout == 20
        return FakeStreamContext()

    def fake_import_module(name: str) -> SimpleNamespace:
        if name == "mcp":
            return SimpleNamespace(ClientSession=FakeClientSession)
        if name == "mcp.client.streamable_http":
            return SimpleNamespace(streamablehttp_client=fake_streamablehttp_client)
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("hyrule_engineering_loop.knowledge_context.import_module", fake_import_module)

    pack = anyio.run(
        _read_mcp_context_pack_async,
        "Engineer a Hyrule Cloud change",
        KnowledgeContextConfig(
            enabled=True,
            role="engineering_loop_reliability_governor",
            mcp_url="http://knowledge.local/mcp",
            authority_min="A1",
        ),
    )

    assert pack == FIXTURE_PACK
    assert calls == [
        (
            "knowledge_context_pack",
            {
                "task": "Engineer a Hyrule Cloud change",
                "role": "engineering_loop",
                "risk_level": "low",
                "budget_tokens": 6000,
                "authority_min": "A1",
            },
        )
    ]


def test_cli_context_pack_request_normalizes_governor_role(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(FIXTURE_PACK), stderr="")

    monkeypatch.setattr("hyrule_engineering_loop.knowledge_context.subprocess.run", fake_run)

    result = load_knowledge_context(
        "Engineer a Hyrule Cloud change",
        config=KnowledgeContextConfig(
            enabled=True,
            repo_path=tmp_path,
            role="engineering_loop_reliability_governor",
        ),
    )

    assert result["status"] == "ok"
    assert calls[0][calls[0].index("--role") + 1] == "engineering_loop"


def test_feature_state_includes_optional_knowledge_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace / "hyrule-cloud")
    request = tmp_path / "request.md"
    request.write_text("Engineer a Hyrule Cloud change using knowledge context.\n", encoding="utf-8")
    fixture = tmp_path / "pack.json"
    fixture.write_text(json.dumps(FIXTURE_PACK), encoding="utf-8")

    state = build_feature_state(
        change_id="KNOWLEDGE_CONTEXT_TEST",
        change_class="app_feature",
        workspace_root=workspace,
        output_root=tmp_path / "out",
        repo_name="hyrule-cloud",
        request_path=request,
        allowed_paths=["docs"],
        knowledge_context=KnowledgeContextConfig(enabled=True, fixture_path=fixture),
    )

    assert state["knowledge_context_status"] == "ok"
    assert "knowledge_context_summary" in state
    assert state["knowledge_context_pack"]["included_refs"][0]["retrieval_scores"]["vector"] is None
