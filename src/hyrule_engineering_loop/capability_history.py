"""Trustworthy success history for capability-envelope auto-approval.

``decide_policy`` already contains the dormant Tier-2 branch: a capability
auto-approves tier-2 work iff its registry entry allows it AND
``success_count >= DEFAULT_STRONG_HISTORY_SUCCESSES`` with zero failures.
Every registry entry ships ``success_count: 0``, so the branch never fires.

This module builds the evidence that could justify raising those numbers:
it joins stored CandidateDecisionRecords (the governor's audit trail) with
the terminal state of the pull requests that carried out the work (via the
same ``gh`` CLI client the intake uses) and emits a per-capability report
plus a proposed registry patch. The patch is NEVER applied automatically —
registry raises land as reviewed promotion PRs against the deployed registry
in network-operations, stamped policy_version
``reliability-governor.tier2-history.v1``.

Outcome rules (90-day window by default):
- success: a PR whose body closes the issue was merged (branch protection
  means required checks passed) by a human (non-bot login), with no revert
  PR referencing it afterwards.
- failure: the PR was closed unmerged, or a revert referencing it merged.
- pending/none: open PRs and issues without PRs are excluded from counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hyrule_engineering_loop.governor import CandidateDecisionRecord
from hyrule_engineering_loop.intake import GhClient

HISTORY_POLICY_VERSION = "reliability-governor.tier2-history.v1"
DEFAULT_WINDOW_DAYS = 90


@dataclass(frozen=True)
class CapabilityOutcome:
    capability: str
    issue_id: str
    pr_url: str
    outcome: str  # success | failure | pending | none
    reason: str
    risk_tier: int = 0


@dataclass
class CapabilityHistory:
    window_days: int
    generated_at: str
    outcomes: list[CapabilityOutcome] = field(default_factory=list)

    def aggregates(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        for outcome in self.outcomes:
            bucket = totals.setdefault(outcome.capability, {"success": 0, "failure": 0, "pending": 0, "none": 0})
            bucket[outcome.outcome] = bucket.get(outcome.outcome, 0) + 1
        return dict(sorted(totals.items()))

    def registry_proposal(self) -> dict[str, Any]:
        """Proposed counts per capability (report artifact).

        ``success_count`` counts only tier>=2 successes — the numbers exist to
        unlock ``decide_policy``'s Tier-2 gate, so a streak of human-merged
        Tier-0/1 work must not satisfy it. Lower-tier successes and
        pending/unknown outcomes are reported alongside so the promotion-PR
        reviewer sees exactly what the evidence covers (and what it doesn't).
        """
        proposal: dict[str, Any] = {}
        for capability in sorted({outcome.capability for outcome in self.outcomes}):
            rows = [outcome for outcome in self.outcomes if outcome.capability == capability]
            proposal[capability] = {
                "success_count": sum(
                    1 for row in rows if row.outcome == "success" and row.risk_tier >= 2
                ),
                "failure_count": sum(1 for row in rows if row.outcome == "failure"),
                "lower_tier_success_count": sum(
                    1 for row in rows if row.outcome == "success" and row.risk_tier < 2
                ),
                "pending_count": sum(1 for row in rows if row.outcome == "pending"),
            }
        return proposal

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": HISTORY_POLICY_VERSION,
            "window_days": self.window_days,
            "generated_at": self.generated_at,
            "aggregates": self.aggregates(),
            "registry_proposal": self.registry_proposal(),
            "evidence": [
                {
                    "capability": item.capability,
                    "issue_id": item.issue_id,
                    "pr_url": item.pr_url,
                    "outcome": item.outcome,
                    "reason": item.reason,
                    "risk_tier": item.risk_tier,
                }
                for item in self.outcomes
            ],
        }


def load_decision_records(state_dir: Path, *, window_days: int = DEFAULT_WINDOW_DAYS, now: datetime | None = None) -> list[CandidateDecisionRecord]:
    """Stored governor decision records inside the evidence window, one per
    (repo, issue): the newest record wins (label transitions re-record)."""
    root = state_dir.expanduser().resolve()
    if not root.exists():
        return []
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    newest: dict[str, CandidateDecisionRecord] = {}
    for path in sorted(root.glob("*.json")):
        try:
            record = CandidateDecisionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        created = _parse_ts(record.created_at)
        if created is None or created < cutoff:
            continue
        key = record.issue_id
        prior = newest.get(key)
        if prior is None or str(record.created_at) > str(prior.created_at):
            newest[key] = record
    return [newest[key] for key in sorted(newest)]


def pr_outcome_for_issue(client: GhClient, *, repo: str, issue_url: str) -> tuple[str, str, str]:
    """(outcome, pr_url, reason) for the newest PR CLOSING this issue.

    The search requires the closing keyword the daemon writes verbatim
    (``Closes <issue_url>``), so PRs that merely mention the issue as related
    context are never counted as its outcome."""
    try:
        raw = client.run(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--search",
                f'"Closes {issue_url}" in:body',
                "--json",
                "number,state,url,mergedAt,mergedBy,title",
                "--limit",
                "10",
            ]
        )
        rows = json.loads(raw or "[]")
    except Exception as exc:
        # Unknown, not absent: a rate-limited/auth-failed lookup must stay
        # visible in the report instead of vanishing from the counts.
        return "pending", "", f"pr lookup failed ({exc.__class__.__name__}); outcome unknown"
    if not isinstance(rows, list) or not rows:
        return "none", "", "no PR closes the issue"
    rows.sort(key=lambda row: int(row.get("number") or 0), reverse=True)
    pr = rows[0]
    pr_url = str(pr.get("url") or "")
    state = str(pr.get("state") or "").upper()
    if state == "OPEN":
        return "pending", pr_url, "PR still open"
    if state == "CLOSED" and not pr.get("mergedAt"):
        return "failure", pr_url, "PR closed without merge"
    merged_by = str((pr.get("mergedBy") or {}).get("login") or "")
    if not merged_by:
        # mergedBy is nullable (deleted/unavailable actor) — no evidence of a
        # human merge, so this must not count toward zero-failure history.
        return "pending", pr_url, "merger unknown (mergedBy null); needs human-merge evidence"
    if merged_by.endswith("[bot]"):
        return "pending", pr_url, f"merged by bot ({merged_by}); needs human-merge evidence"
    reverted = _was_reverted(client, repo=repo, pr_number=int(pr.get("number") or 0))
    if reverted is None:
        # Unknown revert state must never manufacture a success for a
        # zero-failure history gate.
        return "pending", pr_url, "revert lookup failed; outcome unknown"
    if reverted:
        return "failure", pr_url, "a merged revert references this PR"
    return "success", pr_url, f"merged by {merged_by or 'human'} behind required checks"


def _was_reverted(client: GhClient, *, repo: str, pr_number: int) -> bool | None:
    """True/False when determinable, None when the lookup itself failed.

    Covers both revert forms: GitHub's revert button writes ``Reverts
    <owner>/<repo>#N`` into the BODY (title is ``Revert "<original title>"``),
    while hand-written reverts commonly carry ``Revert #N`` in the title."""
    if not pr_number:
        return False
    for search in (f'"Reverts {repo}#{pr_number}" in:body', f'"Revert #{pr_number}" in:title'):
        try:
            raw = client.run(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "merged",
                    "--search",
                    search,
                    "--json",
                    "number",
                    "--limit",
                    "3",
                ]
            )
            rows = json.loads(raw or "[]")
        except Exception:
            return None
        if isinstance(rows, list) and rows:
            return True
    return False


def build_capability_history(
    state_dir: Path,
    *,
    client: GhClient,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> CapabilityHistory:
    now = now or datetime.now(UTC)
    history = CapabilityHistory(window_days=window_days, generated_at=now.isoformat())
    for record in load_decision_records(state_dir, window_days=window_days, now=now):
        if not record.matched_capability:
            continue
        issue_url = f"https://github.com/{record.repo}/issues/{record.issue_number}"
        outcome, pr_url, reason = pr_outcome_for_issue(client, repo=record.repo, issue_url=issue_url)
        history.outcomes.append(
            CapabilityOutcome(
                capability=record.matched_capability,
                issue_id=record.issue_id,
                pr_url=pr_url,
                outcome=outcome,
                reason=reason,
                risk_tier=int(record.risk_tier),
            )
        )
    return history


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
