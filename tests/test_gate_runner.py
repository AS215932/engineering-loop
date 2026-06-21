from __future__ import annotations

import os

import pytest

from hyrule_engineering_loop.gate_runner import run_gate_commands, select_gate_commands_for_mutations
from hyrule_engineering_loop.trace import compact_update


def test_docs_gate_reads_only_mutated_text_paths(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xb8\x00not utf8")

    commands = select_gate_commands_for_mutations(["engineering-loop:docs/note.md"])
    results, errors = run_gate_commands(commands, cwd=tmp_path)

    assert errors == []
    assert results[0]["returncode"] == 0


def test_docs_gate_reports_non_utf8_mutated_file(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_bytes(b"\xb8\x00not utf8")

    commands = select_gate_commands_for_mutations(["docs/note.md"])
    results, errors = run_gate_commands(commands, cwd=tmp_path)

    assert results[0]["returncode"] == 1
    assert results[0]["status"] == "failed"
    assert errors
    assert "UnicodeDecodeError" in errors[0]["stderr"]


def test_missing_gate_binary_is_a_structured_failure(tmp_path) -> None:
    results, errors = run_gate_commands([["definitely-not-a-real-loop-gate"]], cwd=tmp_path)

    assert results[0]["returncode"] == 127
    assert results[0]["status"] == "failed"
    assert errors[0]["domain"] == "ci"
    assert "command not found" in errors[0]["stderr"]


def test_python_gate_uses_uv_dev_group(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['ruff']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands([["ruff", "check", "."]], cwd=repo)

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--group",
        "dev",
        "ruff",
        "check",
        ".",
    ]
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--locked",
        "--group",
        "dev",
        "ruff",
        "check",
        ".",
    ]


def test_uv_gate_uses_optional_dev_extra(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0'\n[project.optional-dependencies]\ndev = ['mypy']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands([["uv", "run", "mypy", "."]], cwd=repo)

    assert errors == []
    assert results[0]["executed_command"] == ["uv", "run", "--locked", "--extra", "dev", "mypy", "."]


def test_explicit_uv_gate_without_dev_env_still_gets_lock_guard(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0'\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands([["uv", "run", "python", "-c", "pass"]], cwd=tmp_path)

    assert errors == []
    assert results[0]["executed_command"] == ["uv", "run", "--locked", "python", "-c", "pass"]


def test_uv_value_option_does_not_hide_python_gate_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "--package", "api", "pytest", "-q"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--group",
        "dev",
        "--package",
        "api",
        "pytest",
        "-q",
    ]


def test_uv_only_group_dev_satisfies_dev_selector(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "--only-group", "dev", "pytest", "-q"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--only-group",
        "dev",
        "pytest",
        "-q",
    ]


def test_uv_only_dev_satisfies_dev_selector(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "--only-dev", "pytest", "-q"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--only-dev",
        "pytest",
        "-q",
    ]


def test_uv_non_dev_extra_does_not_suppress_dev_selector(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0'\n[project.optional-dependencies]\ndev = ['pytest']\ndocs = ['mkdocs']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "--extra", "docs", "pytest", "-q"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--extra",
        "dev",
        "--extra",
        "docs",
        "pytest",
        "-q",
    ]


def test_uv_no_argument_flag_does_not_hide_python_gate_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['pytest']\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "--managed-python", "pytest", "-q"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--group",
        "dev",
        "--managed-python",
        "pytest",
        "-q",
    ]


def test_uv_payload_lock_like_arg_does_not_count_as_lock_guard(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "python", "tools/check.py", "--locked"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "python",
        "tools/check.py",
        "--locked",
    ]


def test_uv_double_dash_payload_lock_like_arg_does_not_count_as_lock_guard(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands(
        [["uv", "run", "--", "python", "tools/check.py", "--frozen"]],
        cwd=tmp_path,
    )

    assert errors == []
    assert results[0]["executed_command"] == [
        "uv",
        "run",
        "--locked",
        "--",
        "python",
        "tools/check.py",
        "--frozen",
    ]


def test_uv_gate_preserves_explicit_frozen_guard(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv-args.txt"
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" > \"$UV_ARG_LOG\"\n", encoding="utf-8")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_ARG_LOG", str(log_path))

    results, errors = run_gate_commands([["uv", "run", "--frozen", "python", "-c", "pass"]], cwd=tmp_path)

    assert errors == []
    assert results[0]["executed_command"] == ["uv", "run", "--frozen", "python", "-c", "pass"]


def test_python_mutations_select_repo_quality_gates_when_dev_env_exists(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = ['pytest', 'ruff', 'mypy']\n",
        encoding="utf-8",
    )

    commands = select_gate_commands_for_mutations(["hyrule_cloud/api.py"], cwd=tmp_path)

    assert commands == [
        ["uv", "run", "python", "-m", "pytest", "-q"],
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "mypy", "hyrule_cloud"],
    ]


def test_gate_output_is_visible_in_compact_trace() -> None:
    summary = compact_update(
        {
            "gate_results": [
                {
                    "command": ["ruff", "check", "."],
                    "executed_command": ["uv", "run", "--locked", "--group", "dev", "ruff", "check", "."],
                    "returncode": 1,
                    "status": "failed",
                    "stdout": "stdout detail",
                    "stderr": "stderr detail",
                }
            ]
        }
    )

    assert summary["gate_results"][0]["stdout"] == "stdout detail"
    assert summary["gate_results"][0]["stderr"] == "stderr detail"
