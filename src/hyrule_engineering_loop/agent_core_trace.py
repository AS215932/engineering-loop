"""Optional, flag-gated emission of agent-core TraceEvent records.

Best-effort and additive: a no-op unless ``HYRULE_ENGINEERING_AGENT_CORE_TRACE`` is
truthy and ``agent-core`` is importable. Delivery uses ``agent_core.tracing.sink_from_env``
so operators can configure a JSONL path, an HTTP collector URL, or both.

The historical JSONL fallback is preserved: when tracing is enabled without an explicit
``*_PATH`` or ``*_COLLECTOR_URL``, events are appended to
``reports/agent-core-trace.jsonl``. Any failure is swallowed so emission can never affect
the loop, and the returned count reflects events delivered to at least one sink.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any

FLAG_ENV = "HYRULE_ENGINEERING_AGENT_CORE_TRACE"
PATH_ENV = "HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH"
COLLECTOR_URL_ENV = f"{FLAG_ENV}_COLLECTOR_URL"
_DEFAULT_PATH = "reports/agent-core-trace.jsonl"
_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUTHY


def _sink_from_env() -> Any:
    sink_mod = importlib.import_module("agent_core.tracing.sink")
    path_configured = bool(os.environ.get(PATH_ENV, "").strip())
    collector_configured = bool(os.environ.get(COLLECTOR_URL_ENV, "").strip())
    if path_configured or collector_configured:
        return sink_mod.sink_from_env(FLAG_ENV)

    original_path = os.environ.get(PATH_ENV)
    os.environ[PATH_ENV] = _DEFAULT_PATH
    try:
        return sink_mod.sink_from_env(FLAG_ENV)
    finally:
        if original_path is None:
            os.environ.pop(PATH_ENV, None)
        else:
            os.environ[PATH_ENV] = original_path


def emit_loop_trace(state: Mapping[str, Any]) -> int:
    """Emit one agent-core TraceEvent per loop-trace item; return count (0 if disabled)."""
    if not enabled():
        return 0
    try:
        adapter = importlib.import_module("agent_core.adapters.engineering_loop")
        sink = _sink_from_env()
        run_id = state.get("change_id")
        events = adapter.trace_events_from_loop_trace(_trace_payload(state), run_id=run_id)
        count = 0
        for event in events:
            if sink.emit(event):
                count += 1
        return count
    except Exception:  # best-effort: emission must never break the loop
        return 0


def emit_published_trace(state: Mapping[str, Any], pr_results: list[dict[str, Any]]) -> int:
    """Re-emit trace after PR publication adds GitHub URL/commit metadata."""
    if not pr_results:
        return 0
    return emit_loop_trace({**dict(state), "pr_status": "pushed", "pr_results": pr_results})


def _trace_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    pr_results = state.get("pr_results")
    if not isinstance(pr_results, list) or not pr_results:
        return payload
    first = pr_results[0]
    if not isinstance(first, Mapping):
        return payload
    github_pr = first.get("github_pr")
    if isinstance(github_pr, Mapping) and github_pr.get("url") and not payload.get("pr_url"):
        payload["pr_url"] = github_pr.get("url")
    if first.get("commit") and not payload.get("commit_sha"):
        payload["commit_sha"] = first.get("commit")
    if first.get("repo") and not payload.get("repository"):
        payload["repository"] = first.get("repo")
    return payload
