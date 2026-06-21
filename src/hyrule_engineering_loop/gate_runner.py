"""Local command gate execution for the engineering loop."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable, Sequence

MAX_OUTPUT_CHARS = 8_000
PYTHON_GATE_TOOLS = frozenset({"pytest", "ruff", "mypy"})
UV_DEV_SELECTORS = frozenset({"--group", "--extra", "--all-groups", "--all-extras"})
UV_LOCK_GUARDS = frozenset({"--locked", "--frozen"})
UV_OPTIONS_WITH_VALUE = frozenset(
    {
        "--config-file",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--extra",
        "--group",
        "--index",
        "--index-url",
        "--isolated",
        "--keyring-provider",
        "--managed-python",
        "--no-group",
        "--project",
        "--python",
        "--resolution",
        "--with",
        "--with-editable",
    }
)


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n[output truncated]"


def _as_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _path_name(value: str) -> str:
    return Path(value).name


def _is_python_executable(value: str) -> bool:
    name = _path_name(value)
    return name == "python" or name.startswith("python3")


def _is_python_gate_payload(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    name = _path_name(argv[0])
    if name in PYTHON_GATE_TOOLS:
        return True
    return _is_python_executable(name) and len(argv) >= 3 and argv[1] == "-m" and argv[2] in PYTHON_GATE_TOOLS


def _uv_run_has_dependency_selector(argv: Sequence[str]) -> bool:
    return any(arg in UV_DEV_SELECTORS or arg.startswith(("--group=", "--extra=")) for arg in argv)


def _uv_run_has_lock_guard(argv: Sequence[str]) -> bool:
    return any(arg in UV_LOCK_GUARDS for arg in argv)


def _with_uv_lock_guard(argv: Sequence[str]) -> list[str]:
    rendered = list(argv)
    if len(rendered) >= 2 and _path_name(rendered[0]) == "uv" and rendered[1] == "run" and not _uv_run_has_lock_guard(rendered):
        return [rendered[0], rendered[1], "--locked", *rendered[2:]]
    return rendered


def _uv_run_payload(argv_after_run: Sequence[str]) -> list[str]:
    index = 0
    argv = list(argv_after_run)
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        if option == "--":
            return argv[index + 1 :]
        if option in UV_LOCK_GUARDS:
            index += 1
        elif "=" in option:
            index += 1
        elif option in UV_OPTIONS_WITH_VALUE:
            index += 2
        else:
            index += 1
    return argv[index:]


def _uv_dev_args(cwd: Path | None) -> tuple[str, str] | tuple[()]:
    if cwd is None:
        return ()
    pyproject = cwd / "pyproject.toml"
    if not pyproject.is_file():
        return ()
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()

    dependency_groups = data.get("dependency-groups")
    if isinstance(dependency_groups, dict) and "dev" in dependency_groups:
        return ("--group", "dev")

    project = data.get("project")
    optional_dependencies = (
        project.get("optional-dependencies") if isinstance(project, dict) else None
    )
    if isinstance(optional_dependencies, dict) and "dev" in optional_dependencies:
        return ("--extra", "dev")
    return ()


def prepare_gate_command(command: Sequence[str], *, cwd: Path | str | None = None) -> list[str]:
    """Return the argv to execute, adding the target repo's dev env when needed.

    Python quality gates in AS215932 repos commonly live in a ``dev``
    dependency group or optional extra. Running ``ruff``/``mypy``/``pytest``
    bare can silently test the loop host instead of the target repo. When the
    worktree declares a dev dependency set, execute those gates via ``uv run``
    with the matching selector.
    """
    argv = list(command)
    cwd_path = Path(cwd).expanduser().resolve() if cwd is not None else None
    dev_args = _uv_dev_args(cwd_path)

    name = _path_name(argv[0]) if argv else ""
    if name == "uv" and len(argv) >= 2 and argv[1] == "run":
        locked = _with_uv_lock_guard(argv)
        if not dev_args:
            return locked
        if _uv_run_has_dependency_selector(locked):
            return locked
        if _is_python_gate_payload(_uv_run_payload(locked[2:])):
            return _with_uv_lock_guard([*argv[:2], *dev_args, *argv[2:]])
        return locked

    if not dev_args:
        return argv
    if _is_python_gate_payload(argv):
        return ["uv", "run", "--locked", *dev_args, *argv]
    return argv


def run_gate_commands(
    commands: Iterable[Sequence[str]],
    *,
    cwd: Path | str | None = None,
    timeout_seconds: int = 120,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run explicit local validation commands and return results plus errors.

    Commands are executed without a shell. This helper is intentionally generic:
    policy about which commands are safe belongs in the graph state and operator
    workflow, not in hidden defaults.
    """
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for command in commands:
        argv = list(command)
        if not argv:
            raise ValueError("gate command cannot be empty")
        prepared = prepare_gate_command(argv, cwd=cwd)

        try:
            completed = subprocess.run(
                prepared,
                cwd=cwd,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
            result = {
                "command": argv,
                "executed_command": prepared,
                "returncode": completed.returncode,
                "status": "passed" if completed.returncode == 0 else "failed",
                "stdout": _clip(completed.stdout),
                "stderr": _clip(completed.stderr),
            }
        except FileNotFoundError as exc:
            result = {
                "command": argv,
                "executed_command": prepared,
                "returncode": 127,
                "status": "failed",
                "stdout": "",
                "stderr": _clip(f"command not found: {prepared[0]} ({exc})"),
            }
        except PermissionError as exc:
            result = {
                "command": argv,
                "executed_command": prepared,
                "returncode": 126,
                "status": "failed",
                "stdout": "",
                "stderr": _clip(f"command is not executable: {prepared[0]} ({exc})"),
            }
        except OSError as exc:
            result = {
                "command": argv,
                "executed_command": prepared,
                "returncode": 126,
                "status": "failed",
                "stdout": "",
                "stderr": _clip(f"command could not start: {' '.join(prepared)} ({exc})"),
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                "command": argv,
                "executed_command": prepared,
                "returncode": 124,
                "status": "failed",
                "stdout": _clip(_as_text(exc.stdout)),
                "stderr": _clip(_as_text(exc.stderr) or f"timed out after {timeout_seconds}s"),
            }

        results.append(result)
        if result["returncode"] != 0:
            stderr = str(result.get("stderr", ""))
            stdout = str(result.get("stdout", ""))
            errors.append(
                {
                    "node": "gate_execution",
                    "domain": "ci",
                    "message": f"command failed: {' '.join(prepared)}",
                    "command": argv,
                    "executed_command": prepared,
                    "returncode": result["returncode"],
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )

    return results, errors


def _mypy_targets(paths: Sequence[str]) -> list[str]:
    top_level = sorted(
        {
            path.split("/", 1)[0]
            for path in paths
            if path.endswith(".py") and "/" in path and path.split("/", 1)[0] not in {"tests", "test"}
        }
    )
    if len(top_level) == 1:
        return [top_level[0]]
    return ["."]


def select_gate_commands_for_mutations(
    paths: Iterable[str],
    *,
    cwd: Path | str | None = None,
) -> list[list[str]]:
    """Select local, workspace-safe gates from proposed mutation paths."""
    normalized = [path.split(":", 1)[1] if ":" in path else path for path in paths]
    if not normalized:
        return []
    if any(path.endswith(".py") for path in normalized):
        cwd_path = Path(cwd).expanduser().resolve() if cwd is not None else None
        if _uv_dev_args(cwd_path):
            return [
                ["uv", "run", "python", "-m", "pytest", "-q"],
                ["uv", "run", "ruff", "check", "."],
                ["uv", "run", "mypy", *_mypy_targets(normalized)],
            ]
        return [[sys.executable, "-m", "compileall", "-q", "."]]
    if all(path.startswith("docs/") or path.endswith((".md", ".txt", ".rst")) for path in normalized):
        paths_literal = repr(json.dumps(normalized))
        script = (
            "import json\n"
            "from pathlib import Path\n"
            f"for raw in json.loads({paths_literal}):\n"
            "    path = Path(raw)\n"
            "    if not path.exists():\n"
            "        continue\n"
            "    if not path.is_file():\n"
            "        raise SystemExit(f'not a file: {raw}')\n"
            "    path.read_text(encoding='utf-8')\n"
        )
        return [[sys.executable, "-c", script]]
    return [[sys.executable, "-c", "from pathlib import Path; assert any(Path('.').rglob('*'))"]]
