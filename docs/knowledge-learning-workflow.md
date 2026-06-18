# Knowledge learning workflow

Engineering Loop can produce a local, sanitized `learning_ledger_v1` artifact for
human review in `AS215932/knowledge`. This is default-off and does **not** write
to the knowledge repository.

## 1. Run Engineering Loop with context and a local learning directory

```bash
uv run hyrule-engineering-loop feature CHANGE_ID \
  --request request.md \
  --repo hyrule-cloud \
  --workspace-root /home/svag/Dev \
  --output-root .engineering-loop-state \
  --allow docs \
  --knowledge-context \
  --knowledge-repo /home/svag/Dev/knowledge \
  --knowledge-learning-dir .engineering-loop-state/learning-events
```

The generated artifact contains compact status/citation metadata only. It must
not include raw prompts, diffs, transcripts, stdout/stderr, command output,
secrets, logs, packet captures, or live telemetry dumps.

## 2. Import the local artifact in AS215932/knowledge

```bash
cd /home/svag/Dev/knowledge
uv run hyrule-knowledge ledger import \
  /home/svag/Dev/engineering-loop/.engineering-loop-state/learning-events/*.learning-event.json
```

Imported events land under `ledger/proposed/` as A4 proposals.

## 3. Review before promotion

```bash
uv run hyrule-knowledge ledger --list
uv run hyrule-knowledge ledger --review <event-id-or-subject> --promotion-kind summary
```

The review packet shows validation status, blockers, source refs, and a curated
OKF preview.

## 4. Promote through a review PR helper

```bash
uv run hyrule-knowledge ledger promote-pr <event-id-or-subject> \
  --reviewer svag \
  --promotion-kind summary \
  --rationale "Reviewed Engineering Loop run summary"
```

This writes:

- `ledger/reviews/<review>.json`
- `okf/curated/summaries/<summary>.md` for A2 summaries, or
  `okf/curated/lessons/<lesson>.md` for A1 lessons
- `reports/learning-promotion-pr.md` checklist
- refreshed exports/reports

Then open a normal PR in `AS215932/knowledge` and run:

```bash
uv run hyrule-knowledge validate okf
uv run hyrule-knowledge eval --check
uv run hyrule-knowledge ledger lifecycle --check
uv run hyrule-knowledge scan-secrets okf exports reports evals ledger schema
```

## Authority reminder

A promoted A2 summary is reviewed institutional learning, not source truth. If it
conflicts with A0 repository evidence, A0 wins and the summary needs re-review or
supersession.
