"""Capability success-history evidence (Tier-2 auto-approval prerequisite)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hyrule_engineering_loop.capability_history import (
    build_capability_history,
    load_decision_records,
    pr_outcome_for_issue,
)
from hyrule_engineering_loop.governor import CandidateDecisionRecord, write_decision_record

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def _record(issue_number: int, *, capability: str | None = "tier0.docs-runbooks-tests", created_at: str | None = None) -> CandidateDecisionRecord:
    return CandidateDecisionRecord(
        record_id=f"rdr_{issue_number}",
        created_at=created_at or "2026-07-01T00:00:00Z",
        issue_id=f"AS215932/network-operations#{issue_number}",
        repo="AS215932/network-operations",
        issue_number=issue_number,
        authority_text_hash="hash",
        issue_text_hash="hash",
        source="noc",
        intent_type="docs",
        risk_tier=0,
        blast_radius="docs-only",
        affected_assets=[],
        affected_services=[],
        affected_customers=[],
        knowledge_export_version="run:retr:pol",
        knowledge_context_pack_id="ctx",
        knowledge_authority_level="A1",
        knowledge_status="current",
        matched_capability=capability,
        routing_decision="allow_approved",
        next_loop="engineering",
        handoff_contract="github_issue_labels",
        labels_to_add=["loop:approved"],
    )


class FakeGh:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> str:
        self.calls.append(args)
        search = ""
        for index, token in enumerate(args):
            if token == "--search":
                search = args[index + 1]
        for needle, response in self.responses.items():
            if needle in search:
                return response
        return "[]"


def _merged_pr(number: int, *, merged_by: str = "svag") -> str:
    return json.dumps(
        [
            {
                "number": number,
                "state": "MERGED",
                "url": f"https://github.com/AS215932/network-operations/pull/{number}",
                "mergedAt": "2026-07-02T00:00:00Z",
                "mergedBy": {"login": merged_by},
                "title": "docs: fix",
            }
        ]
    )


def test_load_decision_records_window_and_newest_wins(tmp_path: Path) -> None:
    old = _record(1, created_at="2026-01-01T00:00:00Z")
    fresh_v1 = _record(2, created_at="2026-07-01T00:00:00Z")
    fresh_v2 = _record(2, created_at="2026-07-05T00:00:00Z")
    for record in (old, fresh_v1, fresh_v2):
        write_decision_record(record, tmp_path)
    records = load_decision_records(tmp_path, window_days=90, now=NOW)
    assert [record.issue_number for record in records] == [2]
    assert records[0].created_at == "2026-07-05T00:00:00Z"


def test_pr_outcomes() -> None:
    issue_url = "https://github.com/AS215932/network-operations/issues/2"
    merged = FakeGh({issue_url: _merged_pr(7), "Revert #7": "[]"})
    assert pr_outcome_for_issue(merged, repo="AS215932/network-operations", issue_url=issue_url)[0] == "success"

    reverted = FakeGh({issue_url: _merged_pr(7), "Revert #7": json.dumps([{"number": 9}])})
    assert pr_outcome_for_issue(reverted, repo="AS215932/network-operations", issue_url=issue_url)[0] == "failure"

    closed = FakeGh(
        {issue_url: json.dumps([{"number": 7, "state": "CLOSED", "url": "u", "mergedAt": None}])}
    )
    assert pr_outcome_for_issue(closed, repo="AS215932/network-operations", issue_url=issue_url)[0] == "failure"

    bot_merged = FakeGh({issue_url: _merged_pr(7, merged_by="some-bot[bot]")})
    assert pr_outcome_for_issue(bot_merged, repo="AS215932/network-operations", issue_url=issue_url)[0] == "pending"

    none = FakeGh({})
    assert pr_outcome_for_issue(none, repo="AS215932/network-operations", issue_url=issue_url)[0] == "none"


def test_build_history_aggregates_and_proposal(tmp_path: Path) -> None:
    write_decision_record(_record(2, created_at="2026-07-01T00:00:00Z"), tmp_path)
    write_decision_record(_record(3, created_at="2026-07-02T00:00:00Z"), tmp_path)
    write_decision_record(_record(4, created_at="2026-07-03T00:00:00Z", capability=None), tmp_path)
    gh = FakeGh(
        {
            "issues/2": _merged_pr(7),
            "issues/3": json.dumps([{"number": 8, "state": "CLOSED", "url": "u8", "mergedAt": None}]),
        }
    )
    history = build_capability_history(tmp_path, client=gh, window_days=90, now=NOW)
    # the record without a matched capability contributes nothing
    assert len(history.outcomes) == 2
    aggregates = history.aggregates()["tier0.docs-runbooks-tests"]
    assert aggregates["success"] == 1
    assert aggregates["failure"] == 1
    proposal = history.registry_proposal()["tier0.docs-runbooks-tests"]
    # both records are tier-0, so the merged one is a lower-tier success and
    # the Tier-2 gate count stays untouched
    assert proposal == {
        "success_count": 0,
        "failure_count": 1,
        "lower_tier_success_count": 1,
        "pending_count": 0,
    }
    payload = history.as_dict()
    assert payload["policy_version"] == "reliability-governor.tier2-history.v1"
    assert len(payload["evidence"]) == 2


class RaisingRevertGh(FakeGh):
    def run(self, args: list[str]) -> str:
        joined = " ".join(args)
        if "Revert" in joined:
            raise RuntimeError("rate limited")
        return super().run(args)


def test_outcome_search_requires_closing_keyword() -> None:
    issue_url = "https://github.com/AS215932/network-operations/issues/2"
    gh = FakeGh({issue_url: _merged_pr(7)})
    pr_outcome_for_issue(gh, repo="AS215932/network-operations", issue_url=issue_url)
    searches = [args[args.index("--search") + 1] for args in gh.calls if "--search" in args]
    # the PR lookup must require the closing keyword, not a bare URL mention
    assert searches[0] == f'"Closes {issue_url}" in:body'


def test_revert_detected_in_github_button_body_form() -> None:
    issue_url = "https://github.com/AS215932/network-operations/issues/2"
    gh = FakeGh(
        {
            issue_url: _merged_pr(7),
            "Reverts AS215932/network-operations#7": json.dumps([{"number": 9}]),
        }
    )
    outcome, _url, reason = pr_outcome_for_issue(
        gh, repo="AS215932/network-operations", issue_url=issue_url
    )
    assert outcome == "failure"
    assert "revert" in reason


def test_revert_lookup_failure_is_unknown_not_success() -> None:
    issue_url = "https://github.com/AS215932/network-operations/issues/2"
    gh = RaisingRevertGh({issue_url: _merged_pr(7)})
    outcome, _url, reason = pr_outcome_for_issue(
        gh, repo="AS215932/network-operations", issue_url=issue_url
    )
    assert outcome == "pending"
    assert "unknown" in reason


def test_newest_pr_sorts_numerically_not_lexicographically() -> None:
    issue_url = "https://github.com/AS215932/network-operations/issues/2"
    closed_99_merged_100 = json.dumps(
        [
            {"number": 99, "state": "CLOSED", "url": "u99", "mergedAt": None},
            {
                "number": 100,
                "state": "MERGED",
                "url": "u100",
                "mergedAt": "2026-07-02T00:00:00Z",
                "mergedBy": {"login": "svag"},
            },
        ]
    )
    gh = FakeGh({issue_url: closed_99_merged_100})
    outcome, pr_url, _reason = pr_outcome_for_issue(
        gh, repo="AS215932/network-operations", issue_url=issue_url
    )
    assert (outcome, pr_url) == ("success", "u100")


def test_null_merged_by_is_pending_not_success() -> None:
    issue_url = "https://github.com/AS215932/network-operations/issues/2"
    merged_no_actor = json.dumps(
        [
            {
                "number": 7,
                "state": "MERGED",
                "url": "u7",
                "mergedAt": "2026-07-02T00:00:00Z",
                "mergedBy": None,
            }
        ]
    )
    gh = FakeGh({issue_url: merged_no_actor})
    outcome, _url, reason = pr_outcome_for_issue(
        gh, repo="AS215932/network-operations", issue_url=issue_url
    )
    assert outcome == "pending"
    assert "merger unknown" in reason


def test_primary_lookup_failure_is_pending_and_reported() -> None:
    class RaisingGh(FakeGh):
        def run(self, args: list[str]) -> str:
            raise RuntimeError("rate limited")

    outcome, _url, reason = pr_outcome_for_issue(
        RaisingGh({}), repo="AS215932/network-operations", issue_url="https://x/issues/2"
    )
    assert outcome == "pending"
    assert "unknown" in reason


def test_registry_proposal_counts_only_tier2_successes(tmp_path: Path) -> None:
    write_decision_record(_record(2, created_at="2026-07-01T00:00:00Z"), tmp_path)  # tier 0
    tier2 = _record(3, created_at="2026-07-02T00:00:00Z")
    tier2 = tier2.model_copy(update={"risk_tier": 2, "record_id": "rdr_3b"})
    write_decision_record(tier2, tmp_path)
    gh = FakeGh({"issues/2": _merged_pr(7), "issues/3": _merged_pr(8), "Revert": "[]"})
    history = build_capability_history(tmp_path, client=gh, window_days=90, now=NOW)
    proposal = history.registry_proposal()["tier0.docs-runbooks-tests"]
    # only the tier-2 success feeds the Tier-2 gate; the tier-0 one is reported
    # separately, never mixed in
    assert proposal["success_count"] == 1
    assert proposal["lower_tier_success_count"] == 1
    assert proposal["failure_count"] == 0
    assert proposal["pending_count"] == 0
