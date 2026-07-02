"""Private insight ledger for Engineering Loop proactivity decisions.

The public ``agent-core`` contract owns the canonical shape. This module emits a
compatible JSON object without importing a not-yet-released dependency version.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

InsightAction = Literal["notify", "question", "draft", "stay_silent"]
SamplingClass = Literal["surfaced", "withheld_logged", "sampled_quiet_interval"]


def write_insight_record(record: dict[str, Any], state_dir: Path) -> Path:
    root = state_dir.expanduser().resolve() / "insights"
    root.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    path = root / f"{day}.jsonl"
    path.open("a", encoding="utf-8").write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def signal_insight_record(
    *,
    repo: str,
    source: str,
    identifier: str,
    title: str,
    context: str,
    action_items: tuple[str, ...] | list[str],
    related: tuple[str, ...] | list[str],
    fingerprint: str,
    action_selected: InsightAction,
    sampling_class: SamplingClass,
    why_now: str,
    issue_ref: str = "",
    policy_version: str = "engineering-intake.v1",
) -> dict[str, Any]:
    utility = _utility_for_signal(action_items=action_items, related=related, issue_ref=issue_ref)
    cost = _interruption_cost_for_action(action_selected=action_selected, why_now=why_now)
    return _base_record(
        fingerprint=fingerprint,
        sampling_class=sampling_class,
        candidate_type="signal",
        candidate_source=f"engineering_intake:{source}",
        action_selected=action_selected,
        why_now=why_now,
        support_facts=[title, context, *list(action_items)[:4]],
        evidence_refs=[{"kind": "repo", "ref": repo}, *[{"kind": "related", "ref": item} for item in list(related)[:6]]],
        expected_utility=utility,
        interruption_cost=cost,
        confidence=0.75 if action_selected != "stay_silent" else 0.65,
        policy_version=policy_version,
        budget_context={"repo": repo, "issue_ref": issue_ref},
    )


def governor_insight_record(
    *,
    issue_id: str,
    title: str,
    routing_decision: str,
    reasons: list[str],
    labels: list[str],
    policy_version: str,
    action_selected: InsightAction | None = None,
    sampling_class: SamplingClass = "surfaced",
) -> dict[str, Any]:
    action = action_selected or _action_for_routing_decision(routing_decision)
    why_now = "; ".join(reasons[:3]) or f"Governor routed issue as {routing_decision}."
    return _base_record(
        fingerprint=_stable_hash(f"governor:{issue_id}:{routing_decision}"),
        sampling_class=sampling_class,
        candidate_type="github_issue",
        candidate_source="reliability_governor",
        action_selected=action,
        why_now=why_now,
        support_facts=[title, routing_decision, *reasons[:6]],
        evidence_refs=[{"kind": "github_issue", "ref": issue_id}, *[{"kind": "label", "ref": label} for label in labels]],
        expected_utility={
            "total": 0.7 if action != "stay_silent" else 0.15,
            "components": {"routing_decision": 0.7 if action != "stay_silent" else 0.15},
            "rationale": [routing_decision],
        },
        interruption_cost=_interruption_cost_for_action(action_selected=action, why_now=why_now),
        confidence=0.8,
        policy_version=policy_version,
        budget_context={"issue_id": issue_id},
    )


def daemon_insight_record(
    *,
    action_selected: InsightAction,
    sampling_class: SamplingClass,
    why_now: str,
    issue_ref: str = "",
    policy_version: str = "engineering-daemon.v1",
    budget_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_record(
        fingerprint=_stable_hash(f"daemon:{issue_ref}:{why_now}"),
        sampling_class=sampling_class,
        candidate_type="approved_queue",
        candidate_source="engineering_daemon",
        action_selected=action_selected,
        why_now=why_now,
        support_facts=[why_now, issue_ref] if issue_ref else [why_now],
        evidence_refs=[{"kind": "github_issue", "ref": issue_ref}] if issue_ref else [],
        expected_utility={
            "total": 0.75 if action_selected == "draft" else 0.05,
            "components": {"approved_queue": 0.75 if action_selected == "draft" else 0.05},
            "rationale": [why_now],
        },
        interruption_cost=_interruption_cost_for_action(action_selected=action_selected, why_now=why_now),
        confidence=0.8,
        policy_version=policy_version,
        budget_context=budget_context or {},
    )


def _base_record(
    *,
    fingerprint: str,
    sampling_class: SamplingClass,
    candidate_type: str,
    candidate_source: str,
    action_selected: InsightAction,
    why_now: str,
    support_facts: list[str],
    evidence_refs: list[dict[str, str]],
    expected_utility: dict[str, Any],
    interruption_cost: dict[str, Any],
    confidence: float,
    policy_version: str,
    budget_context: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "0.1.0",
        "insight_id": f"ins_eng_{_stable_hash(f'{fingerprint}:{now}')}",
        "loop": "engineering",
        "created_at": now,
        "fingerprint": fingerprint,
        "sampling_class": sampling_class,
        "candidate_type": candidate_type,
        "candidate_source": candidate_source,
        "state_snapshot_refs": [],
        "support_facts": [str(item) for item in support_facts if str(item).strip()][:10],
        "evidence_refs": evidence_refs,
        "action_space": ["notify", "question", "draft", "stay_silent"],
        "action_selected": action_selected,
        "why_now": why_now,
        "why_not_other_actions": _why_not_other_actions(action_selected),
        "expected_utility": expected_utility,
        "interruption_cost": interruption_cost,
        "confidence": confidence,
        "risk_class": "medium",
        "policy_version": policy_version,
        "tool_versions": {},
        "budget_context": budget_context,
    }


def _utility_for_signal(
    *, action_items: tuple[str, ...] | list[str], related: tuple[str, ...] | list[str], issue_ref: str
) -> dict[str, Any]:
    action_component = min(0.4, len(action_items) * 0.12)
    grounding_component = min(0.35, len(related) * 0.08)
    dedupe_component = 0.0 if issue_ref else 0.2
    total = round(min(1.0, action_component + grounding_component + dedupe_component), 3)
    return {
        "total": total,
        "components": {
            "actionability": action_component,
            "grounding": grounding_component,
            "novelty": dedupe_component,
        },
        "rationale": ["read-only mined signal", "candidate issue protocol"],
    }


def _interruption_cost_for_action(*, action_selected: InsightAction, why_now: str) -> dict[str, Any]:
    cost = 0.15 if action_selected in {"draft", "stay_silent"} else 0.35
    if "dedupe" in why_now.lower() or "unchanged" in why_now.lower():
        cost += 0.25
    return {
        "total": round(min(1.0, cost), 3),
        "components": {"operator_attention": cost},
        "rationale": [why_now],
    }


def _action_for_routing_decision(routing_decision: str) -> InsightAction:
    if routing_decision in {"needs_context", "knowledge_gap", "needs_human"}:
        return "question"
    if routing_decision in {"allow_candidate", "allow_approved"}:
        return "draft"
    return "stay_silent"


def _why_not_other_actions(action_selected: InsightAction) -> dict[str, str]:
    reasons: dict[str, str] = {}
    if action_selected != "notify":
        reasons["notify"] = "A durable issue/decision artifact is more appropriate than a transient notification."
    if action_selected != "question":
        reasons["question"] = "The available evidence is sufficient for the selected route."
    if action_selected != "draft":
        reasons["draft"] = "The candidate is not ready for draft work under current policy."
    if action_selected != "stay_silent":
        reasons["stay_silent"] = "The candidate is relevant now and should remain visible."
    return reasons


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
