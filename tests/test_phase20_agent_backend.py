"""Phase B (v2): AgentBackend, worktree-first execution, diff policy guard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from hyrule_engineering_loop.backend import (
    BackendConstraints,
    ClaudeCodeBackend,
    PI_PROVIDER_ENV_NAMES,
    PiBackend,
    TaskSpec,
    assemble_backend_prompt,
    env_hygiene_violations,
    scrubbed_backend_env,
)
from hyrule_engineering_loop.cli import main
from hyrule_engineering_loop.feature import build_feature_state
from hyrule_engineering_loop.graph import build_graph
from hyrule_engineering_loop.nodes import delegate_implementation_node
from hyrule_engineering_loop.model_policy import select_backend_for_state, validate_model_policy
from hyrule_engineering_loop.promotion import rollback_promotions, setup_worktrees_for_state
from hyrule_engineering_loop.state import GraphState


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, check=False, text=True)
    assert completed.returncode == 0, completed.stderr


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run(["git", "init"], path)
    _run(["git", "config", "user.email", "loop@example.invalid"], path)
    _run(["git", "config", "user.name", "Engineering Loop"], path)
    (path / "README.md").write_text(f"{path.name}\n", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "initial"], path)


def _summary_from_stdout(output: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(output[output.index("{") :]))


def _feature_state(tmp_path: Path, change_id: str, *, allow: list[str]) -> GraphState:
    workspace_root = tmp_path / "workspace"
    if not (workspace_root / "hyrule-cloud").exists():
        _init_repo(workspace_root / "hyrule-cloud")
    request_path = tmp_path / "request.md"
    request_path.write_text("Phase 20 backend test request.\n", encoding="utf-8")
    return build_feature_state(
        change_id=change_id,
        change_class="app_feature",
        workspace_root=workspace_root,
        output_root=tmp_path / "feature-output",
        repo_name="hyrule-cloud",
        request_path=request_path,
        allowed_paths=allow,
        source_files=["README.md"],
        scaffold_plan=False,
    )


def test_env_hygiene_scrubs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_TOKEN", "supersecret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("HYRULE_LLM_API_KEY", "sk-nope")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-nope")

    env = scrubbed_backend_env()

    assert "VAULT_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "HYRULE_LLM_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "PATH" in env
    assert env_hygiene_violations(env) == []


def test_pi_backend_allows_only_model_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("GH_TOKEN", "github-token")
    monkeypatch.setenv("VAULT_TOKEN", "vault-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")

    env = scrubbed_backend_env(allow_names=PiBackend.extra_env_names)

    for key in PI_PROVIDER_ENV_NAMES:
        assert env[key]
    assert "GH_TOKEN" not in env
    assert "VAULT_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env_hygiene_violations(
        env, allowed_secret_names=PiBackend.extra_env_names
    ) == []
    assert set(env_hygiene_violations(env)) == PI_PROVIDER_ENV_NAMES


def test_prompt_includes_full_request_body_not_just_intent() -> None:
    # The planner's intent is a truncated one-liner (often just the title); the
    # backend must still see the full issue body (action items + constraints).
    spec = TaskSpec(
        change_id="FULL_BODY",
        change_class="app_feature",
        risk_level="low",
        request="# Title\n\n## Action items\n1. Add field payment_status.\n\n## Constraints\nNo generic payment engine.",
        allowed_paths={"hyrule-cloud": ("hyrule_cloud",)},
        intent="feat: title only",
    )
    prompt = assemble_backend_prompt(spec, BackendConstraints(max_iterations=5))
    assert "Add field payment_status." in prompt
    assert "No generic payment engine." in prompt


def test_subprocess_backend_command_assembly_and_refusals(tmp_path: Path) -> None:
    spec = TaskSpec(
        change_id="CMD_ASSEMBLY",
        change_class="app_feature",
        risk_level="low",
        request="do the thing",
        allowed_paths={"hyrule-cloud": ("docs",)},
    )
    constraints = BackendConstraints(max_iterations=7)

    command = ClaudeCodeBackend().build_command(
        prompt=assemble_backend_prompt(spec, constraints), constraints=constraints
    )
    assert command[0] == "claude"
    assert "-p" in command
    assert "--output-format" in command and "json" in command
    assert command[command.index("--max-turns") + 1] == "7"
    assert "CMD_ASSEMBLY" in command[command.index("-p") + 1]

    pi_command = PiBackend().build_command(
        prompt=assemble_backend_prompt(spec, constraints), constraints=constraints
    )
    assert pi_command[:4] == ["pi", "--print", "--mode", "json"]

    refused = PiBackend().execute(task_spec=spec, worktree=None, constraints=constraints)
    assert refused.status == "failed"
    assert "requires a branch-backed worktree" in str(refused.error)

    read_only_command = ClaudeCodeBackend().build_command(
        prompt="x", constraints=BackendConstraints(read_only=True)
    )
    assert "plan" in read_only_command
    assert "acceptEdits" not in read_only_command


def test_pi_backend_parses_single_json_error_event() -> None:
    stdout = json.dumps(
        {
            "type": "agent_error",
            "error": {"message": "provider refused the request"},
            "willRetry": False,
        }
    )

    parsed = PiBackend()._parse_harness_output(stdout)

    assert parsed["num_turns"] == 1
    assert parsed["is_error"] is True


def test_pi_backend_treats_assistant_error_stop_reason_as_error() -> None:
    stdout = "\n".join(
        json.dumps(event)
        for event in [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "provider failed"}],
                    "stopReason": "error",
                    "errorMessage": "upstream provider failed",
                    "usage": {
                        "input": 10,
                        "output": 2,
                        "cost": {"total": 0.01},
                    },
                },
            },
            {"type": "turn_end", "message": {"role": "assistant", "content": []}},
        ]
    )

    parsed = PiBackend()._parse_harness_output(stdout)

    assert parsed["is_error"] is True
    assert parsed["result"] == "provider failed"
    assert parsed["total_cost_usd"] == 0.01


def test_pi_backend_parses_json_event_usage_and_cost() -> None:
    stdout = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "agent_start"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "draft complete"}],
                    "usage": {
                        "input": 1323,
                        "output": 5,
                        "cost": {"input": 0.006615, "output": 0.00015, "total": 0.006765},
                    },
                },
            },
            {"type": "turn_end", "message": {"role": "assistant", "content": []}},
            {"type": "agent_end", "willRetry": False},
        ]
    )

    parsed = PiBackend()._parse_harness_output(stdout)

    assert parsed["num_turns"] == 1
    assert parsed["usage"] == {"input_tokens": 1323, "output_tokens": 5}
    assert parsed["total_cost_usd"] == 0.006765
    assert parsed["result"] == "draft complete"
    assert parsed["is_error"] is False


def test_backend_selection_follows_tier_escalation(tmp_path: Path) -> None:
    policy_path = tmp_path / "model-policy.yml"
    policy_path.write_text(
        "\n".join(
            [
                "version: 1",
                "defaults: {provider: openrouter, model: m, tier: cheap}",
                "roles:",
                "  implementation_writer: {provider: openrouter, model: m, tier: mid}",
                "risk_overrides:",
                "  high: {min_tier: strong}",
                "tier_fallbacks:",
                "  strong: {provider: anthropic, model: claude-sonnet-4-6}",
                "backends:",
                "  default: mock",
                "  tiers: {strong: claude-code}",
                "  definitions:",
                "    claude-code:",
                "      command: [claude, -p, '{prompt}']",
                "",
            ]
        ),
        encoding="utf-8",
    )

    def _state(risk: str) -> GraphState:
        return cast(
            GraphState,
            {
                "change_id": "BACKEND_SELECT",
                "change_class": "app_feature",
                "risk_level": risk,
                "customer_impact": "none",
                "source_of_truth_files": [],
                "proposed_mutations": {},
                "mcp_schema_breaking": False,
                "emulated_lab_verified": "not_applicable",
                "validation_errors": [],
                "role_approvals": {},
                "retry_counters": {},
                "rollback_plan": "",
                "noc_handoff_metadata": {},
                "requires_human_signoff": False,
                "model_policy_file": str(policy_path),
            },
        )

    low = select_backend_for_state(_state("low"))
    assert low.name == "mock"
    assert low.tier == "mid"

    high = select_backend_for_state(_state("high"))
    assert high.name == "claude-code"
    assert high.tier == "strong"
    assert high.command == ["claude", "-p", "{prompt}"]

    bad_policy = tmp_path / "bad-policy.yml"
    bad_policy.write_text("version: 1\nbackends: {default: warp}\n", encoding="utf-8")
    result = validate_model_policy(bad_policy)
    assert result["ok"] is False
    assert any("unknown default backend" in error for error in result["errors"])


def test_subprocess_backend_enforces_reported_cost_budget(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    backend = ClaudeCodeBackend(
        command=[
            sys.executable,
            "-c",
            "import json; print(json.dumps({'num_turns': 2, 'total_cost_usd': 2.5, 'usage': {'input_tokens': 10, 'output_tokens': 20}, 'result': 'done'}))",
        ]
    )
    spec = TaskSpec(
        change_id="COST_BUDGET",
        change_class="app_feature",
        risk_level="low",
        request="exercise cost budget",
        allowed_paths={"repo": ("docs",)},
    )

    result = backend.execute(
        task_spec=spec,
        worktree=repo,
        constraints=BackendConstraints(max_cost_usd=1.0),
    )

    assert result.status == "budget_exhausted"
    assert result.cost.usd == 2.5
    assert "exceeded run budget" in result.notes


def test_budget_exhaustion_routes_to_human_signoff(tmp_path: Path) -> None:
    state = _feature_state(tmp_path, "BUDGET_EXHAUSTED", allow=["docs"])
    state["backend_budget"] = {"max_iterations": 0}

    final_state = dict(build_graph().invoke(state))

    assert final_state["implementation_writer_status"] == "budget_exhausted"
    assert final_state["requires_human_signoff"] is True
    assert final_state["signoff_status"] == "needs_operator_triage"
    assert any(
        "budget exhausted" in str(error.get("message", ""))
        for error in final_state["validation_errors"]
    )
    backend_runs = final_state["backend_results"]
    assert backend_runs[0]["status"] == "budget_exhausted"
    nodes = [event["node"] for event in final_state["trace_events"]]
    assert "delegate_implementation" in nodes
    assert "gate_execution" not in nodes

    rollback_promotions(final_state["worktree_results"])


def test_policy_guard_rejects_diff_outside_allowed_paths(tmp_path: Path) -> None:
    state = _feature_state(tmp_path, "GUARD_SCOPE", allow=["docs"])
    state["llm_mock_responses"] = {
        "implementation_writer": {
            "approved": True,
            "proposed_mutations": [
                {
                    "path": "hyrule-cloud:src/evil.py",
                    "content": "print('out of scope')\n",
                    "operation": "create",
                }
            ],
        }
    }

    final_state = dict(build_graph().invoke(state))

    assert final_state["policy_status"] == "failed"
    assert final_state["requires_human_signoff"] is True
    assert "promotion_status" not in final_state
    assert any(
        "worktree path not allowlisted for hyrule-cloud: src/evil.py" in str(error.get("message"))
        for error in final_state["validation_errors"]
    )

    rollback_promotions(final_state["worktree_results"])


def test_policy_guard_rejects_secret_bearing_diff(tmp_path: Path) -> None:
    state = _feature_state(tmp_path, "GUARD_SECRET", allow=["docs"])
    state["llm_mock_responses"] = {
        "implementation_writer": {
            "approved": True,
            "proposed_mutations": [
                {
                    "path": "hyrule-cloud:docs/leak.md",
                    "content": 'api_key = "definitely-not-a-secret"\n',
                    "operation": "create",
                }
            ],
        }
    }

    final_state = dict(build_graph().invoke(state))

    assert final_state["policy_status"] == "failed"
    assert any(
        "denied by pattern" in str(error.get("message"))
        for error in final_state["validation_errors"]
    )

    rollback_promotions(final_state["worktree_results"])


def test_policy_guard_enforces_changed_file_cap(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.yml"
    policy_path.write_text(
        "\n".join(
            [
                "version: 1",
                "defaults:",
                "  max_changed_files: 1",
                "  max_file_bytes: 1048576",
                "  denied_path_globs: []",
                "  denied_content_patterns: []",
                "  allowed_gate_commands: [python, python3]",
                "repos: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state = _feature_state(tmp_path, "GUARD_CAP", allow=["docs"])
    state["policy_file"] = str(policy_path)
    state["llm_mock_responses"] = {
        "implementation_writer": {
            "approved": True,
            "proposed_mutations": [
                {"path": "hyrule-cloud:docs/a.md", "content": "a\n", "operation": "create"},
                {"path": "hyrule-cloud:docs/b.md", "content": "b\n", "operation": "create"},
            ],
        }
    }

    final_state = dict(build_graph().invoke(state))

    assert final_state["policy_status"] == "failed"
    assert any(
        "changed file count exceeds policy limit" in str(error.get("message"))
        for error in final_state["validation_errors"]
    )

    rollback_promotions(final_state["worktree_results"])


def test_auto_gate_selection_is_per_worktree(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _init_repo(workspace_root / "docs-repo")
    _init_repo(workspace_root / "python-repo")
    (workspace_root / "python-repo" / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['pytest', 'ruff', 'mypy']\n",
        encoding="utf-8",
    )
    (workspace_root / "python-repo" / "python_repo").mkdir()
    (workspace_root / "python-repo" / "python_repo" / "__init__.py").write_text("", encoding="utf-8")
    _run(["git", "add", "pyproject.toml", "python_repo/__init__.py"], workspace_root / "python-repo")
    _run(["git", "commit", "-m", "add python project"], workspace_root / "python-repo")

    state = cast(
        GraphState,
        {
            "change_id": "MULTI_REPO_GATES",
            "change_class": "app_feature",
            "risk_level": "low",
            "customer_impact": "none",
            "source_of_truth_files": [],
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
            "promotion_repositories": {
                "docs-repo": str(workspace_root / "docs-repo"),
                "python-repo": str(workspace_root / "python-repo"),
            },
            "promotion_allowed_paths": {
                "docs-repo": ["docs"],
                "python-repo": ["python_repo"],
            },
            "promotion_worktree_root": str(tmp_path / "worktrees"),
            "promotion_branch_prefix": "hyrule-feature",
            "feature_request": "exercise per-worktree gate selection",
            "llm_mock_responses": {
                "implementation_writer": {
                    "approved": True,
                    "proposed_mutations": [
                        {
                            "path": "python-repo:python_repo/change.py",
                            "content": "VALUE = 1\n",
                            "operation": "create",
                        }
                    ],
                }
            },
        },
    )
    worktrees = setup_worktrees_for_state(state)
    state["worktree_results"] = worktrees

    update = delegate_implementation_node(state)

    assert "gate_commands" not in update
    assert update["gate_commands_by_repo"] == {
        "python-repo": [
            ["uv", "run", "python", "-m", "pytest", "-q"],
            ["uv", "run", "ruff", "check", "."],
            ["uv", "run", "mypy", "python_repo"],
        ]
    }
    rollback_promotions(worktrees)


def test_backend_canary_dry_live_assembles_without_execution(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "workspace"
    _init_repo(workspace_root / "hyrule-cloud")

    assert (
        main(
            [
                "backend-canary",
                "--workspace-root",
                str(workspace_root),
                "--repo-name",
                "hyrule-cloud",
                "--output-root",
                str(tmp_path / "canary-output"),
                "--dry-live",
            ]
        )
        == 0
    )

    payload = _summary_from_stdout(capsys.readouterr().out)
    assert payload["dry_live"] is True
    assert payload["provider_called"] is False

    preflight = cast(dict[str, Any], payload["preflight"])
    backend = cast(dict[str, Any], preflight["backend"])
    assert cast(dict[str, Any], backend["selection"])["name"] == "mock"
    assert int(backend["prompt_chars"]) > 0
    assert any(
        check["name"] == "backend_env_hygiene" and check["ok"]
        for check in cast(list[dict[str, Any]], preflight["checks"])
    )
