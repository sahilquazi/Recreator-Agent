"""
schemas.py - The shape of everything that moves through the pipeline.

Each agent finishes by calling a "submit" tool whose input must match one of
these models. Pydantic validates it, and if it's wrong the agent is told why
and tries again. That keeps outputs machine-readable instead of free text.
"""
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- the ticket
class Ticket(BaseModel):
    """
    Mirrors the ReportedIssues columns we ALLOW the agents to see.
    customer_id and assigned_to are left out on purpose: the agents
    never need to know who the customer or the engineer is.
    """
    id: int
    title: str = ""
    issue_description: str = ""      # the customer's own words (untrusted input)
    impact: str = ""
    severity: str = ""
    status: str = ""
    app_build_details: dict = {}
    identifier_logs: dict = {}
    tenant_setup: dict = {}
    affected_setup: dict = {}
    impact_timing: dict = {}
    reproduction_details: dict = {}

    @property
    def app(self) -> str:
        return str(self.app_build_details.get("app", ""))

    @property
    def version(self) -> str | None:
        return self.app_build_details.get("version") or None

    @property
    def last_good_version(self) -> str | None:
        return self.impact_timing.get("last_working_version") or None


# ---------------------------------------------------------------- step 1: triage
class EnvironmentSpec(BaseModel):
    users: list[str] = Field(description="Placeholder users and their roles, e.g. 'user_1 (system admin)'.")
    structure: list[str] = Field(description="Shape only: teams, channels, mailboxes, sites, files and sizes.")
    config: list[str] = Field(description="Settings, policies, licenses, plugins or add-ins to enable.")


class TriageResult(BaseModel):
    claim: str = Field(description="One sentence: what is wrong, in neutral technical terms.")
    expected: str
    actual: str
    environment: EnvironmentSpec
    repro_plan: list[str] = Field(description="Concrete steps to reproduce in a sandbox.")
    variations_to_try: list[str] = Field(description="Things to change one at a time to find the pattern.")
    hypotheses: list[str] = Field(description="Likely causes, most likely first, each with the reason.")
    missing_info: list[str] = Field(description="Ticket fields that are empty or too vague to use.")
    follow_up_questions: list[str] = Field(description="Questions to send back to the customer, if any.")


# ---------------------------------------------------------------- step 2: reproduction
class CallEvidence(BaseModel):
    method: str
    path: str = Field(description="Endpoint with IDs replaced by {placeholders}.")
    status: int
    observed: str = Field(description="What came back that matters, e.g. 'member_count: 3'.")
    expected: str | None = None


class ReproReport(BaseModel):
    status: Literal["reproduced", "partially_reproduced", "not_reproduced"]
    location: str = Field(description="App > screen > element where the bug shows.")
    trigger: str = Field(description="The exact action that shows the bug.")
    expected: str
    observed: str
    failing_calls: list[CallEvidence] = []
    errors: list[str] = Field(default=[], description="Error codes or log lines tied to the failure.")
    layer: Literal["client", "server", "config", "plugin", "unknown"]
    pattern: str = Field(description="Who and what is affected, and who and what is not.")
    attempts: list[str] = Field(description="What you tried, including what did not reproduce.")
    repro_script: str | None = Field(
        default=None,
        description="Self-contained Python script. Exit 1 if the bug is present, 0 if not.",
    )
    screenshots: list[str] = []
    confidence: float = Field(ge=0, le=1)
    # Filled in by the pipeline (not the agent) after re-running the script:
    verified_on_current: bool | None = None
    verified_clean_on_last_good: bool | None = None


# ---------------------------------------------------------------- step 3: hand-off
class Handoff(BaseModel):
    """
    The ONLY thing the code diagnosis agent receives. No ticket text, no
    customer details, no screenshots: just technical findings.
    """
    claim: str
    app: str
    version: str | None
    last_good_version: str | None
    layer: str
    failing_calls: list[CallEvidence]
    errors: list[str]
    pattern: str
    repro_script: str | None
    reproduction_verified: bool | None
    confidence: float


# ---------------------------------------------------------------- step 4: diagnosis
class Suspect(BaseModel):
    file: str
    function: str
    lines: str = Field(description="e.g. '2702-2723'")
    confidence: float = Field(ge=0, le=1)
    explanation: str = Field(description="Why this code produces exactly the observed behaviour.")


class Diagnosis(BaseModel):
    summary: str = Field(description="Two or three plain sentences a non-engineer could follow.")
    call_path: list[str] = Field(description="Request -> handler -> ... -> faulty line, one hop per item.")
    suspects: list[Suspect] = Field(description="Ranked, most likely first.")
    proposed_patch: str | None = Field(default=None, description="Unified diff. Not applied automatically.")
    caveats: list[str] = []
