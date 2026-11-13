"""
models.py - The shape of everything the troubleshooter reads and writes.

Plain data only. The rules live in classify.py and engine.py.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, computed_field


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- failure reasons
class FailureReason(str, Enum):
    """The five ways a run can end without a fix that counts (see the spec)."""
    ALL_CAUSES_RULED_OUT = "all_causes_ruled_out"      # 1
    FIX_FAILS_REPRO = "fix_fails_repro"                # 2
    FIX_BREAKS_TESTS = "fix_breaks_tests"              # 3
    CANT_REPRODUCE = "cant_reproduce"                  # 4
    OUT_OF_TIME_OR_URGENT = "out_of_time_or_urgent"    # 5


REASON_LABELS: dict[FailureReason, str] = {
    FailureReason.ALL_CAUSES_RULED_OUT: "1. All likely causes ruled out",
    FailureReason.FIX_FAILS_REPRO: "2. Cause found, but no fix passes",
    FailureReason.FIX_BREAKS_TESTS: "3. Fix passes the repro but breaks other tests",
    FailureReason.CANT_REPRODUCE: "4. Can't reproduce",
    FailureReason.OUT_OF_TIME_OR_URGENT: "5. Out of time, or P0/P1",
}

Priority = Literal["P0", "P1", "P2", "P3"]
CauseStatus = Literal["untested", "confirmed", "ruled_out"]
Outcome = Literal["fixed", "handed_off", "waiting_on_admin"]


# ---------------------------------------------------------------- causes and fixes
class Cause(BaseModel):
    """One possible root cause and what we know about it."""
    id: str
    description: str
    location: str = Field(default="", description="file:function:lines, if known")
    confidence: float = Field(default=0.0, ge=0, le=1)
    status: CauseStatus = "untested"
    evidence: list[str] = []
    found_by: str = "diagnosis"     # diagnosis | self_recovery | reviewer_retry | access
    access_needed: str = Field(default="", description="For untested causes: what access testing it would need.")


class FixAttempt(BaseModel):
    """One patch and what happened when we checked it."""
    id: str
    by: str                          # diagnosis | reviewer_retry | self_recovery | access
    target_cause: str | None = None
    patch: str
    verified: bool = Field(description="False if the verifier could not run (the fix then never counts).")
    repro_passed: bool | None = None
    tests_passed: bool | None = None
    broken_tests: list[str] = []
    notes: str = ""

    @property
    def counts(self) -> bool:
        """The spec's bar: the repro passes with the fix AND the existing tests still pass."""
        return bool(self.verified and self.repro_passed and self.tests_passed)

    def why_failed(self) -> str:
        if not self.verified:
            return "not verified (no patch verifier is configured)"
        if self.repro_passed is False:
            return "the repro still fails with this fix"
        if self.tests_passed is False:
            broken = ", ".join(self.broken_tests) or "unnamed tests"
            return f"the repro passes but existing tests break: {broken}"
        if self.counts:
            return "passed"
        return "result unknown"


# ---------------------------------------------------------------- access requests
class AccessKind(str, Enum):
    """Everything the agent may ever ask for. All read-only; there is deliberately no write kind."""
    SOURCE_MODULE = "source_module"        # another service or module the failing code calls
    LOGS = "logs"                          # logs or telemetry for that service
    TENANT_CONFIG = "tenant_config"        # feature flags or config for the affected tenant
    DEPLOY_HISTORY = "deploy_history"      # deploy or commit history outside the current scope


Decision = Literal["approved", "denied", "too_slow", "not_available", "no_target"]


class AccessRequest(BaseModel):
    """The one-time ask. The first six fields are the spec's fixed format."""
    ruled_out: str = Field(description="What was checked and ruled out, with evidence.")
    points_to: str = Field(description="Where the evidence points, and why the agent can't see it now.")
    asking_for: str = Field(description="Exactly what read-only access is requested.")
    expects_to_find: str
    how_to_test: str
    limits: str = "This case only. Read-only. Expires automatically when the troubleshooting run ends."
    # Filled in by code, not by the agent:
    case_id: int
    resource: str = ""
    kind: AccessKind | None = None
    owner_team: str = ""
    target_cause: str | None = None
    requested_at: str = Field(default_factory=now_iso)
    decision: Decision | None = None
    decided_by: str = ""
    decided_at: str = ""
    expires_at: str = ""

    def missing_fields(self) -> list[str]:
        names = ["ruled_out", "points_to", "asking_for", "expects_to_find", "how_to_test", "limits"]
        return [n for n in names if not str(getattr(self, n)).strip()]


# ---------------------------------------------------------------- case file
class CaseFile(BaseModel):
    """
    The main output. Built as the run goes, so even a crash leaves the engineer something.
    The eight sections the spec requires are the computed fields at the bottom plus
    problem_summary, repro, failure_reason and recommended_next_step.
    """
    ticket_id: int
    priority: str = ""
    problem_summary: str = ""
    repro: str = ""
    failure_reason: FailureReason | None = None
    failure_reason_also_matched: list[FailureReason] = []
    causes: list[Cause] = []
    access_log: list[str] = []
    access_request: AccessRequest | None = None
    fixes_tried: list[FixAttempt] = []
    recommended_next_step: str = ""
    engineer_options: list[str] = [
        "Point the agent at a new area",
        "Work alongside the agent",
        "Take over",
    ]
    timeline: list[str] = []

    @computed_field
    @property
    def causes_tested(self) -> list[Cause]:
        return [c for c in self.causes if c.status != "untested"]

    @computed_field
    @property
    def remaining_guesses(self) -> list[Cause]:
        return sorted((c for c in self.causes if c.status == "untested"),
                      key=lambda c: c.confidence, reverse=True)

    @computed_field
    @property
    def access_used(self) -> list[str]:
        lines = list(self.access_log)
        if self.access_request:
            r = self.access_request
            lines.append(f"Access request for '{r.resource or r.asking_for}': {r.decision or 'pending'}"
                         + (f" (by {r.decided_by})" if r.decided_by else ""))
        return lines

    def note(self, text: str) -> None:
        self.timeline.append(f"{now_iso()}  {text}")

    def missing_sections(self) -> list[str]:
        """Sections that would render empty. A handoff must have none (spec P0)."""
        missing = []
        if not self.problem_summary.strip():
            missing.append("problem_summary")
        if not self.repro.strip():
            missing.append("repro")
        if self.failure_reason is None:
            missing.append("failure_reason")
        if not self.access_used:
            missing.append("access_used")
        if not self.recommended_next_step.strip():
            missing.append("recommended_next_step")
        # causes_tested, fixes_tried and remaining_guesses may legitimately be empty;
        # the renderer always prints them with an explicit "none" line.
        return missing


# ---------------------------------------------------------------- what the steps return
class CauseUpdate(BaseModel):
    """A change an agent step reports about one cause (existing id) or a new cause (id null)."""
    id: str | None = Field(default=None, description="Existing cause id (e.g. 'C2'), or null for a new cause.")
    description: str = Field(default="", description="What might be wrong. Required for new causes.")
    location: str = ""
    status: CauseStatus = "untested"
    evidence: str = Field(default="", description="What you read or ran that supports this status.")
    confidence: float | None = Field(default=None, ge=0, le=1)
    access_needed: str = Field(default="", description="If untested because you can't see it: what access would help.")


class StepOutput(BaseModel):
    """What every agent step (reviewer retry, self-recovery, access investigation) submits."""
    cause_updates: list[CauseUpdate] = []
    target_cause: str | None = Field(default=None, description="Id of the cause the proposed patch fixes.")
    proposed_patch: str | None = Field(default=None, description="Unified diff, or null if you have no new fix.")
    new_evidence: bool = Field(description="True if you confirmed or ruled out at least one cause with evidence.")
    next_step: str = Field(default="", description="What you would do next if you could.")
    notes: str = ""


class AccessRequestDraft(BaseModel):
    """What the agent writes. Code turns it into an AccessRequest and adds the limits."""
    resource: str = Field(description="Name of ONE resource from the catalog, or '' if none fits.")
    unlisted_need: str = Field(default="", description="If nothing in the catalog fits: what access you'd need.")
    target_cause: str | None = Field(default=None, description="Id of the cause this access would test.")
    ruled_out: str
    points_to: str
    expects_to_find: str
    how_to_test: str


# ---------------------------------------------------------------- final result
class TroubleshootResult(BaseModel):
    outcome: Outcome
    failure_reason: FailureReason | None = None
    priority: str
    engineer_involved_from_start: bool = False
    stop_rule: str | None = Field(default=None, description="Which rule ended the run, in plain words.")
    budget_used: dict = {}
    steps: list[str] = []
    admin_questions: list[str] = []
    admin_status: str = ""
    admin_status_log: list[str] = []
    winning_fix: FixAttempt | None = None
    case_file: CaseFile
