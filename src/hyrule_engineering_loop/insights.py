"""Private insight ledger for Engineering Loop proactivity decisions.

The public ``agent-core`` contract owns the canonical shape. This module emits
compatible JSON and validates it when records are written.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

InsightAction = Literal["notify", "question", "draft", "stay_silent"]
SamplingClass = Literal["surfaced", "withheld_logged", "sampled_quiet_interval"]

# Master switch for production insight recording (ledger + envelope emission).
# The builders below stay pure/testable; only record_insights() consults this.
INSIGHT_RECORDS_ENV = "HYRULE_ENGINEERING_INSIGHT_RECORDS"
_TRUTHY = {"1", "true", "yes", "on"}


def insight_records_enabled() -> bool:
    return os.environ.get(INSIGHT_RECORDS_ENV, "").strip().lower() in _TRUTHY


def record_insights(
    records: list[dict[str, Any]],
    state_dir: Path,
    *,
    input_event: dict[str, Any] | None = None,
) -> int:
    """Persist records to the private ledger and emit decision envelopes.

    Flag-gated by ``HYRULE_ENGINEERING_INSIGHT_RECORDS`` and strictly
    best-effort: a ledger or delivery failure only loses observability.
    Returns the count delivered to at least one sink.
    """
    if not insight_records_enabled() or not records:
        return 0
    for record in records:
        try:
            write_insight_record(record, state_dir)
        except Exception:  # noqa: BLE001 - one bad record must not drop the rest
            continue
    try:
        from hyrule_engineering_loop import agent_core_trace

        return agent_core_trace.emit_insight_decision_envelopes(
            records, input_event=input_event or {}
        )
    except Exception:  # noqa: BLE001
        return 0


def write_insight_record(record: dict[str, Any], state_dir: Path) -> Path:
    _validate_with_agent_core(record)
    root = state_dir.expanduser().resolve() / "insights"
    root.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    path = root / f"{day}.jsonl"
    path.open("a", encoding="utf-8").write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def _validate_with_agent_core(record: dict[str, Any]) -> None:
    """Validate when the released insight contract is installed.

    Older deployments may still carry an agent-core version that predates the
    insight contract; those keep emitting the already-compatible JSON until the
    repo dependency is bumped.
    """

    try:
        contracts = importlib.import_module("agent_core.contracts")
        model = getattr(contracts, "InsightDecisionRecord")
    except (ImportError, AttributeError):
        return
    model.model_validate(record)


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
    knowledge_context_pack_id: str = "",
    knowledge_export_version: str = "",
) -> dict[str, Any]:
    action = action_selected or _action_for_routing_decision(routing_decision)
    why_now = "; ".join(reasons[:3]) or f"Governor routed issue as {routing_decision}."
    knowledge_refs = (
        [{"kind": "okf_context_pack", "ref": knowledge_context_pack_id}]
        if knowledge_context_pack_id
        else []
    )
    return _base_record(
        fingerprint=_stable_hash(f"governor:{issue_id}:{routing_decision}"),
        sampling_class=sampling_class,
        candidate_type="github_issue",
        candidate_source="reliability_governor",
        action_selected=action,
        why_now=why_now,
        support_facts=[title, routing_decision, *reasons[:6]],
        evidence_refs=[
            {"kind": "github_issue", "ref": issue_id},
            *knowledge_refs,
            *[{"kind": "label", "ref": label} for label in labels],
        ],
        tool_versions=(
            {"knowledge_export": knowledge_export_version} if knowledge_export_version else {}
        ),
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
    tool_versions: dict[str, str] | None = None,
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
        "tool_versions": tool_versions or {},
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


# --- report -> insight mappings (called from the CLI entry points) -----------

_DAEMON_ACTIONS: dict[str, tuple[InsightAction, SamplingClass]] = {
    "published": ("draft", "surfaced"),
    "needs_triage": ("question", "surfaced"),
    "idle": ("stay_silent", "sampled_quiet_interval"),
    "over_budget": ("stay_silent", "withheld_logged"),
    "refused_ci": ("stay_silent", "withheld_logged"),
    "error": ("stay_silent", "withheld_logged"),
}


def daemon_report_insight(report: dict[str, Any]) -> dict[str, Any] | None:
    """One insight per daemon cycle; ``locked``/unknown outcomes emit nothing
    (another cycle is already reporting)."""
    outcome = str(report.get("outcome") or "")
    mapped = _DAEMON_ACTIONS.get(outcome)
    if mapped is None:
        return None
    action, sampling = mapped
    issue = report.get("issue") or {}
    issue_ref = str(issue.get("url") or issue.get("issue_id") or "")
    why_now = f"daemon cycle {outcome}" + (f": {report['detail']}" if report.get("detail") else "")
    budget_context: dict[str, Any] = {
        "outcome": outcome,
        "cost_usd": report.get("cost_usd", 0.0),
    }
    if report.get("pr_url"):
        budget_context["pr_url"] = report["pr_url"]
    return daemon_insight_record(
        action_selected=action,
        sampling_class=sampling,
        why_now=why_now[:300],
        issue_ref=issue_ref,
        budget_context=budget_context,
    )


def governor_report_insights(report: Any, *, dry_run: bool) -> list[dict[str, Any]]:
    """One insight per fresh Reliability Governor decision.

    ``skipped`` entries ("<issue_id>: unchanged decision ...") are the
    governor's dedup — an unchanged decision is not a new insight.
    """
    skipped_ids = {str(entry).split(":", 1)[0] for entry in getattr(report, "skipped", [])}
    records: list[dict[str, Any]] = []
    for decision in getattr(report, "records", []):
        issue_id = str(getattr(decision, "issue_id", ""))
        if issue_id in skipped_ids:
            continue
        reasons = list(getattr(decision, "denial_reasons", []) or []) or list(
            getattr(decision, "policy_rules", []) or []
        )
        # Issue prose is untrusted (LHP rule); identify the issue structurally.
        title = f"{getattr(decision, 'repo', '')}#{getattr(decision, 'issue_number', '')} {getattr(decision, 'intent_type', '')}".strip()
        records.append(
            governor_insight_record(
                issue_id=issue_id,
                title=title,
                routing_decision=str(getattr(decision, "routing_decision", "")),
                reasons=[str(reason) for reason in reasons],
                labels=[str(label) for label in getattr(decision, "labels_to_add", []) or []],
                policy_version=str(getattr(decision, "schema_version", "governor")),
                sampling_class="withheld_logged" if dry_run else "surfaced",
                knowledge_context_pack_id=str(getattr(decision, "knowledge_context_pack_id", "")),
                knowledge_export_version=str(getattr(decision, "knowledge_export_version", "")),
            )
        )
    return records


def intake_report_insights(
    report: Any, signals: list[Any], *, repo: str, dry_run: bool
) -> list[dict[str, Any]]:
    """Filed signals surface as drafts; fingerprint-deduped ones are explicit
    silence (the open issue already carries the information)."""
    by_fingerprint = {getattr(signal, "fingerprint", ""): signal for signal in signals}
    records: list[dict[str, Any]] = []
    for entry in getattr(report, "filed", []):
        signal = by_fingerprint.get(str(entry.get("fingerprint") or ""))
        records.append(
            signal_insight_record(
                repo=repo,
                source=str(entry.get("source") or "intake"),
                identifier=str(entry.get("fingerprint") or ""),
                title=str(entry.get("title") or ""),
                context=str(getattr(signal, "context", "") or "")[:300],
                action_items=list(getattr(signal, "action_items", []) or []),
                related=list(getattr(signal, "related", []) or []),
                fingerprint=str(entry.get("fingerprint") or ""),
                action_selected="draft",
                sampling_class="withheld_logged" if dry_run else "surfaced",
                why_now="new mined signal filed as loop:candidate"
                + (" (dry-run, nothing filed)" if dry_run else ""),
                issue_ref=str(entry.get("url") or ""),
            )
        )
    for entry in getattr(report, "deduplicated", []):
        signal = by_fingerprint.get(str(entry.get("fingerprint") or ""))
        records.append(
            signal_insight_record(
                repo=repo,
                source=str(getattr(signal, "source", "") or "intake"),
                identifier=str(entry.get("fingerprint") or ""),
                title=str(entry.get("title") or ""),
                context="",
                action_items=[],
                related=[],
                fingerprint=str(entry.get("fingerprint") or ""),
                action_selected="stay_silent",
                sampling_class="withheld_logged",
                why_now=f"fingerprint dedupe: open issue #{entry.get('existing_issue')}",
                issue_ref=str(entry.get("existing_issue") or ""),
            )
        )
    return records
