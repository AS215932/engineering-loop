"""Report -> insight mappings and the flag-gated recording path."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_core.contracts import InsightDecisionRecord

from hyrule_engineering_loop import agent_core_trace
from hyrule_engineering_loop.insights import (
    daemon_report_insight,
    governor_report_insights,
    intake_report_insights,
    record_insights,
)


def _daemon_report(**overrides) -> dict:
    report = {"outcome": "idle", "detail": "", "issue": None, "cost_usd": 0.0, "pr_url": None}
    report.update(overrides)
    return report


def test_daemon_report_insight_action_table() -> None:
    published = daemon_report_insight(
        _daemon_report(
            outcome="published",
            pr_url="https://gh/pr/7",
            issue={"url": "https://gh/issue/5"},
        )
    )
    assert published is not None
    assert (published["action_selected"], published["sampling_class"]) == ("draft", "surfaced")
    assert published["budget_context"]["pr_url"] == "https://gh/pr/7"

    triage = daemon_report_insight(_daemon_report(outcome="needs_triage", detail="gates failed"))
    assert triage is not None
    assert (triage["action_selected"], triage["sampling_class"]) == ("question", "surfaced")
    assert "gates failed" in triage["why_now"]

    idle = daemon_report_insight(_daemon_report(outcome="idle"))
    assert idle is not None
    assert (idle["action_selected"], idle["sampling_class"]) == (
        "stay_silent",
        "sampled_quiet_interval",
    )

    for outcome in ("over_budget", "refused_ci", "error"):
        withheld = daemon_report_insight(_daemon_report(outcome=outcome))
        assert withheld is not None
        assert (withheld["action_selected"], withheld["sampling_class"]) == (
            "stay_silent",
            "withheld_logged",
        )

    assert daemon_report_insight(_daemon_report(outcome="locked")) is None
    assert daemon_report_insight(_daemon_report(outcome="unknown-outcome")) is None


def _decision(issue_id: str, routing: str, **overrides) -> SimpleNamespace:
    fields = {
        "issue_id": issue_id,
        "repo": "AS215932/network-operations",
        "issue_number": int(issue_id.rsplit("#", 1)[-1]),
        "intent_type": "config_change",
        "routing_decision": routing,
        "denial_reasons": [],
        "policy_rules": ["rule-1"],
        "labels_to_add": ["loop:candidate"],
        "schema_version": "cdr.v3",
        "knowledge_context_pack_id": "ctx_abc",
        "knowledge_export_version": "run1:retr1:pol1",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_governor_report_insights_skips_unchanged_and_maps_routing() -> None:
    report = SimpleNamespace(
        records=[
            _decision("AS215932/network-operations#10", "allow_candidate"),
            _decision("AS215932/network-operations#11", "needs_human", denial_reasons=["tier 3"]),
            _decision("AS215932/network-operations#12", "allow_approved"),
        ],
        skipped=["AS215932/network-operations#12: unchanged decision rdr_1"],
    )
    records = governor_report_insights(report, dry_run=False)
    assert [r["action_selected"] for r in records] == ["draft", "question"]
    assert all(r["sampling_class"] == "surfaced" for r in records)
    assert "tier 3" in records[1]["why_now"]
    # knowledge context is cited and versioned
    kinds = {ref["kind"] for ref in records[0]["evidence_refs"]}
    assert "okf_context_pack" in kinds
    assert records[0]["tool_versions"] == {"knowledge_export": "run1:retr1:pol1"}
    # issue prose is untrusted; the title is structural
    assert records[0]["support_facts"][0] == "AS215932/network-operations#10 config_change"


def test_governor_report_insights_dry_run_is_withheld() -> None:
    report = SimpleNamespace(
        records=[_decision("AS215932/network-operations#10", "allow_candidate")], skipped=[]
    )
    records = governor_report_insights(report, dry_run=True)
    assert records[0]["sampling_class"] == "withheld_logged"


def test_intake_report_insights_filed_and_deduped() -> None:
    signal = SimpleNamespace(
        fingerprint="fp1",
        source="ci_failures",
        context="CI flaked twice this week.",
        action_items=["stabilize test"],
        related=["AS215932/network-operations#3"],
    )
    report = SimpleNamespace(
        filed=[
            {
                "title": "Stabilize flaky CI",
                "fingerprint": "fp1",
                "repo": "AS215932/network-operations",
                "source": "ci_failures",
                "url": "https://gh/issue/42",
            }
        ],
        deduplicated=[{"title": "Old signal", "fingerprint": "fp2", "existing_issue": 17}],
    )
    records = intake_report_insights(
        report, [signal], repo="AS215932/network-operations", dry_run=False
    )
    assert [r["action_selected"] for r in records] == ["draft", "stay_silent"]
    assert records[0]["sampling_class"] == "surfaced"
    assert records[1]["sampling_class"] == "withheld_logged"
    assert "dedupe" in records[1]["why_now"]


def test_all_mapped_records_validate_against_contract() -> None:
    report = SimpleNamespace(
        records=[_decision("AS215932/network-operations#10", "knowledge_gap")], skipped=[]
    )
    rows = governor_report_insights(report, dry_run=False)
    rows.append(daemon_report_insight(_daemon_report(outcome="published", pr_url="u")))
    for row in rows:
        InsightDecisionRecord.model_validate(row)


def test_record_insights_flag_off_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HYRULE_ENGINEERING_INSIGHT_RECORDS", raising=False)
    count = record_insights(
        [daemon_report_insight(_daemon_report(outcome="idle"))], tmp_path
    )
    assert count == 0
    assert not (tmp_path / "insights").exists()


def test_record_insights_writes_ledger_and_emits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYRULE_ENGINEERING_INSIGHT_RECORDS", "1")
    emitted: list[dict] = []
    monkeypatch.setattr(
        agent_core_trace,
        "emit_insight_decision_envelopes",
        lambda records, *, input_event=None: emitted.extend(records) or len(records),
    )
    record = daemon_report_insight(_daemon_report(outcome="published", pr_url="u"))
    assert record is not None
    count = record_insights([record], tmp_path, input_event={"component": "test"})
    assert count == 1
    assert emitted and emitted[0]["insight_id"] == record["insight_id"]
    ledger_files = list((tmp_path / "insights").glob("*.jsonl"))
    assert len(ledger_files) == 1
    stored = json.loads(ledger_files[0].read_text(encoding="utf-8").strip())
    assert stored["action_selected"] == "draft"


def test_emit_insight_decision_envelopes_ships_full_record(tmp_path, monkeypatch) -> None:
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv(agent_core_trace.FLAG_ENV, "1")
    monkeypatch.setenv(agent_core_trace.PATH_ENV, str(trace_path))
    record = daemon_report_insight(_daemon_report(outcome="idle"))
    assert record is not None
    delivered = agent_core_trace.emit_insight_decision_envelopes(
        [record], input_event={"run_id": "cycle-1"}
    )
    assert delivered == 1
    event = json.loads(trace_path.read_text(encoding="utf-8").strip())
    assert event["event_type"] == "loop_decision_envelope"
    envelope = event["payload"]["loop_decision_envelope"]
    assert envelope["loop"] == "engineering"
    # untrusted-text guard fields + run correlation when no explicit trace id
    assert event["payload"]["untrusted_loop_text"] is True
    assert event["payload"]["model_consumption_allowed"] is False
    assert envelope["trace_id"] == "cycle-1"
    full = event["payload"]["insight_decision_record"]
    assert full["sampling_class"] == "sampled_quiet_interval"
    assert full["action_selected"] == "stay_silent"


def test_daemon_insight_builds_structural_issue_ref() -> None:
    record = daemon_report_insight(
        _daemon_report(
            outcome="needs_triage",
            issue={"repo": "AS215932/network-operations", "number": 42, "title": "t"},
        )
    )
    assert record is not None
    ref = "https://github.com/AS215932/network-operations/issues/42"
    assert record["evidence_refs"] == [{"kind": "github_issue", "ref": ref}]
