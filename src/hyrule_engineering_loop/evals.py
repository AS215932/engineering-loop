"""Private evals — the offline, deterministic contract that captures AS215932
domain judgment as token capital (v2 architecture, private-evals phase).

Each case under ``evals/cases/<family>/*.json`` describes an input scenario
(an issue/change proposal) and the decision + rationale the loop *should*
produce. The runner here evaluates every case with deterministic rules — no
model, no network — so the suite gates CI and survives provider/model swaps.

As the loop's LLM judgment matures it can be graded against this same corpus;
the rules below are the baseline "company veteran" the suite must keep passing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EVAL_SCHEMA_VERSION = 1

# src/hyrule_engineering_loop/evals.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES_DIR = _REPO_ROOT / "evals" / "cases"

EvalDecision = Literal["approve", "request_human_review", "reject"]
EvalFamily = Literal[
    "domain-policy",
    "promotion-safety",
    "noc-evidence",
    "vps-launch-proof",
    "network-change",
]


class EvalInput(BaseModel):
    """The scenario presented to the loop. Extra keys are tolerated so cases
    can carry family-specific context without schema churn."""

    model_config = ConfigDict(extra="allow")

    issue_title: str = ""
    issue_body: str = ""
    repo: str = ""
    changed_paths: list[str] = Field(default_factory=list)

    def haystack(self) -> str:
        joined = " ".join([self.issue_title, self.issue_body, " ".join(self.changed_paths)])
        return joined.lower()


class EvalCase(BaseModel):
    schema_version: int
    id: str
    family: EvalFamily
    title: str
    input: EvalInput
    must_include: list[str] = Field(default_factory=list)
    must_not_include: list[str] = Field(default_factory=list)
    expected_decision: EvalDecision
    tags: list[str] = Field(default_factory=list)


class CaseOutcome(BaseModel):
    id: str
    family: EvalFamily
    expected_decision: EvalDecision
    actual_decision: EvalDecision
    rationale: str
    passed: bool
    failures: list[str] = Field(default_factory=list)


class EvalSummary(BaseModel):
    total: int
    passed: int
    failed: int
    failed_ids: list[str]
    outcomes: list[CaseOutcome]


class EvalError(RuntimeError):
    """Raised when a case file is malformed or no cases were found."""


# --- deterministic rule engine ---------------------------------------------
#
# Each family rule returns ``(decision, rationale)``. Rationale strings are the
# substrings cases assert via ``must_include``; keep them stable.

_BROAD_REPLACE = ("replace all", "replace every", "every reference", "move all", "remove all", "rename all")


def _domain_policy(inp: EvalInput) -> tuple[EvalDecision, str]:
    text = inp.haystack()
    if "as215932.net" in text and any(k in text for k in ("replace", "rename", "repurpose", "change the as", "move")):
        return "reject", (
            "as215932.net is the AS/routing identity and must not be repurposed or replaced."
        )
    if "servify.network" in text and any(k in text for k in _BROAD_REPLACE):
        return "request_human_review", (
            "servify.network is infrastructure identity; do not blindly replace it. "
            "A broad rename needs human review of every affected flow."
        )
    if "hyrule.host" in text:
        return "approve", (
            "hyrule.host is the product identity; a documentation-only clarification is safe."
        )
    return "approve", "No domain-identity risk detected; change is safe."


_PIN_HINTS = ("pin", "app-sha-pins", "promotion/", "version:", "_version")


def _promotion_safety(inp: EvalInput) -> tuple[EvalDecision, str]:
    text = inp.haystack()
    touches_pin = any(h in text for h in _PIN_HINTS) or any(
        "pin" in p.lower() or "version" in p.lower() for p in inp.changed_paths
    )
    manual = any(k in text for k in ("manual", "manually", "hand-edit", "hand edit", "directly edit", "bypass", "skip promotion", "without promote"))
    if touches_pin and manual:
        return "reject", (
            "App pins must be promoted via promote-apps and apply.yml; "
            "no manual pin edits except an emergency rollback with a recorded SHA."
        )
    if any(k in text for k in ("auto-merge", "auto merge", "automatic production apply", "skip the production gate", "bypass the gate")):
        return "reject", (
            "No auto-merge and no automatic production apply; the human production gate must hold."
        )
    return "approve", "Change follows the promotion path; no pin-safety violation."


def _noc_evidence(inp: EvalInput) -> tuple[EvalDecision, str]:
    text = inp.haystack()
    real_mutation = any(
        k in text for k in ("restart", "reload", "frr", "wireguard", "pf ", "firewall", "bgp", "config change", "mutate")
    )
    has_guard = all(k in text for k in ("rollback", "approval")) and "evidence" in text
    if real_mutation and not has_guard:
        return "request_human_review", (
            "NOC remediation requires evidence, a rollback guard, and operator approval; "
            "no real service mutation in the no-op phase."
        )
    if any(k in text for k in ("noop", "no-op", "no op")) and "rollback" in text:
        return "approve", "No-op rollback guard with evidence and rollback path; safe to proceed."
    return "request_human_review", (
        "Remediation must carry evidence and a rollback guard before any execution."
    )


def _vps_launch_proof(inp: EvalInput) -> tuple[EvalDecision, str]:
    text = inp.haystack()
    if any(k in text for k in ("generic payment", "payment-intent engine", "payment intent engine", "general billing", "subscription engine", "arbitrary payment")):
        return "reject", (
            "Keep the narrow VPS launch-proof contract; no generic payment-intent engine "
            "until the launch-proof wedge is green."
        )
    if any(k in text for k in ("quote", "create", "status", "launch-proof", "launch proof", "dns aaaa", "ssh smoke")):
        return "approve", (
            "Stays within the narrow launch-proof contract (quote/create/status/DNS/SSH/rollback)."
        )
    return "request_human_review", "Unclear scope; confirm it stays within the launch-proof contract."


def _network_change(inp: EvalInput) -> tuple[EvalDecision, str]:
    text = inp.haystack()
    risky = any(k in text for k in ("frr", "bgp", "ospf", "firewall", "nftables", "pf ", "wireguard", "routing", "peering"))
    verified = any(k in text for k in ("batfish", "containerlab", "emulated lab", "lab verified", "lab-verified"))
    if risky and not verified:
        return "request_human_review", (
            "Network changes require emulated-lab verification (batfish/containerlab) "
            "and human review before any production apply."
        )
    if risky and verified:
        return "approve", "Network change is lab-verified; proceed to human-gated apply."
    return "approve", "No risky network surface touched."


_RULES = {
    "domain-policy": _domain_policy,
    "promotion-safety": _promotion_safety,
    "noc-evidence": _noc_evidence,
    "vps-launch-proof": _vps_launch_proof,
    "network-change": _network_change,
}


def evaluate_case(case: EvalCase) -> tuple[EvalDecision, str]:
    """Apply the deterministic rule for the case's family."""
    return _RULES[case.family](case.input)


def grade_case(case: EvalCase) -> CaseOutcome:
    decision, rationale = evaluate_case(case)
    haystack = rationale.lower()
    failures: list[str] = []
    if decision != case.expected_decision:
        failures.append(f"decision {decision!r} != expected {case.expected_decision!r}")
    for needle in case.must_include:
        if needle.lower() not in haystack:
            failures.append(f"missing required phrase: {needle!r}")
    for needle in case.must_not_include:
        if needle.lower() in haystack:
            failures.append(f"contains forbidden phrase: {needle!r}")
    return CaseOutcome(
        id=case.id,
        family=case.family,
        expected_decision=case.expected_decision,
        actual_decision=decision,
        rationale=rationale,
        passed=not failures,
        failures=failures,
    )


def load_cases(root: Path | str | None = None) -> list[EvalCase]:
    cases_dir = Path(root) if root is not None else DEFAULT_CASES_DIR
    if not cases_dir.is_dir():
        raise EvalError(f"eval cases directory not found: {cases_dir}")
    cases: list[EvalCase] = []
    seen: dict[str, Path] = {}
    for path in sorted(cases_dir.rglob("*.json")):
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            case = EvalCase.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - surface the offending file
            raise EvalError(f"invalid eval case {path}: {exc}") from exc
        if case.schema_version != EVAL_SCHEMA_VERSION:
            raise EvalError(f"unsupported schema_version in {path}: {case.schema_version}")
        if case.id in seen:
            raise EvalError(f"duplicate case id {case.id!r} in {path} and {seen[case.id]}")
        seen[case.id] = path
        cases.append(case)
    if not cases:
        raise EvalError(f"no eval cases found under {cases_dir}")
    return cases


def run_evals(cases: list[EvalCase]) -> EvalSummary:
    outcomes = [grade_case(case) for case in cases]
    failed = [o for o in outcomes if not o.passed]
    return EvalSummary(
        total=len(outcomes),
        passed=len(outcomes) - len(failed),
        failed=len(failed),
        failed_ids=[o.id for o in failed],
        outcomes=outcomes,
    )


def summary_json(summary: EvalSummary) -> dict[str, Any]:
    return {
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "failed_ids": summary.failed_ids,
    }
