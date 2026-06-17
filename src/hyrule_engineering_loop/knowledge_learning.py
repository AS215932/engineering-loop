"""Local sanitized learning-event artifacts for AS215932/knowledge.

Engineering Loop does not write to the knowledge repository. When explicitly
enabled, it writes a compact local JSON event that a human can inspect and later
promote into the knowledge learning ledger. Raw prompts, diffs, transcripts,
stdout/stderr, and secrets are intentionally excluded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyrule_engineering_loop.state import GraphState

LEDGER_VERSION = "learning_ledger_v1"
SECRET_VALUE_RE = re.compile(r"(-----BEGIN [A-Z ]+PRIVATE KEY-----|\bBearer\s+|authorization:\s*bearer|password\s*[:=])", re.I)
FORBIDDEN_TEXT = {"stdout", "stderr", "raw_log", "packet_capture", "transcript", "secret", "credential", "authorization"}


class KnowledgeLearningError(RuntimeError):
    """Raised when a learning event would violate sanitization rules."""


@dataclass(frozen=True)
class KnowledgeLearningConfig:
    output_dir: Path


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_id(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "learn_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_learning_event_from_state(state: GraphState) -> dict[str, Any]:
    change_id = state["change_id"]
    raw_context_pack = state.get("knowledge_context_pack")
    context_pack: dict[str, Any] = raw_context_pack if isinstance(raw_context_pack, dict) else {}
    included_refs = context_pack.get("included_refs", [])
    citations = _citations_from_refs(included_refs)
    if context_pack.get("id"):
        citations.append({"context_pack_id": str(context_pack["id"])})
    if not citations:
        citations.append({"source_uri": "local://engineering-loop/sanitized-run-summary"})

    policy_decision = context_pack.get("policy_decision", {}) if isinstance(context_pack.get("policy_decision"), dict) else {}
    policy_ids = [str(policy_decision["id"])] if policy_decision.get("id") else []
    subject = f"engineering_loop:{change_id}"
    repo = state.get("feature_target_repo") or ",".join(state.get("promotion_repo_names", [])) or "unknown"
    summary = (
        f"Engineering Loop run {change_id} for {repo} produced a sanitized local learning summary: "
        f"gate={state.get('gate_status', 'not_run')} policy={state.get('policy_status', 'not_run')} "
        f"promotion={state.get('promotion_status', 'not_requested')} signoff={state.get('signoff_status', 'unknown')}."
    )
    metrics = {
        "change_class": state["change_class"],
        "risk_level": state["risk_level"],
        "customer_impact": state["customer_impact"],
        "gate_status": state.get("gate_status", "not_run"),
        "policy_status": state.get("policy_status", "not_run"),
        "promotion_status": state.get("promotion_status", "not_requested"),
        "signoff_status": state.get("signoff_status", "unknown"),
        "requires_human_signoff": state.get("requires_human_signoff", False),
        "knowledge_context_status": state.get("knowledge_context_status", "disabled"),
        "included_ref_count": len(included_refs) if isinstance(included_refs, list) else 0,
        "vector_scores_null": _vector_scores_null(included_refs),
        "validation_error_count": len(state.get("validation_errors", [])),
    }
    event = {
        "ledger_version": LEDGER_VERSION,
        "event_type": "engineering_loop_run_summary",
        "event_time": _utc_now(),
        "producer": "engineering_loop",
        "subject": subject,
        "summary": summary,
        "status": "proposed",
        "authority_tier": "A4",
        "source": {"kind": "local_artifact", "repo": "AS215932/engineering-loop"},
        "data_classes": ["sanitized_trace_summary", "source_ref", "okf_concept", "policy_decision"],
        "citations": citations[:25],
        "context_pack_ids": [str(context_pack["id"])] if context_pack.get("id") else [],
        "policy_decision_ids": policy_ids,
        "eval_case_ids": [],
        "metrics": metrics,
        "lessons": [
            "Promote this summary only after human review; do not treat local proposed learning events as source truth.",
        ],
        "promotion": {"review_required": True, "target": "AS215932/knowledge:ledger/fixtures-or-curated-lessons"},
        "metadata": {
            "state_summary_only": True,
            "raw_prompts_excluded": True,
            "raw_diffs_excluded": True,
            "raw_tool_output_excluded": True,
        },
    }
    event["id"] = _stable_id([event["event_type"], event["producer"], event["subject"], event["citations"], event["metrics"]])
    _validate_sanitized(event)
    return event


def write_learning_event_for_state(state: GraphState, output_dir: Path) -> Path:
    event = build_learning_event_from_state(state)
    root = output_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{state['change_id']}.learning-event.json"
    path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _citations_from_refs(refs: Any) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    if not isinstance(refs, list):
        return citations
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        citation: dict[str, str] = {}
        if ref.get("concept_id"):
            citation["concept_id"] = str(ref["concept_id"])
        source_refs = ref.get("source_refs") if isinstance(ref.get("source_refs"), list) else []
        if source_refs and isinstance(source_refs[0], dict):
            source = source_refs[0]
            repo = source.get("repo")
            path = source.get("path")
            commit = source.get("commit") or ""
            if repo and path:
                citation["source_uri"] = f"repo://{repo}/{path}@{commit}"
        if citation:
            citations.append(citation)
    return citations


def _vector_scores_null(refs: Any) -> bool:
    if not isinstance(refs, list):
        return True
    for ref in refs:
        if isinstance(ref, dict) and isinstance(ref.get("retrieval_scores"), dict):
            if ref["retrieval_scores"].get("vector") is not None:
                return False
    return True


def _validate_sanitized(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in FORBIDDEN_TEXT):
                raise KnowledgeLearningError(f"forbidden key in learning event at {path}.{key}")
            _validate_sanitized(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_sanitized(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise KnowledgeLearningError(f"secret-like value in learning event at {path}")
