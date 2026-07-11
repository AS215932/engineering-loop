from __future__ import annotations

from typing import Any

import pytest
from agent_core.contracts import ApprovalRecord, HandoffEnvelope, HandoffRecord

import hyrule_engineering_loop.coordination as coordination
from hyrule_engineering_loop.daemon import DaemonConfig, DaemonReport


class FakeCoordinator:
    def __init__(self, record: HandoffRecord) -> None:
        self.record = record
        self.results: list[Any] = []

    async def heartbeat(self, payload):  # type: ignore[no-untyped-def]
        return payload

    async def inbox(self, *, status: str):
        return [self.record] if self.record.status == status else []

    async def claim(self, handoff_id: str, *, lease_seconds: int):
        return self.record.model_copy(update={"status": "claimed", "claim_owner": "engineering"})

    async def progress(self, handoff_id: str, summary: str):
        return self.record

    async def heartbeat_claim(self, handoff_id: str, *, lease_seconds: int):
        return self.record

    async def submit_result(self, result):  # type: ignore[no-untyped-def]
        self.results.append(result)
        return self.record


class FakeGh:
    def run(self, args: list[str]) -> str:
        raise AssertionError(f"coordinator intake must not read an approval issue: {args}")


def _record(*, approved: bool = True) -> HandoffRecord:
    envelope = HandoffEnvelope(
        source_loop="soc",
        target_loop="engineering",
        capability="engineering.draft_pr",
        intent="Draft bounded documentation change",
        summary="Document the verified security control",
        risk_level="medium",
        approval_tier="operator",
        payload={"repository": "AS215932/network-operations"},
        constraints={
            "allowed_repository": "AS215932/network-operations",
            "allowed_paths": ["docs"],
            "draft_pr_only": True,
        },
        idempotency_key="soc:engineering:1",
    )
    approval = None
    if approved:
        approval = ApprovalRecord(
            handoff_id=envelope.handoff_id,
            scope_hash=envelope.scope_hash,
            decision="approved",
            approver_id="github:123",
            approver_role="operator",
        )
    return HandoffRecord(
        envelope=envelope,
        status="queued",
        approval=approval,
    )


@pytest.mark.asyncio
async def test_coordinator_work_reuses_daemon_safety_and_returns_draft_pr(monkeypatch) -> None:
    record = _record()
    client = FakeCoordinator(record)
    captured: dict[str, Any] = {}

    def fake_daemon(config, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return DaemonReport(
            outcome="published",
            detail="draft PR published",
            change_id="LHP_TEST",
            pr_url="https://github.com/AS215932/network-operations/pull/999",
        )

    monkeypatch.setattr(coordination, "daemon_once", fake_daemon)
    config = DaemonConfig(
        repos=("AS215932/network-operations",),
        allowed_paths_by_repo={"hyrule-infra": ("docs",)},
    )
    report = await coordination.coordinator_daemon_once(
        config,
        gh_client=FakeGh(),
        coordinator=client,  # type: ignore[arg-type]
    )
    assert report.outcome == "published"
    assert captured["approved_item"].number == 0
    assert captured["approval_scope_override"].allowed_paths == ("docs",)
    assert "scope hash" in captured["approved_body"]
    assert client.results[0].outcome == "succeeded"
    assert client.results[0].payload["draft_pr_only"] is True
    assert client.results[0].payload["production_executed"] is False


@pytest.mark.asyncio
async def test_coordinator_work_requires_immutable_approval() -> None:
    record = _record(approved=False)
    config = DaemonConfig(
        repos=("AS215932/network-operations",),
        allowed_paths_by_repo={"hyrule-infra": ("docs",)},
    )
    with pytest.raises(ValueError, match="no coordinator approval"):
        await coordination.coordinator_daemon_once(
            config,
            gh_client=FakeGh(),
            coordinator=FakeCoordinator(record),  # type: ignore[arg-type]
        )
