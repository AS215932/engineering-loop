"""Optional, flag-gated emission of agent-core TraceEvent records (Phase 3).

Best-effort and additive: a no-op unless ``HYRULE_ENGINEERING_AGENT_CORE_TRACE`` is
truthy AND the optional ``agent-core`` package is importable. ``agent-core`` is NOT a
declared dependency of this repo; ``agent_core`` is imported dynamically via ``importlib``
so ``mypy --strict src`` never depends on it and CI without it simply emits nothing.
Any failure is swallowed so emission can never affect the loop.

Records are appended as JSON lines to ``HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH``
(default ``reports/agent-core-trace.jsonl``). Higher fidelity than the knowledge loop:
the engineering-loop state carries token/USD cost in ``backend_results[].cost``.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FLAG_ENV = "HYRULE_ENGINEERING_AGENT_CORE_TRACE"
PATH_ENV = "HYRULE_ENGINEERING_AGENT_CORE_TRACE_PATH"
_DEFAULT_PATH = "reports/agent-core-trace.jsonl"


def enabled() -> bool:
    return os.environ.get(FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _sink_path() -> Path:
    return Path(os.environ.get(PATH_ENV) or _DEFAULT_PATH)


def emit_loop_trace(state: Mapping[str, Any]) -> int:
    """Emit one agent-core TraceEvent per loop-trace item; return count (0 if disabled)."""
    if not enabled():
        return 0
    try:
        adapter = importlib.import_module("agent_core.adapters.engineering_loop")
        run_id = state.get("change_id")
        events = adapter.trace_events_from_loop_trace(dict(state), run_id=run_id)
        path = _sink_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                record: dict[str, Any] = event.model_dump(mode="json")
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                count += 1
        return count
    except Exception:  # best-effort: emission must never break the loop
        return 0
