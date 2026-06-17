from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyrule_engineering_loop.knowledge_learning import (
    KnowledgeLearningError,
    build_learning_event_from_state,
    write_learning_event_for_state,
)
from hyrule_engineering_loop.state import GraphState


def _state() -> GraphState:
    return {
        "change_id": "LEARN_TEST",
        "change_class": "app_feature",
        "risk_level": "low",
        "customer_impact": "none",
        "source_of_truth_files": ["hyrule-cloud:README.md"],
        "proposed_mutations": {},
        "mcp_schema_breaking": False,
        "emulated_lab_verified": "not_applicable",
        "validation_errors": [],
        "role_approvals": {},
        "retry_counters": {},
        "rollback_plan": "discard local worktree",
        "noc_handoff_metadata": {},
        "requires_human_signoff": True,
        "approval_decision": "pending",
        "feature_target_repo": "hyrule-cloud",
        "gate_status": "passed",
        "policy_status": "passed",
        "promotion_status": "passed",
        "signoff_status": "ready_for_review",
        "knowledge_context_status": "ok",
        "knowledge_context_pack": {
            "id": "ctx_test",
            "policy_decision": {"id": "pol_test", "result": "allow"},
            "included_refs": [
                {
                    "concept_id": "generated/services/hyrule-cloud",
                    "source_refs": [{"repo": "AS215932/hyrule-cloud", "path": "README.md", "commit": "abc123"}],
                    "retrieval_scores": {"exact": 1.0, "graph": None, "fts": None, "vector": None},
                }
            ],
        },
    }


def test_build_learning_event_from_state_is_sanitized() -> None:
    event = build_learning_event_from_state(_state())
    raw = json.dumps(event, sort_keys=True)
    assert event["ledger_version"] == "learning_ledger_v1"
    assert event["event_type"] == "engineering_loop_run_summary"
    assert event["authority_tier"] == "A4"
    assert event["context_pack_ids"] == ["ctx_test"]
    assert event["policy_decision_ids"] == ["pol_test"]
    assert event["metrics"]["vector_scores_null"] is True
    assert "feature_request" not in raw
    assert "diff_excerpt" not in raw
    assert "stdout" not in raw


def test_write_learning_event_for_state(tmp_path: Path) -> None:
    path = write_learning_event_for_state(_state(), tmp_path)
    assert path.name == "LEARN_TEST.learning-event.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["subject"] == "engineering_loop:LEARN_TEST"


def test_learning_event_rejects_secret_like_context() -> None:
    state = _state()
    state["knowledge_context_pack"]["included_refs"][0]["source_refs"][0]["path"] = "Authorization: Bearer abc"
    with pytest.raises(KnowledgeLearningError):
        build_learning_event_from_state(state)
