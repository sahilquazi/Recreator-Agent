"""
models.py - The shape of a Solution Summary.

Same fields and JSON layout (camelCase) as the Engineering Direct portal's `SolutionSummary` type
(src/data/types.ts in mchung042/product_gt_microsoft_fall_2026), so the portal can read what this step saves.
Change both together.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

Confidence = Literal["high", "medium", "low"]
CheckResult = Literal["pass", "fail", "not_run"]   # not_run never counts as a pass
Basis = Literal["reasoned", "pr_tests", "sandbox"]  # reasoned = read from the code, never run
TagCategory = Literal["component", "area", "cause", "symptom", "surface", "version", "fix_size"]


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Evidence(_Model):
    """A source a claim rests on: one step of the run (no url) or a public page."""
    id: str
    kind: Literal["issue", "pull_request", "commit", "triage", "repro", "diagnosis", "case_file"]
    label: str
    url: str | None = None


class SolutionCheck(_Model):
    id: str
    name: str
    description: str


class Claim(_Model):
    text: str
    source_ids: list[str]


class Environment(_Model):
    product: str
    version: str
    surfaces: list[str]
    source_ids: list[str]


class RootCause(_Model):
    text: str
    file: str            # "" when the cause has no code location yet
    fn: str
    confidence: Confidence
    source_ids: list[str]


class SolutionTested(_Model):
    id: int
    approach: str
    results: dict[str, CheckResult]   # one per check id
    overall: CheckResult
    why: str
    basis: Basis
    source_ids: list[str]


class Recommendation(_Model):
    solution_id: int
    change: str
    confidence: Confidence
    source_ids: list[str]


class NextStep(_Model):
    reason: str
    text: str
    source_ids: list[str]


class SummaryTag(_Model):
    category: TagCategory
    value: str


class KnownGap(_Model):
    kind: Literal["inferred", "not_validated", "unseen", "unchecked"]
    text: str
    source_ids: list[str]


class Recreation(_Model):
    attached: bool
    text: str
    script: str | None = None
    verified: bool | None = None


class SolutionSummary(_Model):
    problem: Claim
    environment: Environment
    root_cause: RootCause
    checks: list[SolutionCheck]
    solutions_tested: list[SolutionTested]
    outcome: Literal["fix_found", "no_fix"]
    recommended_solution: Recommendation | None
    next_step: NextStep | None   # set when nothing passed: why, and what an engineer does next
    tags: list[SummaryTag]
    evidence: list[Evidence]
    known_gaps: list[KnownGap]
    recreation: Recreation

    def to_json(self) -> dict:
        """The portal's JSON: camelCase, optional fields left out, but recommendedSolution / nextStep kept as null."""
        out = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        out.setdefault("recommendedSolution", None)
        out.setdefault("nextStep", None)
        return out


class ReportEvidence(_Model):
    """What the engineer report shows beside the summary. Not read when building the summary."""
    screenshots: list[dict]                 # {path, caption, by, sourceId}
    faulty_code: list[dict]                 # {file, fn, startLine, lines, faulty, note, sourceId}
    patches: dict[int, str]                 # solution id -> unified diff
    how_tested: dict[int, list[str]]        # solution id -> steps, in order
    repro_steps: list[str]
    call_path: list[str] = []
