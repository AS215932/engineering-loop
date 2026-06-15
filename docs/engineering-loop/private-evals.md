# Private evals — AS215932 domain judgment as token capital

The private eval suite is the offline contract that captures AS215932/Hyrule
domain judgment so it survives provider/model swaps. It runs in CI on every
change, with **no model and no network**, and blocks regressions in the
"company veteran" rules the loop must keep honoring.

## Layout

```
evals/
  schema.json              # JSON Schema for a case (documentation)
  cases/<family>/*.json    # one case per file
```

Families (≥3 cases each, ≥15 total):

| Family | What it guards |
|---|---|
| `domain-policy` | `servify.network` (infra) / `hyrule.host` (product) / `as215932.net` (AS/routing) identities are not blindly conflated or repurposed |
| `promotion-safety` | app pins go through `promote-apps` + `apply.yml`; no manual pin edits, no auto-merge, no automatic production apply |
| `noc-evidence` | NOC remediation needs evidence + rollback guard + operator approval; no real mutation in the no-op phase |
| `vps-launch-proof` | stay within the narrow VPS launch-proof contract; no generic payment-intent engine |
| `network-change` | FRR/firewall/BGP changes need emulated-lab verification (batfish/containerlab) + human review |

## Case format

```json
{
  "schema_version": 1,
  "id": "domain-policy-servify-network-preserved",
  "family": "domain-policy",
  "title": "Do not blindly replace servify.network",
  "input": {
    "issue_title": "...",
    "issue_body": "...",
    "repo": "AS215932/network-operations",
    "changed_paths": []
  },
  "must_include": ["servify.network is infrastructure identity", "do not blindly replace"],
  "must_not_include": ["replace all servify.network"],
  "expected_decision": "request_human_review",
  "tags": ["domain", "safety"]
}
```

- `expected_decision` ∈ `approve` | `request_human_review` | `reject`.
- `must_include` / `must_not_include` are case-insensitive substring checks against the rule's rationale.

## How it works

`src/hyrule_engineering_loop/evals.py` applies a deterministic per-family rule
to each case's `input`, producing a `(decision, rationale)`. `grade_case`
checks the decision matches `expected_decision` and the rationale satisfies the
`must_include` / `must_not_include` constraints. These rules are the **baseline
judgment**: the loop's LLM judgment can later be graded against the same corpus,
but the deterministic rules must keep passing so CI never depends on a model.

## Running

```bash
uv run --group dev hyrule-engineering-loop evals run --strict          # exit 1 on any failure
uv run --group dev hyrule-engineering-loop evals run --strict --json   # machine summary
```

JSON summary: `{ "total", "passed", "failed", "failed_ids" }`.

## Adding a case

1. Drop a JSON file under `evals/cases/<family>/` with a unique `id`.
2. If it exercises judgment the rules don't yet encode, extend the matching
   rule in `evals.py` (keep rationale strings stable — cases assert them).
3. `uv run --group dev hyrule-engineering-loop evals run --strict` must stay green.

CI runs the suite as the `evals` job (see `.github/workflows/ci.yml`); a failing
case blocks the PR.
