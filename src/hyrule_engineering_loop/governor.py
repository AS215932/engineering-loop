"""Reliability Governor: Staff SRE control plane for autonomous operations.

The Reliability Governor is deliberately separate from loop executors. It may
use LLM-style classification inputs later, but this module keeps authorization
as auditable policy: produce a Reliability Decision Record, post it, then apply
only the labels or loop routes permitted by deterministic policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field

from hyrule_engineering_loop.intake import (
    APPROVED_LABEL,
    CANDIDATE_LABEL,
    KNOWLEDGE_GAP_LABEL,
    NEEDS_CONTEXT_LABEL,
    NEEDS_HUMAN_LABEL,
    GhClient,
)
from hyrule_engineering_loop.knowledge_context import (
    KnowledgeContextConfig,
    load_knowledge_context,
)
from hyrule_engineering_loop.lhp import (
    HttpRequest,
    LhpClientConfig,
    fetch_lhp_payload,
    parse_lhp_pointer,
    payload_hash,
    safe_text,
)

INTAKE_LABEL = "loop:intake"
DECISION_MARKER = "reliability-governor-cdr:"
LEGACY_DECISION_MARKER = "loop-governor-cdr:"
CDR_SCHEMA_VERSION = "reliability-governor.cdr.v1"
WAKE_EVENT_SCHEMA_VERSION: Literal["reliability-governor.wake.v1"] = "reliability-governor.wake.v1"
GOVERNOR_NAME = "Reliability Governor"
GOVERNOR_ROLE = "staff_sre_autonomous_operations"
CONTROLLED_LOOPS: tuple[str, ...] = ("engineering", "noc", "knowledge")
DEFAULT_STRONG_HISTORY_SUCCESSES = 5
LHP_FETCH_ERROR_PREFIX = "fetch_error:"

SourceLoop = Literal["human", "noc", "knowledge", "scheduled_miner", "unknown"]
ControlledLoop = Literal["engineering", "noc", "knowledge"]
NextLoop = Literal["engineering", "noc", "knowledge", "human", "none"]
WakeEventSource = Literal[
    "github",
    "github_actions",
    "noc",
    "knowledge",
    "engineering",
    "scheduler",
]
WakeEventType = Literal[
    "github.issue.changed",
    "github_actions.check.changed",
    "noc.handoff.changed",
    "knowledge.context.changed",
    "engineering.run.changed",
    "scheduler.reconcile",
]
WakeEventSubjectKind = Literal[
    "github_issue",
    "noc_case",
    "noc_handoff",
    "pull_request",
    "github_check",
    "engineering_run",
    "knowledge_context",
    "repo",
    "global",
]
RoutingDecision = Literal[
    "allow_candidate",
    "allow_approved",
    "needs_context",
    "knowledge_gap",
    "needs_human",
    "reject",
]
KnowledgeStatus = Literal["current", "missing", "stale", "contradictory", "error"]
IntentType = Literal[
    "docs",
    "tests",
    "runbook",
    "dashboard",
    "monitoring",
    "alert_tuning",
    "non_prod_tooling",
    "internal_service_code",
    "provisioning_helper",
    "production_network",
    "customer_provisioning",
    "routing_policy",
    "secret",
    "billing",
    "legal",
    "compliance",
    "unknown",
]


def _default_engineering_target() -> list[ControlledLoop]:
    return ["engineering"]


def _default_controlled_loops() -> list[ControlledLoop]:
    return ["engineering", "noc", "knowledge"]


class IssueSnapshot(BaseModel):
    """The GitHub issue fields the Governor is allowed to reason over."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    url: str = ""
    updated_at: str = ""

    @property
    def issue_id(self) -> str:
        return f"{self.repo}#{self.number}"


class KnowledgeSummary(BaseModel):
    """Authority-tiered context used by policy."""

    model_config = ConfigDict(extra="forbid")

    status: KnowledgeStatus
    export_version: str = "unknown"
    context_pack_id: str = "unknown"
    authority_level_used: str = "unknown"
    policy_result: str = "unknown"
    refs: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class LhpAuthoritySummary(BaseModel):
    """CaseService payload identity used for NOC-origin work."""

    model_config = ConfigDict(extra="forbid")

    handoff_id: str
    case_id: str
    payload_hash: str


class WakeEventSubject(BaseModel):
    """Transport-neutral subject for a future Reliability Governor wake event."""

    model_config = ConfigDict(extra="forbid")

    kind: WakeEventSubjectKind
    id: str = Field(min_length=1)
    repo: str | None = None
    issue_number: int | None = None
    case_id: str | None = None
    handoff_id: str | None = None
    pull_request_number: int | None = None
    check_run_id: str | None = None
    run_id: str | None = None
    context_pack_id: str | None = None


class ReliabilityGovernorWakeEvent(BaseModel):
    """A callback wake signal; reconciliation still performs authorization."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["reliability-governor.wake.v1"] = WAKE_EVENT_SCHEMA_VERSION
    event_id: str = Field(min_length=1)
    source: WakeEventSource
    event_type: WakeEventType
    subject: WakeEventSubject
    occurred_at: datetime
    correlation_id: str | None = None
    delivery_id: str | None = None
    payload_ref: str | None = None


class IssueClassification(BaseModel):
    """Structured classification input consumed by deterministic policy."""

    model_config = ConfigDict(extra="forbid")

    source_loop: SourceLoop
    intent_type: IntentType
    risk_tier: int = Field(ge=0, le=4)
    domains: list[str] = Field(default_factory=list)
    blast_radius: str = "unknown"
    affected_assets: list[str] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    affected_customers: list[str] = Field(default_factory=list)
    expected_paths: list[str] = Field(default_factory=list)
    verification_method: str = ""
    rollback_plan: str = ""
    capability_hints: list[str] = Field(default_factory=list)
    production_routing: bool = False
    secrets: bool = False
    billing: bool = False
    legal: bool = False
    compliance: bool = False
    destructive_data: bool = False
    customer_impacting_config: bool = False
    rationale: str = ""


class CapabilityEnvelope(BaseModel):
    """An approved autonomy envelope."""

    model_config = ConfigDict(extra="forbid")

    id: str
    domains: list[str]
    allowed_repos: list[str]
    allowed_paths: list[str]
    forbidden_paths: list[str] = Field(default_factory=list)
    target_loops: list[ControlledLoop] = Field(default_factory=_default_engineering_target)
    source_loops: list[SourceLoop] = Field(default_factory=list)
    max_risk_tier: int = Field(ge=0, le=4)
    auto_approve_max_risk_tier: int = Field(default=1, ge=0, le=4)
    required_evidence: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    rollback_required: bool = True
    verification_required: bool = True
    allowed_source_loops: list[SourceLoop] = Field(default_factory=list)
    handoff_contract: str = "github_issue_labels"
    verification_owner: NextLoop = "engineering"
    learning_required: bool = False
    allows_production_routing: bool = False
    allows_secrets: bool = False
    allows_billing: bool = False
    allows_legal: bool = False
    allows_compliance: bool = False
    allows_destructive_data: bool = False
    allows_customer_config: bool = False
    success_count: int = 0
    failure_count: int = 0


class CapabilityRegistry(BaseModel):
    """Versioned capability registry."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    capabilities: list[CapabilityEnvelope] = Field(default_factory=list)


class CandidateDecisionRecord(BaseModel):
    """Auditable Reliability Decision Record posted to GitHub and JSON."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = CDR_SCHEMA_VERSION
    governor_name: str = GOVERNOR_NAME
    governor_role: str = GOVERNOR_ROLE
    controlled_loops: list[ControlledLoop] = Field(default_factory=_default_controlled_loops)
    record_id: str
    created_at: str
    issue_id: str
    repo: str
    issue_number: int
    authority_text_hash: str
    issue_text_hash: str
    source: SourceLoop
    intent_type: IntentType
    risk_tier: int = Field(ge=0, le=4)
    blast_radius: str
    affected_assets: list[str]
    affected_services: list[str]
    affected_customers: list[str]
    knowledge_export_version: str
    knowledge_context_pack_id: str
    knowledge_authority_level: str
    knowledge_status: KnowledgeStatus
    lhp: LhpAuthoritySummary | None = None
    matched_capability: str | None = None
    denial_reasons: list[str] = Field(default_factory=list)
    policy_rules: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    expected_paths: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    verification_method: str = ""
    rollback_plan: str = ""
    routing_decision: RoutingDecision
    next_loop: NextLoop
    handoff_contract: str
    labels_to_add: list[str] = Field(default_factory=list)
    labels_to_remove: list[str] = Field(default_factory=list)
    storage_path: str | None = None


@dataclass(frozen=True)
class GovernorConfig:
    """One Reliability Governor service cycle configuration."""

    repos: tuple[str, ...]
    state_dir: Path = Path(".engineering-loop-state/reliability-governor")
    registry_path: Path | None = None
    knowledge_context: KnowledgeContextConfig | None = None
    lhp: LhpClientConfig | None = None
    limit: int = 20
    dry_run: bool = False


@dataclass
class GovernorReport:
    """Outcome of one Reliability Governor service pass."""

    dry_run: bool
    records: list[CandidateDecisionRecord] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "governor_name": GOVERNOR_NAME,
            "governor_role": GOVERNOR_ROLE,
            "dry_run": self.dry_run,
            "records": [record.model_dump(mode="json") for record in self.records],
            "skipped": self.skipped,
        }


def default_capability_registry() -> CapabilityRegistry:
    """Built-in conservative registry used when no file is supplied."""

    return CapabilityRegistry.model_validate(
        {
            "version": 1,
            "capabilities": [
                {
                    "id": "tier0.docs-runbooks-tests",
                    "domains": ["docs", "runbook", "tests", "dashboard"],
                    "allowed_repos": ["*"],
                    "allowed_paths": ["docs/", "README.md", "tests/", ".github/", "dashboards/"],
                    "forbidden_paths": ["secrets/", "**/secrets/", ".env", ".env."],
                    "target_loops": ["engineering"],
                    "source_loops": ["human", "noc", "knowledge", "scheduled_miner"],
                    "max_risk_tier": 0,
                    "auto_approve_max_risk_tier": 0,
                    "required_evidence": ["knowledge_context", "verification_method"],
                    "required_checks": ["targeted_tests_or_docs_review"],
                    "rollback_required": True,
                    "verification_required": True,
                    "handoff_contract": "github_issue_labels",
                    "verification_owner": "engineering",
                    "learning_required": False,
                    "success_count": 0,
                    "failure_count": 0,
                },
                {
                    "id": "tier1.monitoring-alert-tuning",
                    "domains": ["monitoring", "alert_tuning"],
                    "allowed_repos": [
                        "AS215932/network-operations",
                        "AS215932/noc-agent",
                        "AS215932/engineering-loop",
                    ],
                    "allowed_paths": [
                        "docs/",
                        "tests/",
                        "monitoring/",
                        "alerts/",
                        "config/",
                        "app/knowledge/",
                    ],
                    "forbidden_paths": ["secrets/", "**/secrets/", ".env", ".env."],
                    "target_loops": ["engineering"],
                    "source_loops": ["human", "noc", "knowledge", "scheduled_miner"],
                    "max_risk_tier": 1,
                    "auto_approve_max_risk_tier": 1,
                    "required_evidence": [
                        "knowledge_context",
                        "verification_method",
                        "rollback_plan",
                    ],
                    "required_checks": ["targeted_tests_or_alert_fixture"],
                    "rollback_required": True,
                    "verification_required": True,
                    "handoff_contract": "github_issue_labels",
                    "verification_owner": "noc",
                    "learning_required": True,
                    "success_count": 0,
                    "failure_count": 0,
                },
                {
                    "id": "tier2.internal-service-low-risk",
                    "domains": ["internal_service_code", "provisioning_helper", "non_prod_tooling"],
                    "allowed_repos": [
                        "AS215932/hyrule-cloud",
                        "AS215932/hyrule-web",
                        "AS215932/hyrule-mcp",
                        "AS215932/noc-agent",
                        "AS215932/engineering-loop",
                    ],
                    "allowed_paths": [
                        "docs/",
                        "tests/",
                        "hyrule_cloud/",
                        "hyrule_web/",
                        "src/",
                        "app/",
                        "scripts/",
                    ],
                    "forbidden_paths": ["secrets/", "**/secrets/", ".env", ".env."],
                    "target_loops": ["engineering"],
                    "source_loops": ["human", "noc", "knowledge", "scheduled_miner"],
                    "max_risk_tier": 2,
                    "auto_approve_max_risk_tier": 2,
                    "required_evidence": [
                        "knowledge_context",
                        "verification_method",
                        "rollback_plan",
                    ],
                    "required_checks": ["pytest"],
                    "rollback_required": True,
                    "verification_required": True,
                    "handoff_contract": "github_issue_labels",
                    "verification_owner": "engineering",
                    "learning_required": True,
                    "success_count": 0,
                    "failure_count": 0,
                },
            ],
        }
    )


def load_capability_registry(path: Path | None) -> CapabilityRegistry:
    """Load a registry from YAML/JSON or return the built-in default."""

    if path is None:
        return default_capability_registry()
    loaded = yaml.safe_load(path.expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"capability registry must be a mapping: {path}")
    return CapabilityRegistry.model_validate(loaded)


def list_governor_issues(repos: list[str], *, client: GhClient) -> list[IssueSnapshot]:
    """List open issues eligible for Governor review."""

    issues: list[IssueSnapshot] = []
    for repo in repos:
        raw = client.run(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,body,labels,url,updatedAt",
            ]
        )
        decoded = json.loads(raw or "[]")
        for entry in decoded if isinstance(decoded, list) else []:
            if not isinstance(entry, dict):
                continue
            labels = [
                str(item.get("name", ""))
                for item in entry.get("labels", [])
                if isinstance(item, dict)
            ]
            issue = IssueSnapshot(
                repo=repo,
                number=int(entry.get("number", 0)),
                title=str(entry.get("title", "")),
                body=str(entry.get("body", "")),
                labels=labels,
                url=str(entry.get("url", "")),
                updated_at=str(entry.get("updatedAt", "")),
            )
            if _eligible_for_governor(issue):
                issues.append(issue)
    return issues


def governor_once(
    config: GovernorConfig,
    *,
    client: GhClient,
    lhp_requester: HttpRequest | None = None,
    knowledge_loader: Callable[[str, KnowledgeContextConfig | None], KnowledgeSummary] | None = None,
) -> GovernorReport:
    """Run one Reliability Governor pass over intake/candidate issues."""

    registry = load_capability_registry(config.registry_path)
    report = GovernorReport(dry_run=config.dry_run)
    issues = list_governor_issues(list(config.repos), client=client)
    for issue in issues[: config.limit]:
        record = govern_issue(
            issue,
            registry=registry,
            knowledge_context=config.knowledge_context,
            lhp_config=config.lhp,
            lhp_requester=lhp_requester,
            knowledge_loader=knowledge_loader,
        )
        if not config.dry_run:
            path = decision_record_path(record, config.state_dir)
            record.storage_path = str(path)
            if path.exists():
                if _labels_already_converged(issue, record):
                    report.skipped.append(f"{issue.issue_id}: unchanged decision {record.record_id}")
                else:
                    apply_label_transition(issue, record, client=client)
            else:
                post_decision_record(issue, record, client=client)
                path = write_decision_record(record, config.state_dir)
                record.storage_path = str(path)
                apply_label_transition(issue, record, client=client)
        report.records.append(record)
    return report


ReliabilityGovernorConfig: TypeAlias = GovernorConfig
ReliabilityGovernorReport: TypeAlias = GovernorReport
ReliabilityDecisionRecord: TypeAlias = CandidateDecisionRecord


def reliability_governor_once(
    config: ReliabilityGovernorConfig,
    *,
    client: GhClient,
    lhp_requester: HttpRequest | None = None,
    knowledge_loader: Callable[[str, KnowledgeContextConfig | None], KnowledgeSummary] | None = None,
) -> ReliabilityGovernorReport:
    """Product-named alias for ``governor_once``."""

    return governor_once(
        config,
        client=client,
        lhp_requester=lhp_requester,
        knowledge_loader=knowledge_loader,
    )


def govern_issue(
    issue: IssueSnapshot,
    *,
    registry: CapabilityRegistry,
    knowledge_context: KnowledgeContextConfig | None = None,
    lhp_config: LhpClientConfig | None = None,
    lhp_requester: HttpRequest | None = None,
    knowledge_loader: Callable[[str, KnowledgeContextConfig | None], KnowledgeSummary] | None = None,
) -> CandidateDecisionRecord:
    """Classify an issue, apply policy, and return a decision record."""

    lhp_payload: dict[str, Any] | None = None
    lhp_summary: LhpAuthoritySummary | None = None
    pointer = parse_lhp_pointer(issue.body)
    if pointer is not None:
        active_lhp = lhp_config or LhpClientConfig.from_env()
        if active_lhp.configured:
            try:
                lhp_payload = fetch_lhp_payload(pointer, active_lhp, requester=lhp_requester)
                lhp_summary = LhpAuthoritySummary(
                    handoff_id=pointer.handoff_id,
                    case_id=pointer.case_id,
                    payload_hash=payload_hash(lhp_payload)[:16],
                )
            except Exception as exc:
                lhp_summary = LhpAuthoritySummary(
                    handoff_id=pointer.handoff_id,
                    case_id=pointer.case_id,
                    payload_hash=f"{LHP_FETCH_ERROR_PREFIX}{payload_hash(type(exc).__name__ + str(exc))[:12]}",
                )
        else:
            lhp_summary = LhpAuthoritySummary(
                handoff_id=pointer.handoff_id,
                case_id=pointer.case_id,
                payload_hash="unfetched",
            )

    task_text = _authority_text(issue, lhp_payload)
    loader = knowledge_loader or _load_governor_knowledge
    knowledge = loader(task_text, knowledge_context)
    classification = classify_issue_intent(
        issue,
        task_text=_classification_text(issue, lhp_payload),
        lhp_payload=lhp_payload,
    )
    decision, capability, denial_reasons, policy_rules = decide_policy(
        classification,
        registry=registry,
        issue=issue,
        knowledge=knowledge,
        lhp_configured=lhp_summary is None or _lhp_payload_fetched(lhp_summary),
        knowledge_authority_min=_knowledge_authority_min(knowledge_context),
    )
    labels_to_add, labels_to_remove = labels_for_decision(decision)
    allowed_paths = capability.allowed_paths if capability is not None else []
    forbidden_paths = capability.forbidden_paths if capability is not None else []
    required_checks = capability.required_checks if capability is not None else []
    next_loop = _next_loop_for_decision(
        decision,
        classification=classification,
        knowledge=knowledge,
        capability=capability,
        lhp_summary=lhp_summary,
    )
    handoff_contract = _handoff_contract_for_decision(
        decision,
        capability=capability,
        next_loop=next_loop,
    )
    created_at = datetime.now(UTC).isoformat()
    authority_text_hash = payload_hash(task_text)
    issue_text_hash = payload_hash({"title": issue.title, "body": issue.body})
    record_id = payload_hash(
        {
            "schema": CDR_SCHEMA_VERSION,
            "issue": issue.issue_id,
            "authority_text_hash": authority_text_hash,
            "issue_text_hash": issue_text_hash,
            "classification": classification.model_dump(mode="json"),
            "knowledge": knowledge.model_dump(mode="json"),
            "decision": decision,
            "capability": capability.model_dump(mode="json") if capability is not None else None,
            "denial_reasons": denial_reasons,
            "labels_to_add": labels_to_add,
            "labels_to_remove": labels_to_remove,
        }
    )[:20]
    return CandidateDecisionRecord(
        record_id=record_id,
        created_at=created_at,
        issue_id=issue.issue_id,
        repo=issue.repo,
        issue_number=issue.number,
        authority_text_hash=authority_text_hash,
        issue_text_hash=issue_text_hash,
        source=classification.source_loop,
        intent_type=classification.intent_type,
        risk_tier=classification.risk_tier,
        blast_radius=classification.blast_radius,
        affected_assets=classification.affected_assets,
        affected_services=classification.affected_services,
        affected_customers=classification.affected_customers,
        knowledge_export_version=knowledge.export_version,
        knowledge_context_pack_id=knowledge.context_pack_id,
        knowledge_authority_level=knowledge.authority_level_used,
        knowledge_status=knowledge.status,
        lhp=lhp_summary,
        matched_capability=capability.id if capability is not None else None,
        denial_reasons=denial_reasons,
        policy_rules=policy_rules,
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        expected_paths=classification.expected_paths,
        required_checks=required_checks,
        verification_method=classification.verification_method,
        rollback_plan=classification.rollback_plan,
        routing_decision=decision,
        next_loop=next_loop,
        handoff_contract=handoff_contract,
        labels_to_add=labels_to_add,
        labels_to_remove=labels_to_remove,
    )


def classify_issue_intent(
    issue: IssueSnapshot,
    *,
    task_text: str,
    lhp_payload: dict[str, Any] | None = None,
) -> IssueClassification:
    """Deterministic fallback classifier; replaceable by reviewed LLM output."""

    text = _normalized_text(" ".join([issue.title, task_text]))
    source_loop = _source_loop(issue, lhp_payload=lhp_payload)
    assets = [issue.repo]
    services: list[str] = []
    customers: list[str] = []
    domains: list[str] = []
    expected_paths: list[str] = []
    intent: IntentType = "unknown"
    risk_tier = 2
    blast_radius = "internal repo"
    rationale = "default internal-service classification"

    secrets = _contains_any(text, ["secret", "token", "credential", "private key", "api key"])
    billing = _contains_any(text, ["billing", "invoice", "payment", "subscription", "price", "stripe"])
    legal = _contains_any(text, ["legal", "terms of service", "contract", "liability"])
    compliance = _contains_any(text, ["compliance", "gdpr", "kyc", "aml", "audit requirement"])
    destructive = _contains_any(text, ["delete data", "drop table", "truncate", "destroy customer"])
    production_routing = _contains_any(
        text,
        [
            "bgp",
            "frr",
            "ospf",
            "routing policy",
            "route-map",
            "prefix-list",
            "peering",
            "transit",
            "core routing",
        ],
    )
    customer_config = _contains_any(
        text,
        ["customer-impacting", "customer impacting", "customer provisioning", "provisioning config"],
    )

    if secrets:
        intent, risk_tier, domains = "secret", 4, ["secret"]
        expected_paths = ["secrets/"]
        blast_radius = "credential plane"
        rationale = "secrets are Tier 4"
    elif billing:
        intent, risk_tier, domains = "billing", 4, ["billing"]
        expected_paths = ["billing/"]
        blast_radius = "customer billing"
        customers = ["customers"]
        rationale = "billing/payment surfaces are Tier 4"
    elif legal:
        intent, risk_tier, domains = "legal", 4, ["legal"]
        expected_paths = ["legal/"]
        blast_radius = "legal/commercial"
        rationale = "legal surfaces are Tier 4"
    elif compliance:
        intent, risk_tier, domains = "compliance", 4, ["compliance"]
        expected_paths = ["compliance/"]
        blast_radius = "compliance"
        rationale = "compliance surfaces are Tier 4"
    elif _contains_any(text, ["runbook", "readme", "documentation", "docs", "typo"]):
        intent, risk_tier, domains = "runbook", 0, ["runbook", "docs"]
        expected_paths = ["docs/", "README.md"]
        blast_radius = "documentation only"
        rationale = "documentation/runbook work is Tier 0"
    elif _contains_any(text, ["test", "pytest", "fixture", "ci check"]):
        intent, risk_tier, domains = "tests", 0, ["tests"]
        expected_paths = ["tests/"]
        blast_radius = "test-only"
        rationale = "test-only work is Tier 0"
    elif _contains_any(text, ["dashboard", "grafana"]):
        intent, risk_tier, domains = "dashboard", 0, ["dashboard", "docs"]
        expected_paths = ["docs/", "dashboards/"]
        blast_radius = "operator dashboard"
        rationale = "dashboard/runbook work is Tier 0"
    elif _contains_any(text, ["alert", "monitoring", "icinga", "prometheus", "disk"]):
        intent, risk_tier, domains = "monitoring", 1, ["monitoring", "alert_tuning"]
        expected_paths = ["docs/", "monitoring/", "alerts/", "tests/"]
        services = ["monitoring"]
        blast_radius = "operator monitoring"
        rationale = "monitoring/alert tuning is Tier 1"
    elif production_routing:
        intent = "routing_policy" if _contains_any(text, ["policy", "route-map", "prefix-list"]) else "production_network"
        risk_tier = 4 if _contains_any(text, ["core routing", "peering strategy"]) else 3
        domains = ["production_network", "routing_policy"]
        expected_paths = ["host_vars/", "group_vars/", "roles/", "frr/", "network/"]
        services = ["production network"]
        customers = ["customers"]
        blast_radius = "production network"
        rationale = "production network behavior requires human approval"
    elif customer_config:
        intent, risk_tier, domains = "customer_provisioning", 3, ["customer_provisioning"]
        expected_paths = ["host_vars/", "group_vars/", "provisioning/", "scripts/"]
        customers = ["customers"]
        blast_radius = "customer-impacting provisioning"
        rationale = "customer-impacting provisioning is Tier 3"
    elif _contains_any(text, ["tooling", "non-prod", "nonprod", "developer tool"]):
        intent, risk_tier, domains = "non_prod_tooling", 1, ["non_prod_tooling"]
        expected_paths = ["docs/", "tests/", "scripts/", "src/"]
        blast_radius = "non-production tooling"
        rationale = "non-production tooling is Tier 1"
    else:
        intent, risk_tier, domains = "internal_service_code", 2, ["internal_service_code"]
        expected_paths = ["docs/", "tests/", "src/", "app/", "hyrule_cloud/", "hyrule_web/"]

    verification_method = _verification_method(text, lhp_payload=lhp_payload, intent=intent)
    rollback_plan = _rollback_plan(text, intent=intent)
    return IssueClassification(
        source_loop=source_loop,
        intent_type=intent,
        risk_tier=risk_tier,
        domains=domains,
        blast_radius=blast_radius,
        affected_assets=assets,
        affected_services=services,
        affected_customers=customers,
        expected_paths=expected_paths,
        verification_method=verification_method,
        rollback_plan=rollback_plan,
        production_routing=production_routing,
        secrets=secrets,
        billing=billing,
        legal=legal,
        compliance=compliance,
        destructive_data=destructive,
        customer_impacting_config=customer_config,
        rationale=rationale,
    )


def decide_policy(
    classification: IssueClassification,
    *,
    registry: CapabilityRegistry,
    issue: IssueSnapshot,
    knowledge: KnowledgeSummary,
    lhp_configured: bool,
    knowledge_authority_min: str = "A4",
) -> tuple[RoutingDecision, CapabilityEnvelope | None, list[str], list[str]]:
    """Apply deterministic hard gates and capability policy."""

    denial_reasons: list[str] = []
    policy_rules: list[str] = []
    if knowledge.status != "current":
        denial_reasons.extend(knowledge.reasons or [f"knowledge context is {knowledge.status}"])
        policy_rules.append("deny stale, contradictory, missing, or errored Knowledge context")
        return "knowledge_gap", None, denial_reasons, policy_rules
    if not _authority_satisfies(knowledge.authority_level_used, knowledge_authority_min):
        denial_reasons.append(
            f"Knowledge authority {knowledge.authority_level_used} is below required {knowledge_authority_min}"
        )
        policy_rules.append("deny Knowledge context below configured authority floor")
        return "knowledge_gap", None, denial_reasons, policy_rules
    if not lhp_configured and classification.source_loop == "noc":
        denial_reasons.append("NOC LHP pointer was present but CaseService payload was not fetched")
        policy_rules.append("treat GitHub prose as untrusted for NOC LHP work")
        return "needs_context", None, denial_reasons, policy_rules
    if not classification.verification_method:
        denial_reasons.append("missing verification method")
        policy_rules.append("deny work without a verification method")
        return "needs_context", None, denial_reasons, policy_rules
    if not classification.rollback_plan:
        denial_reasons.append("missing rollback plan")
        policy_rules.append("deny work without a rollback plan")
        return "needs_context", None, denial_reasons, policy_rules

    capability = _match_capability(classification, registry=registry, repo=issue.repo)
    if _has_sensitive_gate(classification):
        sensitive_denials = _sensitive_denials(classification, capability)
        if sensitive_denials:
            denial_reasons.extend(sensitive_denials)
            policy_rules.append("deny sensitive Tier 4 domains unless a capability explicitly allows them")
            return "needs_human", capability, denial_reasons, policy_rules

    if capability is None:
        denial_reasons.append("no matching capability envelope")
        policy_rules.append("without a capability, sufficiently specified work can only become candidate")
        return "allow_candidate", None, denial_reasons, policy_rules

    capability_denials = _capability_denials(classification, capability, repo=issue.repo)
    if capability_denials:
        denial_reasons.extend(capability_denials)
        policy_rules.append("deny when expected paths/source/risk exceed capability bounds")
        return "needs_human", capability, denial_reasons, policy_rules

    if classification.risk_tier <= min(1, capability.auto_approve_max_risk_tier):
        policy_rules.append("Tier 0/1 within capability envelope may be auto-approved")
        return "allow_approved", capability, denial_reasons, policy_rules
    if classification.risk_tier == 2:
        if (
            capability.auto_approve_max_risk_tier >= 2
            and capability.success_count >= DEFAULT_STRONG_HISTORY_SUCCESSES
            and capability.failure_count == 0
        ):
            policy_rules.append("Tier 2 auto-approval requires strong success history")
            return "allow_approved", capability, denial_reasons, policy_rules
        denial_reasons.append("Tier 2 lacks strong capability history for auto-approval")
        policy_rules.append("Tier 2 may become candidate but needs human approval without history")
        return "allow_candidate", capability, denial_reasons, policy_rules
    if classification.risk_tier == 3:
        denial_reasons.append("Tier 3 requires human approval")
        policy_rules.append("production/customer-impacting work cannot be auto-approved")
        return "allow_candidate", capability, denial_reasons, policy_rules

    denial_reasons.append("Tier 4 cannot be autonomously approved")
    policy_rules.append("Tier 4 requires human handling")
    return "needs_human", capability, denial_reasons, policy_rules


def labels_for_decision(decision: RoutingDecision) -> tuple[list[str], list[str]]:
    """Map a policy decision to deterministic GitHub label changes."""

    state_labels = [
        INTAKE_LABEL,
        CANDIDATE_LABEL,
        APPROVED_LABEL,
        NEEDS_CONTEXT_LABEL,
        KNOWLEDGE_GAP_LABEL,
        NEEDS_HUMAN_LABEL,
    ]
    if decision == "allow_approved":
        return [APPROVED_LABEL], [label for label in state_labels if label != APPROVED_LABEL]
    if decision == "allow_candidate":
        return [CANDIDATE_LABEL], [label for label in state_labels if label != CANDIDATE_LABEL]
    if decision == "needs_context":
        return [NEEDS_CONTEXT_LABEL], [label for label in state_labels if label != NEEDS_CONTEXT_LABEL]
    if decision == "knowledge_gap":
        return [KNOWLEDGE_GAP_LABEL], [label for label in state_labels if label != KNOWLEDGE_GAP_LABEL]
    return [NEEDS_HUMAN_LABEL], [label for label in state_labels if label != NEEDS_HUMAN_LABEL]


def _next_loop_for_decision(
    decision: RoutingDecision,
    *,
    classification: IssueClassification,
    knowledge: KnowledgeSummary,
    capability: CapabilityEnvelope | None,
    lhp_summary: LhpAuthoritySummary | None,
) -> NextLoop:
    if decision == "reject":
        return "none"
    if decision == "needs_human":
        return "human"
    if decision == "allow_candidate":
        return "human"
    if decision == "knowledge_gap" or knowledge.status != "current":
        return "knowledge"
    if decision == "needs_context":
        if classification.source_loop == "noc" and (
            lhp_summary is None or not _lhp_payload_fetched(lhp_summary)
        ):
            return "noc"
        return "human"
    if capability is not None and capability.target_loops:
        return capability.target_loops[0]
    return "engineering"


def _handoff_contract_for_decision(
    decision: RoutingDecision,
    *,
    capability: CapabilityEnvelope | None,
    next_loop: NextLoop,
) -> str:
    if decision == "allow_candidate":
        return "human_review"
    if capability is not None:
        return capability.handoff_contract
    if next_loop == "noc":
        return "case_service_lhp"
    if next_loop == "knowledge":
        return "knowledge_context_pack"
    if next_loop == "human":
        return "human_review"
    if decision == "reject":
        return "none"
    return "github_issue_labels"


def render_decision_comment(record: CandidateDecisionRecord) -> str:
    """Render the Reliability Decision Record comment."""

    payload = json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True)
    capability = record.matched_capability or "none"
    reasons = "; ".join(record.denial_reasons) if record.denial_reasons else "none"
    return "\n".join(
        [
            f"<!-- {DECISION_MARKER}{record.record_id} -->",
            "## Reliability Governor Decision",
            "",
            f"- role: `{record.governor_role}`",
            f"- decision: `{record.routing_decision}`",
            f"- next_loop: `{record.next_loop}`",
            f"- handoff_contract: `{record.handoff_contract}`",
            f"- source: `{record.source}`",
            f"- intent: `{record.intent_type}` / tier `{record.risk_tier}`",
            f"- capability: `{capability}`",
            f"- knowledge: `{record.knowledge_status}` / `{record.knowledge_authority_level}`",
            f"- reasons: {reasons}",
            "",
            "```json",
            payload,
            "```",
        ]
    )


def post_decision_record(
    issue: IssueSnapshot,
    record: CandidateDecisionRecord,
    *,
    client: GhClient,
) -> None:
    """Post the Reliability Decision Record before labels are changed."""

    client.run(
        [
            "issue",
            "comment",
            str(issue.number),
            "--repo",
            issue.repo,
            "--body",
            render_decision_comment(record),
        ]
    )


def apply_label_transition(
    issue: IssueSnapshot,
    record: CandidateDecisionRecord,
    *,
    client: GhClient,
) -> None:
    """Apply deterministic labels after the CDR has been posted."""

    current = set(issue.labels)
    for label in record.labels_to_remove:
        if label in current:
            client.run(
                [
                    "issue",
                    "edit",
                    str(issue.number),
                    "--repo",
                    issue.repo,
                    "--remove-label",
                    label,
                ]
            )
    for label in record.labels_to_add:
        if label not in current:
            client.run(
                [
                    "issue",
                    "edit",
                    str(issue.number),
                    "--repo",
                    issue.repo,
                    "--add-label",
                    label,
                ]
            )


def write_decision_record(record: CandidateDecisionRecord, state_dir: Path) -> Path:
    """Store the structured CDR JSON locally for replay/audit."""

    path = decision_record_path(record, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def decision_record_path(record: CandidateDecisionRecord, state_dir: Path) -> Path:
    """Return the local audit path for a stable decision record id."""

    root = state_dir.expanduser().resolve()
    filename = f"{_slug(record.repo)}-{record.issue_number}-{record.record_id}.json"
    return root / filename


def _load_governor_knowledge(
    task: str,
    config: KnowledgeContextConfig | None,
) -> KnowledgeSummary:
    if config is None or not config.enabled:
        return KnowledgeSummary(
            status="missing",
            reasons=["Knowledge context is not configured for Governor"],
        )
    loaded = load_knowledge_context(task, config=config)
    if loaded.get("status") != "ok" or not isinstance(loaded.get("pack"), dict):
        return KnowledgeSummary(
            status="error",
            reasons=[str(loaded.get("error") or loaded.get("status") or "knowledge load failed")],
        )
    return summarize_knowledge_pack(loaded["pack"])


def summarize_knowledge_pack(pack: dict[str, Any]) -> KnowledgeSummary:
    """Reduce a Knowledge context pack to the policy fields the Governor needs."""

    refs = [ref for ref in pack.get("included_refs", []) if isinstance(ref, dict)]
    ref_ids = [str(ref.get("concept_id", "unknown")) for ref in refs]
    reasons: list[str] = []
    status: KnowledgeStatus = "current"
    top_freshness = str(pack.get("freshness_status") or pack.get("context_status") or "").lower()
    if top_freshness in {"stale", "expired"}:
        status = "stale"
        reasons.append("Knowledge context is stale")
    if top_freshness in {"contradictory", "conflict", "conflicted"}:
        status = "contradictory"
        reasons.append("Knowledge context is contradictory")
    raw_policy = pack.get("policy_decision")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    policy_result = str(policy.get("result") or "unknown")
    if policy_result.lower() in {"deny", "reject", "blocked", "contradictory"}:
        status = "contradictory"
        reasons.append(f"Knowledge policy result is {policy_result}")
    for ref in refs:
        freshness = str(ref.get("freshness_status") or "").lower()
        if freshness in {"stale", "expired"}:
            status = "stale"
            reasons.append(f"Knowledge ref {ref.get('concept_id')} is stale")
        conflicts = ref.get("conflicts_with")
        if isinstance(conflicts, list) and conflicts:
            status = "contradictory"
            reasons.append(f"Knowledge ref {ref.get('concept_id')} has conflicts")
    authority = _best_authority(refs)
    if not refs:
        status = "missing"
        reasons.append("Knowledge context returned no included_refs")
    return KnowledgeSummary(
        status=status,
        export_version=str(
            pack.get("knowledge_snapshot")
            or pack.get("export_version")
            or pack.get("retrieval_version")
            or "unknown"
        ),
        context_pack_id=str(pack.get("id") or "unknown"),
        authority_level_used=authority,
        policy_result=policy_result,
        refs=ref_ids,
        reasons=reasons,
    )


def _authority_text(issue: IssueSnapshot, lhp_payload: dict[str, Any] | None) -> str:
    if lhp_payload is None:
        return safe_text(f"{issue.title}\n{issue.body}", limit=5000)
    selected = {
        "handoff": lhp_payload.get("handoff"),
        "case": lhp_payload.get("case"),
        "verification_objectives": lhp_payload.get("verification_objectives"),
        "knowledge_artifacts": lhp_payload.get("knowledge_artifacts"),
    }
    return safe_text(json.dumps(selected, sort_keys=True, default=str), limit=7000)


def _classification_text(issue: IssueSnapshot, lhp_payload: dict[str, Any] | None) -> str:
    if lhp_payload is None:
        return f"{issue.title}\n{issue.body}"[:7000]
    selected = {
        "handoff": lhp_payload.get("handoff"),
        "case": lhp_payload.get("case"),
        "verification_objectives": lhp_payload.get("verification_objectives"),
        "knowledge_artifacts": lhp_payload.get("knowledge_artifacts"),
    }
    return json.dumps(selected, sort_keys=True, default=str)[:9000]


def _eligible_for_governor(issue: IssueSnapshot) -> bool:
    labels = set(issue.labels)
    terminal = {NEEDS_CONTEXT_LABEL, KNOWLEDGE_GAP_LABEL, NEEDS_HUMAN_LABEL}
    if labels & terminal:
        return False
    loop_labels = {label for label in labels if label.startswith("loop:")}
    return not loop_labels or INTAKE_LABEL in labels or CANDIDATE_LABEL in labels or APPROVED_LABEL in labels


def _source_loop(issue: IssueSnapshot, *, lhp_payload: dict[str, Any] | None) -> SourceLoop:
    labels = {label.lower() for label in issue.labels}
    body = issue.body.lower()
    if lhp_payload is not None or parse_lhp_pointer(issue.body) is not None:
        return "noc"
    if "knowledge" in labels or "knowledge" in body:
        return "knowledge"
    if "scheduled" in labels or "filed by the engineering loop intake scan" in body:
        return "scheduled_miner"
    return "human"


def _verification_method(
    text: str,
    *,
    lhp_payload: dict[str, Any] | None,
    intent: IntentType,
) -> str:
    if lhp_payload is not None:
        objectives = [
            str(item.get("name") or item.get("objective_key"))
            for item in lhp_payload.get("verification_objectives", [])
            if isinstance(item, dict)
        ]
        criteria = [
            str(item)
            for item in (lhp_payload.get("handoff") or {}).get("acceptance_criteria", [])
        ] if isinstance(lhp_payload.get("handoff"), dict) else []
        combined = [item for item in objectives + criteria if item]
        if combined:
            return "; ".join(safe_text(item, limit=160) for item in combined[:4])
    if _contains_any(text, ["verify", "verified", "test", "pytest", "alert clears", "check", "validated"]):
        return "Use the issue-specified verification: tests/checks/evidence named in the request."
    if intent in {"docs", "runbook", "dashboard"}:
        return "Docs/runbook review plus any repository docs checks."
    if intent == "tests":
        return "Run the targeted test suite touched by the change."
    if intent in {"monitoring", "alert_tuning"}:
        return "Run targeted alert fixture/tests and verify the monitoring condition clears."
    return ""


def _rollback_plan(text: str, *, intent: IntentType) -> str:
    if "rollback" in text or "revert" in text:
        return "Use the rollback/revert procedure specified in the request."
    if intent in {"docs", "runbook", "dashboard", "tests"}:
        return "Close the draft PR or revert the docs/test commit before merge."
    if intent in {"monitoring", "alert_tuning", "non_prod_tooling"}:
        return "Revert the draft PR; for deployed alert tuning, restore the previous rule/config version."
    return ""


def _match_capability(
    classification: IssueClassification,
    *,
    registry: CapabilityRegistry,
    repo: str,
) -> CapabilityEnvelope | None:
    for capability in registry.capabilities:
        repo_allowed = "*" in capability.allowed_repos or repo in capability.allowed_repos
        domain_allowed = bool(set(classification.domains) & set(capability.domains))
        if repo_allowed and domain_allowed:
            return capability
    return None


def _capability_denials(
    classification: IssueClassification,
    capability: CapabilityEnvelope,
    *,
    repo: str,
) -> list[str]:
    denials: list[str] = []
    if repo not in capability.allowed_repos and "*" not in capability.allowed_repos:
        denials.append(f"repo {repo} is outside capability {capability.id}")
    if classification.source_loop not in _capability_source_loops(capability):
        denials.append(f"source loop {classification.source_loop} is not allowed")
    if classification.risk_tier > capability.max_risk_tier:
        denials.append(
            f"risk tier {classification.risk_tier} exceeds capability max {capability.max_risk_tier}"
        )
    for path in classification.expected_paths:
        if not _path_matches_any(path, capability.allowed_paths):
            denials.append(f"expected path {path} exceeds allowed paths")
    for path in classification.expected_paths:
        if _path_matches_any(path, capability.forbidden_paths):
            denials.append(f"expected path {path} is forbidden")
    if capability.verification_required and not classification.verification_method:
        denials.append("capability requires verification evidence")
    if capability.rollback_required and not classification.rollback_plan:
        denials.append("capability requires rollback plan")
    return denials


def _capability_source_loops(capability: CapabilityEnvelope) -> list[SourceLoop]:
    return capability.source_loops or capability.allowed_source_loops


def _knowledge_authority_min(config: KnowledgeContextConfig | None) -> str:
    if config is None:
        return "A4"
    return config.authority_min


def _authority_satisfies(level: str, minimum: str) -> bool:
    actual = _authority_rank(level)
    required = _authority_rank(minimum)
    return actual is not None and required is not None and actual <= required


def _authority_rank(level: str) -> int | None:
    return {"A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4}.get(level)


def _lhp_payload_fetched(summary: LhpAuthoritySummary) -> bool:
    return summary.payload_hash != "unfetched" and not summary.payload_hash.startswith(LHP_FETCH_ERROR_PREFIX)


def _labels_already_converged(issue: IssueSnapshot, record: CandidateDecisionRecord) -> bool:
    labels = set(issue.labels)
    return all(label in labels for label in record.labels_to_add) and all(
        label not in labels for label in record.labels_to_remove
    )


def _has_sensitive_gate(classification: IssueClassification) -> bool:
    return any(
        [
            classification.production_routing,
            classification.secrets,
            classification.billing,
            classification.legal,
            classification.compliance,
            classification.destructive_data,
            classification.customer_impacting_config,
        ]
    )


def _sensitive_denials(
    classification: IssueClassification,
    capability: CapabilityEnvelope | None,
) -> list[str]:
    if capability is None:
        denials: list[str] = []
        if classification.production_routing:
            denials.append("production routing is not explicitly allowed")
        if classification.secrets:
            denials.append("secrets are not explicitly allowed")
        if classification.billing:
            denials.append("billing is not explicitly allowed")
        if classification.legal:
            denials.append("legal work is not explicitly allowed")
        if classification.compliance:
            denials.append("compliance work is not explicitly allowed")
        if classification.destructive_data:
            denials.append("destructive data work is not explicitly allowed")
        if classification.customer_impacting_config:
            denials.append("customer-impacting config is not explicitly allowed")
        return denials or ["sensitive domain has no explicit capability"]
    explicit_denials: list[str] = []
    if classification.production_routing and not capability.allows_production_routing:
        explicit_denials.append("production routing is not explicitly allowed")
    if classification.secrets and not capability.allows_secrets:
        explicit_denials.append("secrets are not explicitly allowed")
    if classification.billing and not capability.allows_billing:
        explicit_denials.append("billing is not explicitly allowed")
    if classification.legal and not capability.allows_legal:
        explicit_denials.append("legal work is not explicitly allowed")
    if classification.compliance and not capability.allows_compliance:
        explicit_denials.append("compliance work is not explicitly allowed")
    if classification.destructive_data and not capability.allows_destructive_data:
        explicit_denials.append("destructive data work is not explicitly allowed")
    if classification.customer_impacting_config and not capability.allows_customer_config:
        explicit_denials.append("customer-impacting config is not explicitly allowed")
    return explicit_denials


def _path_matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.lstrip("/")
    for pattern in patterns:
        if pattern == "*":
            return True
        clean = pattern.lstrip("/")
        if clean.startswith("**/"):
            clean = clean[3:]
        if clean.endswith("/"):
            if normalized.startswith(clean):
                return True
            continue
        if normalized == clean or normalized.startswith(clean.rstrip("/") + "/"):
            return True
    return False


def _best_authority(refs: list[dict[str, Any]]) -> str:
    order = {"A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4}
    best = "unknown"
    best_rank = 99
    for ref in refs:
        level = str(ref.get("authority_tier") or ref.get("authority") or "unknown")
        rank = order.get(level, 99)
        if rank < best_rank:
            best = level
            best_rank = rank
    return best


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _normalized_text(text: str) -> str:
    return " ".join(text.lower().split())


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-").lower()
