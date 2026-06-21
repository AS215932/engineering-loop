"""Phase F (v2): the budgeted, locked, observable operations lane."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from hyrule_engineering_loop.daemon import (
    CORE_REPOS,
    DaemonConfig,
    DaemonReport,
    acquire_lock,
    backend_budget_for_issue,
    classify_issue,
    daemon_once,
    notify_discord,
    notify_icinga,
    repo_name_for_issue,
    _default_http_post,
)
from hyrule_engineering_loop.cli import build_parser
from hyrule_engineering_loop.intake import IntakeItem
from hyrule_engineering_loop.nodes import STALL_ROUND_LIMIT, delegate_implementation_node
from hyrule_engineering_loop.promotion import rollback_promotions, setup_worktrees_for_state
from hyrule_engineering_loop.state import GraphState


@pytest.fixture(autouse=True)
def _no_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The daemon refuses to run when GITHUB_ACTIONS is set (the CI-runner
    # guard). CI itself sets GITHUB_ACTIONS=true, so clear it here; the
    # dedicated refusal test sets it back explicitly.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, check=False, text=True)
    assert completed.returncode == 0, completed.stderr


def _init_repo(path: Path, *, remote: Path | None = None) -> None:
    path.mkdir(parents=True)
    _run(["git", "init"], path)
    _run(["git", "config", "user.email", "loop@example.invalid"], path)
    _run(["git", "config", "user.name", "Engineering Loop"], path)
    (path / "README.md").write_text(f"{path.name}\n", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "initial"], path)
    if remote is not None:
        _run(["git", "init", "--bare", str(remote)], path)
        _run(["git", "remote", "add", "origin", str(remote)], path)


class FakeGh:
    """Records gh calls; serves canned JSON by command prefix."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses

    def run(self, args: list[str]) -> str:
        self.calls.append(list(args))
        key = " ".join(args[:2])
        return self.responses.get(key, "[]")


def _approved_issue_json(number: int, *, repo: str, labels: list[str]) -> str:
    return json.dumps(
        [
            {
                "number": number,
                "title": "Add a docs note",
                "body": "## Context\nx\n## Action items\n1. y\n## Related\n- z",
                "labels": [{"name": name} for name in labels],
                "url": f"https://github.com/{repo}/issues/{number}",
                "updatedAt": "2026-06-12T00:00:00Z",
            }
        ]
    )


# --- AC1: run lock ----------------------------------------------------------


def test_second_invocation_exits_immediately_on_the_lock(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    # A live, fresh lock held by this very process.
    held = acquire_lock(state_dir, max_age_seconds=3600)
    assert held is not None

    config = DaemonConfig(state_dir=state_dir, output_root=tmp_path / "out")
    gh = FakeGh({"issue list": "[]"})
    report = daemon_once(config, client=gh)

    assert report.outcome == "locked"
    # Locked cycle does not even query the queue, and stays silent.
    assert gh.calls == []
    assert report.notifications == []


def test_stale_lock_is_broken(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "daemon.lock").write_text(
        json.dumps({"pid": 999_999_999, "started_at": 0.0}), encoding="utf-8"
    )
    lock = acquire_lock(state_dir, max_age_seconds=3600)
    assert lock is not None  # dead-pid lock was broken and re-taken


# --- AC: CI-runner refusal --------------------------------------------------


def test_daemon_refuses_to_run_on_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    config = DaemonConfig(state_dir=tmp_path / "state")
    report = daemon_once(config, client=FakeGh({}))
    assert report.outcome == "refused_ci"


# --- AC4: end-to-end seeded issue -> draft PR, no human input --------------


def test_seeded_approved_issue_becomes_draft_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_repo(workspace / "hyrule-cloud", remote=tmp_path / "hyrule-cloud.git")
    repo = "AS215932/hyrule-cloud"

    gh = FakeGh(
        {
            "issue list": _approved_issue_json(241, repo=repo, labels=["loop:approved", "monitoring"]),
            "issue view": json.dumps({"body": "## Context\nAdd a docs note.\n"}),
        }
    )
    monkeypatch.setenv("HYRULE_MOCK_GITHUB_PR_URL", "https://github.invalid/pr/1")

    discord: list[dict[str, Any]] = []
    icinga: list[dict[str, Any]] = []
    monkeypatch.setenv("HYRULE_DISCORD_WEBHOOK", "https://discord.invalid/webhook")
    monkeypatch.setenv("HYRULE_ICINGA_URL", "https://mon.invalid:5665")
    monkeypatch.setenv("HYRULE_ICINGA_USER", "loop")
    monkeypatch.setenv("HYRULE_ICINGA_PASSWORD", "x")

    config = DaemonConfig(
        repos=(repo,),
        workspace_root=workspace,
        output_root=tmp_path / "runs",
        state_dir=tmp_path / "state",
        memory_dir=str(tmp_path / "memory"),
    )

    report = daemon_once(
        config,
        client=gh,
        discord_poster=lambda url, payload: discord.append(payload),
        icinga_poster=lambda url, payload: icinga.append(payload),
    )

    # No human input between "timer fire" (daemon_once) and the draft PR.
    assert report.outcome == "published"
    assert report.pr_url == "https://github.invalid/pr/1"
    assert report.change_id == "ISSUE_HYRULE_CLOUD_241"

    # The branch was really pushed to the bare remote.
    branches = subprocess.run(
        ["git", "branch", "-a"],
        cwd=tmp_path / "hyrule-cloud.git",
        capture_output=True,
        text=True,
    ).stdout
    assert "hyrule-feature/" in branches

    # Reporting fired on both channels.
    assert report.notifications == ["discord", "icinga"]
    assert "published" in discord[0]["content"]
    assert icinga[0]["exit_status"] == 0

    # The ledger recorded the run.
    ledger_files = list((tmp_path / "state").glob("ledger-*.json"))
    assert ledger_files and json.loads(ledger_files[0].read_text())["runs"] == 1


def test_idle_queue_reports_idle(tmp_path: Path) -> None:
    config = DaemonConfig(
        repos=("AS215932/network-operations",),
        state_dir=tmp_path / "state",
        output_root=tmp_path / "out",
    )
    report = daemon_once(config, client=FakeGh({"issue list": "[]"}))
    assert report.outcome == "idle"


def test_daemon_cli_per_run_budget_flags() -> None:
    parser = build_parser()
    # Defaults match the conservative DaemonConfig values.
    default_args = parser.parse_args(["daemon", "--once"])
    assert default_args.max_iterations_per_run == DaemonConfig.max_iterations_per_run
    assert default_args.max_wall_clock_minutes_per_run == DaemonConfig.max_wall_clock_minutes_per_run
    # Overridable for a one-off larger run.
    args = parser.parse_args(
        ["daemon", "--once", "--max-iterations-per-run", "40", "--max-wall-clock-minutes-per-run", "90"]
    )
    assert args.max_iterations_per_run == 40
    assert args.max_wall_clock_minutes_per_run == 90


def test_daemon_defaults_to_core_repos_and_low_and_slow_budget() -> None:
    config = DaemonConfig()
    assert config.repos == CORE_REPOS
    assert config.max_runs_per_day == 2
    assert config.max_cost_usd_per_day == 10.0
    assert config.allowed_paths == ("docs",)
    assert config.allowed_paths_by_repo == {}


def test_loop_budget_label_raises_only_the_per_issue_run_budget() -> None:
    item = IntakeItem(
        repo="AS215932/hyrule-cloud",
        number=12,
        title="Feature-sized work",
        url="u",
        labels=("loop:approved", "loop:budget-xl"),
        updated_at="",
        score=0.0,
        body_complete=True,
    )

    budget = backend_budget_for_issue(item, DaemonConfig())

    assert budget == {"max_iterations": 60, "max_wall_clock_minutes": 120, "max_cost_usd": 10.0}


def test_loop_budget_label_is_clamped_to_remaining_daily_cost() -> None:
    item = IntakeItem(
        repo="AS215932/hyrule-cloud",
        number=12,
        title="Feature-sized work",
        url="u",
        labels=("loop:approved", "loop:budget-xl"),
        updated_at="",
        score=0.0,
        body_complete=True,
    )

    budget = backend_budget_for_issue(item, DaemonConfig(), remaining_cost_usd=4.25)

    assert budget == {"max_iterations": 60, "max_wall_clock_minutes": 120, "max_cost_usd": 4.25}


def _capture_allowed_paths(tmp_path: Path, config_kwargs: dict[str, Any], repo: str = "AS215932/hyrule-cloud") -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def runner(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"final_state": {}, "state_path": str(tmp_path / "state.json")}

    config = DaemonConfig(repos=(repo,), state_dir=tmp_path / "state", output_root=tmp_path / "runs", **config_kwargs)
    gh = FakeGh({"issue list": _approved_issue_json(1, repo=repo, labels=["loop:approved"]), "issue view": json.dumps({"body": "x"})})
    daemon_once(config, client=gh, feature_runner=runner)
    return captured


def test_daemon_allowed_paths_default_is_docs_only(tmp_path: Path) -> None:
    captured = _capture_allowed_paths(tmp_path, {})
    assert captured["repo_name"] == "hyrule-cloud"
    assert captured["allowed_paths"] == ["docs"]


def test_daemon_allowed_paths_per_repo_override(tmp_path: Path) -> None:
    captured = _capture_allowed_paths(
        tmp_path, {"allowed_paths_by_repo": {"hyrule-cloud": ("hyrule_cloud", "tests", "docs")}}
    )
    assert captured["allowed_paths"] == ["hyrule_cloud", "tests", "docs"]


def test_daemon_allowed_paths_unlisted_repo_falls_back_to_docs(tmp_path: Path) -> None:
    # An override for one repo must not widen a different repo.
    captured = _capture_allowed_paths(
        tmp_path,
        {"allowed_paths_by_repo": {"hyrule-web": ("hyrule_web",)}},
        repo="AS215932/hyrule-cloud",
    )
    assert captured["allowed_paths"] == ["docs"]


def test_daemon_passes_issue_budget_override_to_feature_runner(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def runner(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"final_state": {}, "state_path": str(tmp_path / "state.json")}

    repo = "AS215932/hyrule-cloud"
    config = DaemonConfig(repos=(repo,), state_dir=tmp_path / "state", output_root=tmp_path / "runs")
    gh = FakeGh(
        {
            "issue list": _approved_issue_json(12, repo=repo, labels=["loop:approved", "loop:budget-xl"]),
            "issue view": json.dumps({"body": "x"}),
        }
    )

    daemon_once(config, client=gh, feature_runner=runner)

    assert captured["backend_budget"] == {
        "max_iterations": 60,
        "max_wall_clock_minutes": 120,
        "max_cost_usd": 10.0,
    }


def test_daemon_blocks_publication_when_reported_cost_exceeds_run_budget(tmp_path: Path) -> None:
    def runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "state_path": str(kwargs["output_root"] / "state" / f"{kwargs['change_id']}.json"),
            "signoff_status": "ready_for_review",
            "final_state": {
                "promotion_results": [{"repo": "hyrule-cloud", "branch": "b", "worktree_path": "w"}],
                "noc_handoff_path": "h",
                "backend_results": [{"cost": {"usd": 6.0}}],
                "reflection_results": {"written": True},
            },
        }

    published: list[dict[str, Any]] = []
    repo = "AS215932/hyrule-cloud"
    config = DaemonConfig(repos=(repo,), state_dir=tmp_path / "state", output_root=tmp_path / "runs")
    gh = FakeGh(
        {
            "issue list": _approved_issue_json(13, repo=repo, labels=["loop:approved"]),
            "issue view": json.dumps({"body": "x"}),
        }
    )

    report = daemon_once(
        config,
        client=gh,
        feature_runner=runner,
        publisher=lambda state, **kwargs: published.append(state) or [],
    )

    assert report.outcome == "needs_triage"
    assert "exceeded budget" in report.detail
    assert published == []


def test_daemon_clamps_issue_budget_to_remaining_daily_cost(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def runner(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"final_state": {}, "state_path": str(tmp_path / "state.json")}

    repo = "AS215932/hyrule-cloud"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    (state_dir / f"ledger-{day}.json").write_text(
        json.dumps({"runs": 1, "cost_usd": 6.0, "wall_clock_seconds": 10.0}),
        encoding="utf-8",
    )
    config = DaemonConfig(repos=(repo,), state_dir=state_dir, output_root=tmp_path / "runs")
    gh = FakeGh(
        {
            "issue list": _approved_issue_json(12, repo=repo, labels=["loop:approved", "loop:budget-xl"]),
            "issue view": json.dumps({"body": "x"}),
        }
    )

    daemon_once(config, client=gh, feature_runner=runner)

    assert captured["backend_budget"] == {
        "max_iterations": 60,
        "max_wall_clock_minutes": 120,
        "max_cost_usd": 4.0,
    }


def test_repo_name_for_issue_maps_core_repo_checkout_names() -> None:
    cases = {
        "AS215932/engineering-loop": "engineering-loop",
        "AS215932/network-operations": "hyrule-infra",
        "AS215932/hyrule-cloud": "hyrule-cloud",
        "AS215932/hyrule-web": "hyrule-web",
        "AS215932/hyrule-mcp": "hyrule-mcp",
        "AS215932/noc-agent": "hyrule-noc-agent",
        "AS215932/hyrule-network-proxy": "hyrule-network-proxy",
        "AS215932/as215932.net": "as215932.net",
    }
    for repo, checkout in cases.items():
        item = IntakeItem(
            repo=repo,
            number=1,
            title="t",
            url="u",
            labels=("loop:approved",),
            updated_at="",
            score=0.0,
            body_complete=True,
        )
        assert repo_name_for_issue(item) == checkout


# --- AC2: per-run budget exhaustion is journaled, next run unaffected -------


def _paused_run(**kwargs: Any) -> dict[str, Any]:
    return {
        "state_path": str(kwargs["output_root"] / "state" / f"{kwargs['change_id']}.json"),
        "signoff_status": "needs_operator_triage",
        "failure_summary": {"error_excerpt": "backend budget exhausted: wall clock"},
        "final_state": {
            "backend_results": [{"cost": {"usd": 0.0}}],
            "reflection_results": {
                "written": True,
                "journal_path": str(kwargs["output_root"] / "journal.md"),
            },
        },
    }


def _published_run(**kwargs: Any) -> dict[str, Any]:
    return {
        "state_path": str(kwargs["output_root"] / "state" / f"{kwargs['change_id']}.json"),
        "signoff_status": "ready_for_review",
        "final_state": {
            "promotion_results": [{"repo": "hyrule-cloud", "branch": "b", "worktree_path": "w"}],
            "noc_handoff_path": "h",
            "backend_results": [{"cost": {"usd": 0.0}}],
            "reflection_results": {"written": True},
        },
    }


def test_budget_exhaustion_journals_and_next_run_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = "AS215932/hyrule-cloud"
    gh = FakeGh(
        {
            "issue list": _approved_issue_json(7, repo=repo, labels=["loop:approved"]),
            "issue view": json.dumps({"body": "## Context\nx\n"}),
        }
    )
    monkeypatch.setenv("HYRULE_DISCORD_WEBHOOK", "https://discord.invalid/webhook")
    config = DaemonConfig(
        repos=(repo,),
        workspace_root=tmp_path / "workspace",
        output_root=tmp_path / "runs",
        state_dir=tmp_path / "state",
    )
    (tmp_path / "workspace").mkdir()

    discord: list[dict[str, Any]] = []
    report1 = daemon_once(
        config,
        client=gh,
        feature_runner=_paused_run,
        discord_poster=lambda url, payload: discord.append(payload),
    )

    assert report1.outcome == "needs_triage"
    assert report1.pr_url is None
    assert "budget exhausted" in report1.detail
    assert report1.notifications == ["discord"]
    assert "needs_triage" in discord[0]["content"]

    published: list[dict[str, Any]] = []
    report2 = daemon_once(
        config,
        client=gh,
        feature_runner=_published_run,
        publisher=lambda state, **kw: [{"github_pr": {"url": "https://github.invalid/pr/9"}}],
        discord_poster=lambda url, payload: published.append(payload),
    )

    assert report2.outcome == "published"
    assert report2.pr_url == "https://github.invalid/pr/9"

    ledger = json.loads(next((tmp_path / "state").glob("ledger-*.json")).read_text())
    assert ledger["runs"] == 2


def test_daily_run_budget_stops_further_runs(tmp_path: Path) -> None:
    repo = "AS215932/hyrule-cloud"
    config = DaemonConfig(
        repos=(repo,),
        state_dir=tmp_path / "state",
        output_root=tmp_path / "out",
        max_runs_per_day=0,
    )
    report = daemon_once(config, client=FakeGh({"issue list": "[]"}))
    assert report.outcome == "over_budget"
    assert "run budget" in report.detail


# --- kill criterion: stall detection ----------------------------------------


def test_unchanged_diff_across_rounds_aborts_to_signoff(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_repo(workspace / "hyrule-cloud")

    base: GraphState = cast(
        GraphState,
        {
            "change_id": "STALL_TEST",
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
            "rollback_plan": "",
            "noc_handoff_metadata": {},
            "requires_human_signoff": False,
            "promotion_enabled": True,
            "promotion_repositories": {"hyrule-cloud": str(workspace / "hyrule-cloud")},
            "promotion_allowed_paths": {"hyrule-cloud": ["docs"]},
            "promotion_worktree_root": str(tmp_path / "worktrees"),
            "promotion_branch_prefix": "hyrule-feature",
            "feature_request": "stall",
            "llm_mock_responses": {
                "implementation_writer": {
                    "approved": True,
                    "proposed_mutations": [
                        {
                            "path": "hyrule-cloud:docs/stall.md",
                            "content": "# Stall\n",
                            "operation": "create",
                        }
                    ],
                }
            },
        },
    )
    worktrees = setup_worktrees_for_state(base)
    base["worktree_results"] = worktrees

    fingerprint: str | None = None
    stall_rounds = 0
    statuses: list[str] = []
    for _ in range(STALL_ROUND_LIMIT + 1):
        state = cast(GraphState, dict(base))
        if fingerprint is not None:
            state["last_diff_fingerprint"] = fingerprint
            state["stall_rounds"] = stall_rounds
        update = delegate_implementation_node(state)
        statuses.append(update["implementation_writer_status"])
        fingerprint = update["last_diff_fingerprint"]
        stall_rounds = update["stall_rounds"]

    # First few rounds proceed; the run aborts once the diff has been
    # unchanged for STALL_ROUND_LIMIT consecutive rounds.
    assert statuses[-1] == "stalled"
    assert statuses[:STALL_ROUND_LIMIT] == ["complete"] * STALL_ROUND_LIMIT
    rollback_promotions(worktrees)


# --- worktree self-heal -----------------------------------------------------


def test_setup_worktrees_self_heals_stale_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_repo(workspace / "hyrule-cloud")

    def _fresh_state() -> GraphState:
        return cast(
            GraphState,
            {
                "change_id": "STALE_TEST",
                "promotion_enabled": True,
                "promotion_repositories": {"hyrule-cloud": str(workspace / "hyrule-cloud")},
                "promotion_worktree_root": str(tmp_path / "worktrees"),
                "promotion_branch_prefix": "hyrule-feature",
            },
        )

    # First setup creates the branch-backed worktree.
    first = setup_worktrees_for_state(_fresh_state())
    worktree_path = Path(first[0]["worktree_path"])
    assert worktree_path.is_dir()

    # A crashed run leaves the worktree + branch on disk. A brand-new state
    # (no recorded worktree_results) must self-heal and recreate rather than
    # raising "worktree path already exists", which would wedge every retry.
    second = setup_worktrees_for_state(_fresh_state())
    assert Path(second[0]["worktree_path"]).is_dir()
    assert second[0]["branch"] == first[0]["branch"]
    rollback_promotions(second)


# --- reporting helpers ------------------------------------------------------


def test_classify_issue_maps_labels() -> None:
    item = IntakeItem(
        repo="r", number=1, title="t", url="u",
        labels=("firewall", "critical"), updated_at="", score=0.0, body_complete=True,
    )
    change_class, risk = classify_issue(item)
    assert change_class == "firewall_policy"
    assert risk == "high"


def test_notifications_skip_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("HYRULE_DISCORD_WEBHOOK", "HYRULE_ICINGA_URL"):
        monkeypatch.delenv(key, raising=False)
    report = DaemonReport(outcome="idle")
    assert notify_discord(report) is False
    assert notify_icinga(report) is False


def test_default_http_post_sets_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(request: Any, **kwargs: Any) -> _Response:
        seen["headers"] = dict(request.header_items())
        seen["kwargs"] = kwargs
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _default_http_post("https://discord.example/webhook", {"content": "hello"})

    assert seen["headers"]["User-agent"] == "AS215932-Engineering-Loop/1.0"
    assert seen["kwargs"] == {"timeout": 20}


def test_notify_icinga_requests_relaxed_x509_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYRULE_ICINGA_URL", "https://mon.as215932.net:5665")
    monkeypatch.setenv("HYRULE_ICINGA_USER", "icinga-user")
    monkeypatch.setenv("HYRULE_ICINGA_PASSWORD", "icinga-password")
    captured: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any]) -> None:
        captured["url"] = url
        captured["payload"] = payload

    assert notify_icinga(DaemonReport(outcome="idle"), poster=fake_post) is True

    assert captured["url"] == "https://mon.as215932.net:5665/v1/actions/process-check-result"
    assert captured["payload"]["_relax_x509_strict"] is True
