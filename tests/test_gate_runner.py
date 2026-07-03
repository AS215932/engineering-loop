from __future__ import annotations

from hyrule_engineering_loop.gate_runner import run_gate_commands, select_gate_commands_for_mutations


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
    assert errors
    assert "UnicodeDecodeError" in errors[0]["stderr"]


def test_python_gate_uses_uv_dev_extra_when_available(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo"',
                'version = "0.1.0"',
                "[project.optional-dependencies]",
                'dev = ["ruff", "mypy", "pytest"]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    commands = select_gate_commands_for_mutations(["src/app.py"], cwd=tmp_path)

    assert commands == [
        ["uv", "run", "--extra", "dev", "ruff", "check", "."],
        ["uv", "run", "--extra", "dev", "mypy", "."],
        ["uv", "run", "--extra", "dev", "python", "-m", "pytest", "-q"],
    ]


def test_gate_reports_missing_tool_as_failure() -> None:
    results, errors = run_gate_commands([["definitely-missing-hyrule-gate-tool"]])

    assert results[0]["returncode"] == 127
    assert results[0]["status"] == "failed"
    assert errors
    assert "could not start" in errors[0]["message"]
