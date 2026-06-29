from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

pytest.importorskip("agent_core")

from hyrule_engineering_loop import agent_core_trace


@contextmanager
def _collector() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"stored"}')

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/trace", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _state() -> dict[str, object]:
    return {
        "change_id": "chg-test-1",
        "pr_results": [
            {
                "repo": "network-operations",
                "commit": "abc123",
                "github_pr": {"created": True, "url": "https://github.com/AS215932/network-operations/pull/318"},
            }
        ],
        "workflow_run_id": "28392093138",
        "trace_events": [
            {
                "node": "hydrate_context",
                "timestamp": "2026-06-29T00:00:00Z",
                "output": {"status": "passed"},
            }
        ],
        "llm_outputs": [
            {
                "role": "security_auditor",
                "approved": False,
                "model_selection": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            }
        ],
        "gate_results": [{"command": ["ruff"], "status": "failed", "returncode": 2}],
        "backend_results": [
            {
                "backend": "mock",
                "status": "completed",
                "cost": {"input_tokens": 100, "output_tokens": 20, "usd": 0.0},
            }
        ],
    }


def test_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE", raising=False)
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH", str(tmp_path / "t.jsonl"))
    assert agent_core_trace.emit_loop_trace(_state()) == 0
    assert not (tmp_path / "t.jsonl").exists()


def test_emits_when_enabled(monkeypatch, tmp_path):
    sink = tmp_path / "t.jsonl"
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE", "1")
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH", str(sink))
    count = agent_core_trace.emit_loop_trace(_state())
    assert count == 4
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    kinds = {json.loads(line)["event_type"] for line in lines}
    assert kinds == {"model_call", "tool_call", "backend_execution", "loop_node"}
    backend = next(
        json.loads(line) for line in lines if json.loads(line)["event_type"] == "backend_execution"
    )
    assert backend["cost"]["input_tokens"] == 100
    assert backend["run_id"] == "chg-test-1"
    assert backend["change_id"] == "chg-test-1"
    assert backend["pr_number"] == 318
    assert backend["commit_sha"] == "abc123"
    assert backend["workflow_run_id"] == "28392093138"
    loop_node = next(json.loads(line) for line in lines if json.loads(line)["event_type"] == "loop_node")
    assert loop_node["parent_event_id"]


def test_emit_published_trace_reemits_pr_correlation(monkeypatch, tmp_path):
    sink = tmp_path / "published.jsonl"
    state = _state()
    state.pop("pr_results")
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE", "1")
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH", str(sink))

    count = agent_core_trace.emit_published_trace(
        state,
        [
            {
                "repo": "network-operations",
                "commit": "publishedabc",
                "github_pr": {
                    "created": True,
                    "url": "https://github.com/AS215932/network-operations/pull/319",
                },
            }
        ],
    )

    assert count == 4
    records = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert {record["pr_number"] for record in records} == {319}
    assert {record["commit_sha"] for record in records} == {"publishedabc"}


def test_emits_to_collector_and_file_when_collector_url_is_set(monkeypatch, tmp_path):
    sink = tmp_path / "t.jsonl"
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE", "1")
    monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH", str(sink))
    with _collector() as (url, received):
        monkeypatch.setenv("HYRULE_ENGINEERING_AGENT_CORE_TRACE_COLLECTOR_URL", url)
        count = agent_core_trace.emit_loop_trace(_state())

    assert count == 4
    assert len(sink.read_text(encoding="utf-8").strip().splitlines()) == 4
    assert [event["event_type"] for event in received] == [
        "model_call",
        "tool_call",
        "backend_execution",
        "loop_node",
    ]
    assert received[-1]["change_id"] == "chg-test-1"
