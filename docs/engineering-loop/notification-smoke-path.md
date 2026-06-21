# Engineering Loop notification smoke path

Each completed daemon cycle reports its outcome through two channels:

1. **Discord webhook** — a short summary showing the run result
   (success, stuck, or over-budget), the change class, the risk tier,
   and iteration/wall-clock stats.
2. **Icinga passive check** — the daemon submits `loop!engineering-loop`
   with the corresponding state:
   - `OK` when the cycle completed and produced a draft PR
   - `WARNING` when the cycle is stuck or exhausted its retry budget
   - `CRITICAL` when the cycle failed outright or breached a hard gate

These are the Engineering Loop's own heartbeat/observability channels.
They do not replace the per-repo CI gates or the authoritative role
evaluators; they exist so the NOC can detect a silent or looping
daemon without reading pull-request bodies.
