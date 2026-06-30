# Reliability Governor Production Runtime

The Reliability Governor is the Staff Site Reliability Engineer, Autonomous
Operations control plane. It authorizes autonomous routing; it does not execute
Engineering work, own NOC recovery, or mutate Knowledge directly.

## Production v1

Production v1 is a timer-driven reconciler on the dedicated `loop` VM:

```text
systemd timer
  -> hyrule-engineering-loop reliability-governor --once
    -> scan unlabeled / loop:intake / loop:candidate GitHub issues
    -> fetch authoritative NOC LHP-v1 payloads from CaseService
    -> load authority-tiered Knowledge context
    -> write and post a Reliability Decision Record
    -> apply deterministic routing labels
```

The service is intentionally `Type=oneshot`, idempotent, and fail-closed. A
failure leaves the existing labels in place, with the next timer pass doing a
fresh reconciliation. GitHub remains the visible operations substrate, and the
Engineering daemon consumes only `loop:approved`.

Deployment defaults:

- unit: `configs/loop/hyrule-reliability-governor.service`;
- timer: `configs/loop/hyrule-reliability-governor.timer`;
- state: `/var/lib/engineering-loop/reliability-governor`;
- cadence: every 15 minutes with jitter, ahead of the Engineering daemon;
- Knowledge: loopback MCP context pack from the `AS215932/knowledge` runtime;
- NOC: `ENGINEERING_LOOP_NOC_LHP_BASE_URL` and
  `ENGINEERING_LOOP_NOC_LHP_SECRET` from the loop VM environment.

## Callback Model

The mature runtime may be persistent and callback-driven, but callbacks are
wake signals only. They never approve work directly.

Every callback becomes a normalized `ReliabilityGovernorWakeEvent` with:

- `schema_version`: `reliability-governor.wake.v1`;
- `event_id`, `source`, `event_type`, `subject`, `occurred_at`;
- optional `correlation_id`, `delivery_id`, and `payload_ref`;
- no raw webhook payload fields.

Initial sources are GitHub, GitHub Actions, NOC CaseService, Knowledge,
Engineering Loop, and the scheduler. Supported event types are issue changed,
check changed, NOC handoff changed, Knowledge context changed, Engineering run
changed, and scheduled reconcile.

When a wake event arrives, the Governor must refetch authority from GitHub,
CaseService, Knowledge, and CI before deciding. The output is still a
Reliability Decision Record plus one or more routing actions:

- GitHub labels/comments for intake authorization;
- NOC callback/request for verification or missing LHP context;
- Knowledge gap or learning proposal routing;
- human review routing.

## Ownership

- NOC Loop owns production cases, evidence, verification, and recovery state.
- Engineering Loop owns guarded implementation, checks, branches, and draft PRs.
- Knowledge Loop owns authority tiers, context packs, and reviewed learning.
- Reliability Governor owns authorization, routing, escalation, and audit.

Policy changes remain Tier 4 and require human review. Human merge remains
mandatory until outcome history justifies narrower auto-merge rules.
