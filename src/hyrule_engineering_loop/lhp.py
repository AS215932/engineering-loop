"""Loop Handoff Protocol v1 helpers for NOC-origin work.

GitHub issue text remains delivery/triage only. These helpers parse the bounded
pointer embedded by NOC, fetch the authoritative payload from CaseService, and
post authenticated progress callbacks when enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

LHP_SCHEMA_VERSION = "lhp.v1"
POINTER_RE = re.compile(r"```json\s*(\{.*?\"fetch_path\".*?\})\s*```", re.S)
HANDOFF_MARKER_RE = re.compile(r"noc-lhp-handoff-id:([A-Za-z0-9_.:-]+)")
CASE_MARKER_RE = re.compile(r"noc-case-id:([A-Za-z0-9_.:-]+)")

HttpRequest = Callable[[str, str, dict[str, str] | None, bytes | None], tuple[int, dict[str, Any]]]


@dataclass(frozen=True)
class LhpPointer:
    handoff_id: str
    case_id: str
    fetch_path: str
    schema_version: str = LHP_SCHEMA_VERSION


@dataclass(frozen=True)
class LhpClientConfig:
    base_url: str = ""
    secret: str = ""
    callback_enabled: bool = False
    timeout_s: float = 20.0

    @classmethod
    def from_env(cls) -> "LhpClientConfig":
        return cls(
            base_url=os.environ.get("ENGINEERING_LOOP_NOC_LHP_BASE_URL", "").strip(),
            secret=os.environ.get("ENGINEERING_LOOP_NOC_LHP_SECRET", "").strip(),
            callback_enabled=os.environ.get("ENGINEERING_LOOP_LHP_CALLBACK_ENABLED", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.secret)


def parse_lhp_pointer(body: str) -> LhpPointer | None:
    text = str(body or "")
    match = POINTER_RE.search(text)
    payload: dict[str, Any] = {}
    if match:
        try:
            decoded = json.loads(match.group(1))
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}
    if not payload:
        handoff_match = HANDOFF_MARKER_RE.search(text)
        case_match = CASE_MARKER_RE.search(text)
        if not handoff_match or not case_match:
            return None
        handoff_id = handoff_match.group(1)
        payload = {
            "schema_version": LHP_SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "case_id": case_match.group(1),
            "fetch_path": f"/loop-handoff/v1/engineering/handoffs/{handoff_id}",
        }
    if payload.get("schema_version") != LHP_SCHEMA_VERSION:
        return None
    handoff_id = _token(payload.get("handoff_id"))
    case_id = _token(payload.get("case_id"))
    fetch_path = str(payload.get("fetch_path") or "")
    if not handoff_id or not case_id or not fetch_path.startswith("/loop-handoff/v1/engineering/handoffs/"):
        return None
    return LhpPointer(handoff_id=handoff_id, case_id=case_id, fetch_path=fetch_path)


def fetch_lhp_payload(pointer: LhpPointer, config: LhpClientConfig, *, requester: HttpRequest | None = None) -> dict[str, Any]:
    if not config.configured:
        raise RuntimeError("LHP client is not configured")
    requester = requester or _default_requester(config.timeout_s)
    status, payload = requester("GET", _url(config.base_url, pointer.fetch_path), _headers(config, "GET", pointer.fetch_path, {}), None)
    if status != 200:
        raise RuntimeError(f"NOC LHP fetch failed with status {status}")
    if payload.get("schema_version") != LHP_SCHEMA_VERSION:
        raise RuntimeError("NOC LHP payload schema mismatch")
    handoff = _dict_value(payload.get("handoff"))
    if handoff.get("handoff_id") != pointer.handoff_id or handoff.get("case_id") != pointer.case_id:
        raise RuntimeError("NOC LHP payload identity mismatch")
    return payload


def render_lhp_request(payload: dict[str, Any], *, issue_url: str, issue_body: str) -> str:
    handoff = _dict_value(payload.get("handoff"))
    case = _dict_value(payload.get("case"))
    objectives = _list_value(payload.get("verification_objectives"))
    lines = [
        f"# {safe_text(handoff.get('objective') or 'NOC LHP request')}",
        "",
        f"- source issue: {issue_url}",
        f"- schema_version: {payload.get('schema_version', LHP_SCHEMA_VERSION)}",
        f"- case_id: {safe_text(handoff.get('case_id') or case.get('case_id'))}",
        f"- handoff_id: {safe_text(handoff.get('handoff_id'))}",
        f"- objective_key: {safe_text(handoff.get('objective_key'))}",
        f"- case_type: {safe_text(handoff.get('case_type'))}",
        "",
        "## Resource",
        json.dumps(handoff.get("resource") or {}, indent=2, sort_keys=True),
        "",
        "## Constraints",
        *(f"- {safe_text(item)}" for item in (handoff.get("constraints") or [])),
        "",
        "## Acceptance criteria",
        *(f"- {safe_text(item)}" for item in (handoff.get("acceptance_criteria") or [])),
        "",
        "## Verification objectives",
        *(f"- {safe_text(obj.get('objective_key'))}: {safe_text(obj.get('name'))}" for obj in objectives if isinstance(obj, dict)),
        "",
        "## Untrusted background from GitHub issue",
        safe_text(issue_body, limit=2000),
    ]
    return "\n".join(lines)


def post_lhp_update(
    pointer: LhpPointer,
    config: LhpClientConfig,
    *,
    update_type: str,
    status: str,
    summary: str = "",
    evidence: list[dict[str, Any]] | None = None,
    requester: HttpRequest | None = None,
) -> bool:
    if not (config.configured and config.callback_enabled):
        return False
    requester = requester or _default_requester(config.timeout_s)
    body = {
        "schema_version": LHP_SCHEMA_VERSION,
        "case_id": pointer.case_id,
        "handoff_id": pointer.handoff_id,
        "source_loop": "engineering",
        "update_type": update_type,
        "status": status,
        "summary": safe_text(summary, limit=1200),
        "evidence": evidence or [],
        "external_event_id": f"engineering-loop:{pointer.handoff_id}:{update_type}:{payload_hash(summary + status)[:12]}",
        "correlation_id": f"eng_{payload_hash(pointer.handoff_id)[:12]}",
    }
    path = "/webhook/engineering-loop/handoff-update"
    status_code, _ = requester("POST", _url(config.base_url, path), _headers(config, "POST", path, body), json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return 200 <= status_code < 300


def payload_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def safe_text(value: Any, *, limit: int = 1000) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "[redacted]", text, flags=re.I)
    text = re.sub(r"\b(password|passwd|secret|token|credential)\s*[:=]\s*[^\s,;]+", "[redacted]", text, flags=re.I)
    text = "".join(" " if ch in "`<>[]{}" or ord(ch) < 32 else ch for ch in text)
    return (text or "—")[:limit]


def _headers(config: LhpClientConfig, method: str, path: str, body: Any) -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat()
    return {
        "Content-Type": "application/json",
        "X-NOC-Loop-Identity": "engineering",
        "X-NOC-Loop-Timestamp": timestamp,
        "X-NOC-Loop-Signature": _signature(config.secret, method, path, timestamp, body),
    }


def _signature(secret: str, method: str, path: str, timestamp: str, body: Any) -> str:
    message = "\n".join([_token(method.upper()) or "GET", path, _token(timestamp), json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)]).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _default_requester(timeout_s: float) -> HttpRequest:
    def request(method: str, url: str, headers: dict[str, str] | None, data: bytes | None) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
                return int(response.status), json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                payload = {"error": raw}
            return int(exc.code), payload
    return request


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _token(value: Any) -> str:
    text = str(value or "")
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-", ":", ".", "/"})[:180]
