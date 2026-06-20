# Hyrule Engineering Loop

Autonomous development loop for the Hyrule Networks (AS215932) infrastructure.

This repository is a [LangGraph](https://langchain-ai.github.io/langgraph/)
runtime that classifies a change, plans it into a task spec, delegates
implementation to a real coding-agent backend inside a guarded worktree, re-runs
gates, has senior-role agents judge the resulting diff, learns from every run,
and stops at a **draft PR** for human sign-off. Merges and production applies
are always human-gated.

Extracted from [`AS215932/network-operations`](https://github.com/AS215932/network-operations)
once the v2 refactor stabilized — see that repo's `docs/engineering-loop/` for
the design spec and roadmap, and `docs/agentic-development-loop.md` here for the
runtime reference.

## Why it exists

Running an ISP in public means a lot of small, precise changes: firewall rules,
monitoring checks, DNS records, config tweaks. The Engineering Loop automates
the mechanical parts — classification, planning, implementation, testing, and
review prep — while keeping humans in control of anything that touches
production.

## Layout

- `src/hyrule_engineering_loop/` — the LangGraph runtime, `AgentBackend`,
  policy/judgment/memory/intake/daemon modules, and the operator CLI.
- `tests/` — the phased test suites (`test_engineering_graph.py`,
  `test_phase*.py`), fully offline (mock backend, no API keys).
- `skills/` — role, writer, and ISP-procedure skills the loop injects.
- `docs/agent-loops/`, `docs/agentic-development-loop.md`,
  `docs/engineering-loop/` — role cards, runtime reference, and v2 design.
- `integrations/pi/` — the Pi `/loop` extension.
- `configs/loop/` — systemd service + timer for the operations lane.
- `model-policy.yml`, `engineering-loop-policy.yml` — model/backend routing
  and the mutation/publication policy guards.
- Optional AS215932 knowledge context-pack integration is default-off and read-only.

## Develop

```bash
uv run --group dev python -m pytest -q
uv run --group dev mypy --strict src
uvx ruff check src tests
```

## Run

```bash
uv run hyrule-engineering-loop --help
# one operations-lane cycle over the core AS215932 loop:approved queues:
uv run hyrule-engineering-loop daemon --once
```

A Docker image can be built for the `loop` VM runtime shape:

```bash
docker build -t as215932/engineering-loop:local .
docker run --rm as215932/engineering-loop:local --help
```

A feature run can optionally include a read-only knowledge context pack from
`AS215932/knowledge` without enabling live tools, telemetry, LLM calls, or writes
in the knowledge repo. Local development can shell out to a checkout:

```bash
uv run hyrule-engineering-loop feature CHANGE_ID \
  --request request.md \
  --repo hyrule-cloud \
  --workspace-root /home/svag/Dev \
  --output-root .engineering-loop-state \
  --allow docs \
  --knowledge-context \
  --knowledge-repo /home/svag/Dev/knowledge
```

On the dedicated `loop` VM, prefer the containerized knowledge MCP server over
shelling out to a checkout:

```bash
uv run hyrule-engineering-loop daemon --once \
  --knowledge-context \
  --knowledge-mcp-url http://127.0.0.1:8767/mcp \
  --knowledge-mcp-transport streamable-http
```

A run can also write a local sanitized learning-event artifact for human
promotion into `AS215932/knowledge` later. This does not write to the knowledge
repo and excludes raw prompts, diffs, transcripts, command output, and secrets.
See `docs/knowledge-learning-workflow.md` for the import/review/promotion flow:

```bash
uv run hyrule-engineering-loop feature CHANGE_ID \
  --request request.md \
  --repo hyrule-cloud \
  --workspace-root /home/svag/Dev \
  --output-root .engineering-loop-state \
  --allow docs \
  --knowledge-context \
  --knowledge-learning-dir .engineering-loop-state/learning-events
```

The daemon's default production scope is the eight core repos:
`engineering-loop`, `network-operations`, `hyrule-cloud`, `hyrule-web`,
`hyrule-mcp`, `noc-agent`, `hyrule-network-proxy`, and `as215932.net`. It runs low-and-slow by
default: at most 2 runs/day, $10/day, and docs-only mutation boundaries unless
a later reviewed PR widens them.

The dedicated `loop` VM sets `HYRULE_MODEL_POLICY_FILE` to
`configs/loop/model-policy.production.yml` after the operator completes Pi auth;
local tests keep using the root `model-policy.yml` mock backend.

## Safety

The backend executes generated code. CI runs only on the unprivileged
`ci-pr` runner (label `hyrule-public-pr`); the daemon refuses to run when
`GITHUB_ACTIONS` is set. Never schedule it on a privileged runner.

Knowledge context is read-only and policy-scoped. It can call the local
`hyrule-knowledge context-pack` command, a loopback `AS215932/knowledge` MCP
HTTP/SSE endpoint, or an explicit JSON fixture in tests. It stores returned
citations in graph state. It must not call live Prometheus/Icinga endpoints or
expose secrets. Optional learning events are local sanitized artifacts only;
humans import, review, and promote them in the knowledge repo.

## Related repositories

- [`network-operations`](https://github.com/AS215932/network-operations) — Production infrastructure record
- [`hyrule-mcp`](https://github.com/AS215932/hyrule-mcp) — Live MCP diagnostics consumed during investigations
- [`noc-agent`](https://github.com/AS215932/noc-agent) — Operator-facing incident agent
