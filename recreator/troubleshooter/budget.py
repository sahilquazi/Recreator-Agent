"""
budget.py - Priority, time budget, and the one-shot allowances for a case.

The allowances are counted per CASE, not per run: if a case comes back (for
example after the admin answers questions), what was used before still counts.
History is read from earlier recreator_runs rows for the same ticket.
"""
import re
from dataclasses import dataclass, field

# Severity words seen in tickets -> priority. "P0".."P3" are also accepted as-is.
_SEVERITY_TO_PRIORITY = {
    "critical": "P0", "sev0": "P0", "sev 0": "P0", "blocker": "P0",
    "high": "P1", "sev1": "P1", "sev 1": "P1", "urgent": "P1",
    "medium": "P2", "moderate": "P2", "normal": "P2", "sev2": "P2", "sev 2": "P2",
    "low": "P3", "minor": "P3", "sev3": "P3", "sev 3": "P3",
}


def priority_for(severity: str) -> str:
    """Map the ticket's severity field to P0..P3. Unknown values become P2 (middle of the road)."""
    text = (severity or "").strip().lower()
    match = re.fullmatch(r"p\s*([0-3])", text)
    if match:
        return f"P{match.group(1)}"
    return _SEVERITY_TO_PRIORITY.get(text, "P2")


# One of each per case. These numbers ARE the spec; change them only if the spec changes.
MAX_SELF_RECOVERY = 1
MAX_ACCESS_REQUESTS = 1
MAX_REVIEWER_RETRIES = 1
MAX_ADMIN_ROUNDS = 1
# Belt and braces: the most steps a single run can ever take (initial check + the four above).
MAX_STEPS = 5


@dataclass
class CaseHistory:
    """What earlier runs of the same ticket already used up."""
    self_recovery_used: int = 0
    access_requests_used: int = 0
    reviewer_retries_used: int = 0
    admin_rounds_used: int = 0
    denied_resources: set[str] = field(default_factory=set)

    @classmethod
    def from_prior_results(cls, rows: list[dict]) -> "CaseHistory":
        """rows: earlier `troubleshooting` JSON blobs for this ticket (oldest first)."""
        h = cls()
        for ts in rows:
            if not isinstance(ts, dict):
                continue
            used = ts.get("budget_used") or {}
            h.self_recovery_used += int(used.get("self_recovery", 0))
            h.access_requests_used += int(used.get("access_request", 0))
            h.reviewer_retries_used += int(used.get("reviewer_retry", 0))
            if ts.get("outcome") == "waiting_on_admin":
                h.admin_rounds_used += 1
            req = (ts.get("case_file") or {}).get("access_request") or {}
            if req.get("decision") == "denied" and req.get("resource"):
                h.denied_resources.add(req["resource"])
        return h


@dataclass
class Allowance:
    """Live counters for this run, starting from the case history."""
    history: CaseHistory = field(default_factory=CaseHistory)
    self_recovery: int = 0
    access_request: int = 0
    reviewer_retry: int = 0

    def can_self_recover(self) -> bool:
        return self.history.self_recovery_used + self.self_recovery < MAX_SELF_RECOVERY

    def can_request_access(self) -> bool:
        return self.history.access_requests_used + self.access_request < MAX_ACCESS_REQUESTS

    def can_retry_reviewer(self) -> bool:
        return self.history.reviewer_retries_used + self.reviewer_retry < MAX_REVIEWER_RETRIES

    def can_ask_admin(self) -> bool:
        return self.history.admin_rounds_used < MAX_ADMIN_ROUNDS

    @property
    def access_spent(self) -> bool:
        """Once the access request is used (this run or before), anything short of a fix is a handoff."""
        return not self.can_request_access()

    def used(self) -> dict:
        return {"self_recovery": self.self_recovery, "access_request": self.access_request,
                "reviewer_retry": self.reviewer_retry}
