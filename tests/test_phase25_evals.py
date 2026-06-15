from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from hyrule_engineering_loop.evals import (
    DEFAULT_CASES_DIR,
    EVAL_SCHEMA_VERSION,
    EvalCase,
    EvalError,
    grade_case,
    load_cases,
    run_evals,
    summary_json,
)

FAMILIES = ("domain-policy", "promotion-safety", "noc-evidence", "vps-launch-proof", "network-change")


def test_shipped_corpus_loads_and_all_pass() -> None:
    cases = load_cases()
    summary = run_evals(cases)
    assert summary.failed == 0, summary.failed_ids
    assert summary.passed == summary.total


def test_corpus_meets_minimum_coverage() -> None:
    cases = load_cases()
    assert len(cases) >= 15
    per_family = collections.Counter(c.family for c in cases)
    for family in FAMILIES:
        assert per_family[family] >= 3, f"{family} has only {per_family[family]} cases"
    assert all(c.schema_version == EVAL_SCHEMA_VERSION for c in cases)


def test_summary_json_shape() -> None:
    summary = run_evals(load_cases())
    payload = summary_json(summary)
    assert set(payload) == {"total", "passed", "failed", "failed_ids"}


def _case(**kw: object) -> EvalCase:
    base = {
        "schema_version": 1,
        "id": "t",
        "family": "domain-policy",
        "title": "t",
        "input": {},
        "expected_decision": "approve",
    }
    base.update(kw)
    return EvalCase.model_validate(base)


def test_rule_rejects_as215932_net_repurpose() -> None:
    case = _case(
        id="as-net",
        family="domain-policy",
        input={"issue_title": "Rename as215932.net", "issue_body": "rename as215932.net everywhere"},
        expected_decision="reject",
        must_include=["AS/routing identity"],
    )
    assert grade_case(case).passed


def test_rule_blocks_real_noc_mutation_without_guard() -> None:
    case = _case(
        id="noc",
        family="noc-evidence",
        input={"issue_title": "restart frr", "issue_body": "just restart FRR to fix BGP"},
        expected_decision="request_human_review",
        must_include=["rollback guard"],
    )
    assert grade_case(case).passed


def test_grade_case_flags_wrong_decision() -> None:
    # A safe README change is an 'approve'; asserting 'reject' must fail.
    case = _case(
        id="mismatch",
        family="network-change",
        input={"issue_title": "fix readme typo", "issue_body": "typo"},
        expected_decision="reject",
    )
    outcome = grade_case(case)
    assert not outcome.passed
    assert any("decision" in f for f in outcome.failures)


def test_grade_case_flags_missing_phrase() -> None:
    case = _case(
        id="phrase",
        family="domain-policy",
        input={"issue_title": "doc hyrule.host", "issue_body": "note hyrule.host"},
        expected_decision="approve",
        must_include=["this phrase is not in the rationale"],
    )
    assert not grade_case(case).passed


def test_load_cases_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalError):
        load_cases(tmp_path / "nope")


def test_load_cases_empty_dir_raises(tmp_path: Path) -> None:
    (tmp_path / "cases").mkdir()
    with pytest.raises(EvalError):
        load_cases(tmp_path / "cases")


def test_load_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "id": "dup",
        "family": "domain-policy",
        "title": "t",
        "input": {"issue_title": "hyrule.host", "issue_body": ""},
        "expected_decision": "approve",
    }
    (tmp_path / "a.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvalError, match="duplicate"):
        load_cases(tmp_path)


def test_default_cases_dir_points_at_repo_evals() -> None:
    assert DEFAULT_CASES_DIR.name == "cases"
    assert DEFAULT_CASES_DIR.parent.name == "evals"
