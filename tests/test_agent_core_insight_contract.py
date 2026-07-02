from __future__ import annotations

import json
from pathlib import Path

from agent_core.contracts import InsightDecisionRecord

from hyrule_engineering_loop.insights import (
    daemon_insight_record,
    governor_insight_record,
    signal_insight_record,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "insights" / "decisions.json"


def test_engineering_insight_fixtures_validate_against_agent_core_contract() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    actions: set[str] = set()
    for row in payload["insights"]:
        actions.add(str(row["action_selected"]))
        InsightDecisionRecord.model_validate(row)

    assert actions == {"question", "draft", "stay_silent"}


def test_engineering_insight_builders_emit_agent_core_records() -> None:
    rows = [
        signal_insight_record(
            repo="AS215932/engineering-loop",
            source="fixture",
            identifier="sig-1",
            title="Fixture signal",
            context="A read-only mined signal exists.",
            action_items=["review"],
            related=["AS215932/engineering-loop#32"],
            fingerprint="sig-fp",
            action_selected="stay_silent",
            sampling_class="withheld_logged",
            why_now="Existing issue already covers the signal.",
        ),
        governor_insight_record(
            issue_id="AS215932/engineering-loop#32",
            title="Needs context",
            routing_decision="needs_context",
            reasons=["Knowledge context is missing."],
            labels=["loop:needs-context"],
            policy_version="fixture-governor.v1",
        ),
        daemon_insight_record(
            action_selected="draft",
            sampling_class="surfaced",
            why_now="Approved queue item can be drafted.",
            issue_ref="AS215932/engineering-loop#33",
            policy_version="fixture-daemon.v1",
        ),
    ]

    for row in rows:
        InsightDecisionRecord.model_validate(row)
