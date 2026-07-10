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

import hashlib
import importlib
import json
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
        events.append(_loop_decision_event(state, run_id=run_id))
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


def emit_insight_decision_envelopes(
    insights: list[dict[str, Any]],
    *,
    input_event: Mapping[str, Any] | None = None,
) -> int:
    """Emit one LoopDecisionEnvelope TraceEvent per insight record.

    Mirrors the NOC/SOC modules: the payload carries both the envelope and the
    full validated ``InsightDecisionRecord`` (the envelope alone drops
    sampling_class/utility/cost/support_facts, which the knowledge repo's
    IDQ/CGS evaluation needs). Best-effort like everything else here.
    """
    if not enabled() or not insights:
        return 0
    try:
        sink = _sink_from_env()
        count = 0
        for insight in insights:
            event = _insight_decision_event(insight, input_event=dict(input_event or {}))
            if sink.emit(event):
                count += 1
        return count
    except Exception:
        return 0


def _insight_decision_event(insight: Mapping[str, Any], *, input_event: dict[str, Any]) -> Any:
    contracts = importlib.import_module("agent_core.contracts")
    TraceEvent = getattr(contracts, "TraceEvent")
    LoopDecisionEnvelope = getattr(contracts, "LoopDecisionEnvelope")
    InsightDecisionRecord = getattr(contracts, "InsightDecisionRecord")

    validated = InsightDecisionRecord.model_validate(dict(insight))
    envelope = LoopDecisionEnvelope(
        envelope_id=(
            f"ldec_eng_{_stable_hash([validated.insight_id, validated.fingerprint, validated.action_selected])}"
        ),
        loop="engineering",
        environment="production",
        graph_id="engineering-loop",
        node_id="insight_stream",
        agent_role="engineering_loop",
        run_id=_string_or_none(input_event.get("run_id")) or validated.run_id,
        trace_id=validated.trace_id,
        input_event={
            **input_event,
            "candidate_type": validated.candidate_type,
            "candidate_source": validated.candidate_source,
        },
        retrieved_context=validated.evidence_refs,
        decision=validated.action_selected,
        evidence_refs=validated.evidence_refs,
        proposed_action={
            "candidate_type": validated.candidate_type,
            "candidate_source": validated.candidate_source,
            "why_now": validated.why_now,
            "support_fact_count": len(validated.support_facts),
        },
        human_outcome=validated.human_feedback,
        governance=validated.governance,
        insight_id=validated.insight_id,
        case_id=validated.case_id,
        fingerprint=validated.fingerprint,
        policy_version=validated.policy_version,
    )
    return TraceEvent(
        event_type="loop_decision_envelope",
        graph_id="engineering-loop",
        node_id="loop_decision_envelope",
        agent_role="engineering_loop",
        environment="production",
        run_id=envelope.run_id,
        trace_id=envelope.trace_id,
        summary=f"Engineering loop decision envelope for {validated.insight_id}",
        payload={
            "loop_decision_envelope": envelope.model_dump(mode="json"),
            "insight_decision_record": validated.model_dump(mode="json"),
        },
    )


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


def _loop_decision_event(state: Mapping[str, Any], *, run_id: Any) -> Any:
    contracts = importlib.import_module("agent_core.contracts")
    TraceEvent = getattr(contracts, "TraceEvent")
    LoopDecisionEnvelope = getattr(contracts, "LoopDecisionEnvelope")
    GovernanceControls = getattr(contracts, "GovernanceControls")

    payload = _trace_payload(state)
    change_id = _string_or_none(payload.get("change_id") or run_id)
    repository = _string_or_none(payload.get("repository") or _first_pr_result(payload).get("repo"))
    pr_url = _string_or_none(payload.get("pr_url") or _first_github_pr(payload).get("url"))
    commit_sha = _string_or_none(payload.get("commit_sha") or _first_pr_result(payload).get("commit"))
    workflow_run_id = _string_or_none(payload.get("workflow_run_id"))
    decision = _decision_from_state(payload)
    evidence_refs = _source_refs_from_state(payload)
    envelope = LoopDecisionEnvelope(
        envelope_id=f"ldec_eng_{_stable_hash([change_id, repository, decision, pr_url, commit_sha])}",
        loop="engineering",
        environment="production",
        graph_id="engineering-loop",
        node_id="loop_runtime",
        agent_role="engineering_loop",
        run_id=change_id,
        trace_id=_string_or_none(payload.get("trace_id")) or change_id,
        input_event={
            "change_id": change_id,
            "repository": repository,
            "workflow_run_id": workflow_run_id,
            "approval_decision": payload.get("approval_decision"),
            "pr_status": payload.get("pr_status"),
        },
        retrieved_context=_context_refs(payload),
        decision=decision,
        evidence_refs=evidence_refs,
        proposed_action={
            "pr_status": payload.get("pr_status"),
            "pr_url": pr_url,
            "commit_sha": commit_sha,
            "gate_result_count": len(_listish(payload.get("gate_results"))),
            "backend_result_count": len(_listish(payload.get("backend_results"))),
        },
        human_outcome={
            key: value
            for key, value in {"approval_decision": _string_or_none(payload.get("approval_decision"))}.items()
            if value
        },
        governance=GovernanceControls(
            sensitivity_class="internal",
            approval_tier="operator",
            risk_class="medium",
            learning_allowed=True,
            never_learn=False,
            policy_ids=["engineering-loop-decision.v1"],
            rationale="Engineering Loop runtime decisions are emitted as sanitized envelopes for replay.",
        ),
        case_id=_string_or_none(payload.get("case_id")),
        fingerprint=_string_or_none(payload.get("fingerprint") or change_id or repository) or "",
        policy_version="engineering-loop-decision.v1",
    )
    return TraceEvent(
        event_type="loop_decision_envelope",
        graph_id="engineering-loop",
        node_id="loop_decision_envelope",
        agent_role="engineering_loop",
        environment="production",
        run_id=change_id,
        trace_id=envelope.trace_id,
        change_id=change_id,
        repository=repository,
        pr_number=_pr_number(pr_url),
        commit_sha=commit_sha,
        workflow_run_id=workflow_run_id,
        summary=f"Engineering Loop decision envelope for {change_id or repository or 'run'}",
        payload={"loop_decision_envelope": envelope.model_dump(mode="json")},
    )


def _decision_from_state(state: Mapping[str, Any]) -> str:
    if _listish(state.get("pr_results")) or state.get("pr_status") == "pushed":
        return "draft"
    approval = str(state.get("approval_decision") or "").lower()
    if approval in {"pending", "review", "needs_review"}:
        return "question"
    if approval in {"rejected", "blocked"}:
        return "stay_silent"
    if _listish(state.get("gate_results")) or _listish(state.get("backend_results")):
        return "draft"
    return "stay_silent"


def _source_refs_from_state(state: Mapping[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    repository = _string_or_none(state.get("repository") or _first_pr_result(state).get("repo"))
    if repository:
        refs.append({"kind": "repo", "ref": repository})
    pr_url = _string_or_none(state.get("pr_url") or _first_github_pr(state).get("url"))
    if pr_url:
        refs.append({"kind": "github_pr", "ref": pr_url})
    workflow_run_id = _string_or_none(state.get("workflow_run_id"))
    if workflow_run_id:
        refs.append({"kind": "github_actions", "ref": workflow_run_id})
    refs.extend(_context_refs(state))
    return refs[:20]


def _context_refs(state: Mapping[str, Any]) -> list[dict[str, str]]:
    context = state.get("knowledge_context") or state.get("context_pack")
    if not isinstance(context, Mapping):
        return []
    raw_refs = context.get("included_refs") or context.get("source_refs") or []
    refs: list[dict[str, str]] = []
    for item in _listish(raw_refs)[:12]:
        if isinstance(item, Mapping) and item.get("ref"):
            refs.append({"kind": _safe_text(item.get("kind") or "knowledge", limit=64), "ref": _safe_text(item["ref"], limit=180)})
    return refs


def _first_pr_result(state: Mapping[str, Any]) -> Mapping[str, Any]:
    pr_results = _listish(state.get("pr_results"))
    first = pr_results[0] if pr_results else {}
    return first if isinstance(first, Mapping) else {}


def _first_github_pr(state: Mapping[str, Any]) -> Mapping[str, Any]:
    github_pr = _first_pr_result(state).get("github_pr")
    return github_pr if isinstance(github_pr, Mapping) else {}


def _pr_number(url: str | None) -> int | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _listish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _safe_text(value: Any, *, limit: int = 120) -> str:
    text = _string_or_none(value) or ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stable_hash(parts: list[Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
