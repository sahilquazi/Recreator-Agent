"""
engine.py - The troubleshooter's state machine.

Loop: classify the run -> pick the route for that reason -> take at most one
step -> classify again. Every step spends a one-shot allowance (budget.py), and
MAX_STEPS caps the loop on top of that, so the run always ends. It never loops.

The engine knows nothing about Claude, Docker or Supabase. Everything that does
real work comes in through `Ports` (plain functions), so tests and demos can
swap in fakes. runner.py wires up the real ones.
"""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..llm import Tool
from . import case_file as case_file_io
from . import status
from .access import AccessCatalog, Approver, Grant, build_request
from .budget import MAX_STEPS, Allowance, CaseHistory
from .classify import URGENT, Classification, Situation, classify
from .models import (AccessRequest, AccessRequestDraft, CaseFile, Cause, CauseUpdate, FailureReason,
                     FixAttempt, StepOutput, TroubleshootResult, now_iso)
from .verifier import PatchVerifier


# ---------------------------------------------------------------- inputs and ports
@dataclass
class StepContext:
    """What an agent step is told about the case so far (technical findings only)."""
    reason: FailureReason
    why: str
    causes: list[Cause]
    fixes: list[FixAttempt]


@dataclass
class StepResult:
    output: StepOutput
    reads: list[str] = field(default_factory=list)   # what the step looked at, for "access used"


@dataclass
class Ports:
    verifier: PatchVerifier
    approver: Approver
    catalog: AccessCatalog = field(default_factory=AccessCatalog)
    reviewer: Callable[[StepContext], StepResult] | None = None
    recovery: Callable[[StepContext], StepResult] | None = None
    planner: Callable[[StepContext, AccessCatalog], AccessRequestDraft | None] | None = None
    investigator: Callable[[StepContext, AccessRequest, list[Tool]], StepResult] | None = None
    admin_questions: Callable[[], list[str]] | None = None


@dataclass
class CaseInput:
    ticket_id: int
    priority: str
    reproduced: bool
    problem_summary: str
    repro: str
    causes: list[Cause] = field(default_factory=list)
    initial_patch: str | None = None
    initial_target: str | None = None
    baseline_access: list[str] = field(default_factory=list)
    budget_minutes: float | None = None        # None = no time limit (P0/P1 hand off anyway)
    started_at: float | None = None             # time.monotonic() when the case started
    history: CaseHistory = field(default_factory=CaseHistory)
    grant_ttl_minutes: float = 30.0


@dataclass
class _Handoff:
    rule: str


@dataclass
class _AskAdmin:
    pass


@dataclass
class _Action:
    name: str


# ---------------------------------------------------------------- the engine
class Engine:
    def __init__(self, case: CaseInput, ports: Ports, run_dir: Path | None = None,
                 leak_check: Callable[[str], list] | None = None,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        self.case, self.ports, self.run_dir, self.leak_check = case, ports, run_dir, leak_check
        self.clock, self.log = clock, log
        self.started = case.started_at if case.started_at is not None else clock()
        self.allow = Allowance(history=case.history)
        self.steps: list[str] = []
        self.status_log: list[str] = []
        self.agent_next_step = ""
        self.access_stop_rule: str | None = None
        self.cf = CaseFile(
            ticket_id=case.ticket_id, priority=case.priority, problem_summary=case.problem_summary,
            repro=case.repro, causes=[c.model_copy(deep=True) for c in case.causes],
            access_log=list(case.baseline_access),
        )

    # ------------------------------------------------------------ main loop
    def run(self) -> TroubleshootResult:
        self._step(f"Troubleshooting started ({self.case.priority}).")
        if self.case.initial_patch:
            self._verify(self.case.initial_patch, self.case.initial_target, by="diagnosis")

        for _ in range(MAX_STEPS):
            cls = classify(self._situation())
            self.cf.failure_reason, self.cf.failure_reason_also_matched = cls.reason, cls.also_matched
            if cls.reason is None:
                return self._finish("fixed", None, cls)
            self._step(f"Classified as {cls.reason.value}: {cls.why}")

            choice = self._choose(cls)
            if isinstance(choice, _Handoff):
                return self._finish("handed_off", choice.rule, cls)
            if isinstance(choice, _AskAdmin):
                return self._finish("waiting_on_admin", "Waiting on the admin's answers (one round per case).", cls)
            self._emit(status.investigating(self.cf, self.case.reproduced))
            getattr(self, f"_do_{choice.name}")(cls)
            self._save()

        cls = classify(self._situation())
        return self._finish("handed_off", "Reached the step limit.", cls)

    # ------------------------------------------------------------ routing
    def _choose(self, cls: Classification) -> _Handoff | _AskAdmin | _Action:
        r, a, p = cls.reason, self.allow, self.ports
        if r is FailureReason.OUT_OF_TIME_OR_URGENT:
            return _Handoff(cls.why)
        if r is FailureReason.FIX_BREAKS_TESTS:
            return _Handoff("A fix passes the repro but breaks other tests. Which behavior to keep is a human call.")
        if r is FailureReason.CANT_REPRODUCE:
            if a.can_ask_admin() and p.admin_questions:
                return _AskAdmin()
            return _Handoff("Still can't reproduce after asking the admin once." if not a.can_ask_admin()
                            else "Can't reproduce, and there's no way to ask the admin from here.")
        if a.access_spent:
            return _Handoff(self.access_stop_rule or "It has used its one access request.")
        if r is FailureReason.FIX_FAILS_REPRO:
            if not p.verifier.can_verify:
                return _Handoff("A cause is confirmed, but fixes can't be verified automatically yet "
                                "(no patch verifier configured), so a retry wouldn't prove anything.")
            if a.can_retry_reviewer() and p.reviewer:
                return _Action("reviewer_retry")
            return _Handoff("Used its one Reviewer retry and no fix passes the repro.")
        # ALL_CAUSES_RULED_OUT: recovery pass, then one access request, then a human.
        if a.can_self_recover() and p.recovery:
            return _Action("self_recovery")
        if a.can_request_access() and p.planner:
            return _Action("access_request")
        if a.can_request_access():
            return _Handoff("Used its self-recovery pass; access requests aren't available for this case.")
        return _Handoff("It has used its one self-recovery pass and its one access request.")

    # ------------------------------------------------------------ steps
    def _do_reviewer_retry(self, cls: Classification) -> None:
        self.allow.reviewer_retry += 1
        self._step("Reviewer retry (once): proposing a different fix with the failure details.")
        self._apply(self.ports.reviewer(self._ctx(cls)), by="reviewer_retry")

    def _do_self_recovery(self, cls: Classification) -> None:
        self.allow.self_recovery += 1
        self._step("Self-recovery pass (once): re-ranking guesses, checking recent commits and near-misses.")
        self._apply(self.ports.recovery(self._ctx(cls)), by="self_recovery")

    def _do_access_request(self, cls: Classification) -> None:
        self.allow.access_request += 1
        ctx = self._ctx(cls)
        draft = self.ports.planner(ctx, self.ports.catalog)
        if draft is None or not (draft.resource or draft.unlisted_need):
            self.access_stop_rule = "The evidence doesn't point to any specific area to request access to."
            self._step("Access request: no target, so nothing was requested.")
            return

        entry = self.ports.catalog.entries.get(draft.resource)
        req = build_request(draft, entry, self.case.ticket_id)
        self.cf.access_request = req
        target = self._cause(req.target_cause)

        def decide(decision, who, rule):
            req.decision, req.decided_by, req.decided_at = decision, who, now_iso()
            self.access_stop_rule = rule
            self._step(f"Access request for '{req.resource or req.asking_for}': {decision} ({who}).")

        if req.missing_fields():
            return decide("not_available", "code", f"Request was incomplete (missing {req.missing_fields()}).")
        if entry is None or self.ports.investigator is None:
            if target:
                target.access_needed = req.asking_for
            return decide("not_available", "catalog",
                          "The access the evidence points to isn't something the agent can be granted. "
                          "The case file lists it under remaining guesses.")
        if entry.name in self.allow.history.denied_resources:
            return decide("denied", "earlier decision on this case", "Access request denied (a denial is final for this case).")

        remaining = self._minutes_left()
        if remaining is not None and entry.approval_eta_hours * 60 > remaining:
            if target:
                target.access_needed = req.asking_for
            return decide("too_slow", f"policy: approval usually takes ~{entry.approval_eta_hours:g}h",
                          "Approval would take too long. The case file lists what to check next and the access it needs.")

        decision, who = self.ports.approver.decide(req, entry)
        if decision == "denied":
            return decide("denied", who, "Access request denied (a denial is final for this case).")
        if decision != "approved":
            if target:
                target.access_needed = req.asking_for
            return decide(decision, who, "Approval would take too long. The case file lists what to check next "
                                         "and the access it needs.")

        decide("approved", who, "It has used its one access request.")
        grant = Grant(self.case.ticket_id, entry, self.case.grant_ttl_minutes)
        req.expires_at = grant.expires_at_iso
        try:
            result = self.ports.investigator(ctx, req, grant.tools(self.ports.catalog))
        finally:
            grant.revoke()
            req.expires_at = now_iso()       # revoked as soon as the investigation ends
        self.cf.access_log.append(f"Granted read-only '{entry.name}' ({entry.kind.value}) for this case only; "
                                  f"revoked when the investigation ended.")
        self._apply(result, by="access")
        if not result.output.new_evidence:
            self.access_stop_rule = "New access produced no new evidence (no cause confirmed or ruled out)."

    # ------------------------------------------------------------ helpers
    def _apply(self, result: StepResult, by: str) -> None:
        out = result.output
        self._merge(out.cause_updates, by)
        for read in result.reads:
            line = f"[{by}] {read}"
            if line not in self.cf.access_log:
                self.cf.access_log.append(line)
        if out.next_step:
            self.agent_next_step = out.next_step
        if out.notes:
            self._step(f"{by}: {out.notes}")
        if out.proposed_patch:
            self._verify(out.proposed_patch, out.target_cause, by=by)
        elif by != "access":
            self._step(f"{by}: no new fix proposed.")

    def _merge(self, updates: list[CauseUpdate], by: str) -> None:
        for u in updates:
            cause = self._cause(u.id)
            if cause is None:
                if not u.description.strip():
                    continue
                cause = Cause(id=f"C{len(self.cf.causes) + 1}", description=u.description, location=u.location,
                              confidence=u.confidence or 0.0, found_by=by, access_needed=u.access_needed)
                self.cf.causes.append(cause)
            if u.status != "untested" and u.status != cause.status:
                if not u.evidence.strip():
                    self._step(f"{by}: ignored status change on {cause.id} with no evidence.")
                else:
                    cause.status = u.status
            if u.evidence.strip():
                cause.evidence.append(f"[{by}] {u.evidence.strip()}")
            if u.confidence is not None:
                cause.confidence = u.confidence
            if u.access_needed:
                cause.access_needed = u.access_needed
            if u.location and not cause.location:
                cause.location = u.location

    def _verify(self, patch: str, target: str | None, by: str) -> None:
        if any(f.patch.strip() == patch.strip() for f in self.cf.fixes_tried):
            self._step(f"{by}: proposed the same patch again; not re-tested.")
            return
        r = self.ports.verifier.verify(patch, target)
        fix = FixAttempt(id=f"F{len(self.cf.fixes_tried) + 1}", by=by, target_cause=target, patch=patch,
                         verified=r.verified, repro_passed=r.repro_passed, tests_passed=r.tests_passed,
                         broken_tests=r.broken_tests, notes=r.notes)
        self.cf.fixes_tried.append(fix)
        self._step(f"Checked fix {fix.id} ({by}): {fix.why_failed()}.")
        cause = self._cause(target)
        if cause and fix.verified and fix.repro_passed:
            cause.status = "confirmed"
            cause.evidence.append(f"[verifier] Fix {fix.id} makes the repro pass.")
        self._save()

    def _cause(self, cause_id: str | None) -> Cause | None:
        return next((c for c in self.cf.causes if c.id == cause_id), None) if cause_id else None

    def _minutes_left(self) -> float | None:
        if self.case.budget_minutes is None:
            return None
        return self.case.budget_minutes - (self.clock() - self.started) / 60

    def _situation(self) -> Situation:
        left = self._minutes_left()
        return Situation(priority=self.case.priority, out_of_time=left is not None and left <= 0,
                         reproduced=self.case.reproduced, causes=self.cf.causes, fixes=self.cf.fixes_tried)

    def _ctx(self, cls: Classification) -> StepContext:
        return StepContext(cls.reason, cls.why, [c.model_copy(deep=True) for c in self.cf.causes],
                           [f.model_copy(deep=True) for f in self.cf.fixes_tried])

    def _step(self, text: str) -> None:
        self.steps.append(text)
        self.cf.note(text)
        self.log(f"  [troubleshoot] {text}")

    def _emit(self, message: str) -> None:
        self.status_log.append(message)

    def _save(self) -> None:
        case_file_io.save(self.cf, self.run_dir, self.leak_check)

    # ------------------------------------------------------------ finishing
    def _recommend(self, outcome: str, cls: Classification) -> str:
        cf = self.cf
        if outcome == "fixed":
            winner = next(f for f in cf.fixes_tried if f.counts)
            return f"Review fix {winner.id} and ship it through the normal release process."
        r = cls.reason
        if outcome == "waiting_on_admin":
            text = ("Send the admin the questions below. When they answer, re-run the pipeline on this ticket; "
                    "if it still can't be reproduced, it goes to an engineer.")
        elif self.case.priority in URGENT:
            text = f"Engineer leads ({self.case.priority}). Start from the findings below; the agent can keep working alongside."
        elif r is FailureReason.FIX_BREAKS_TESTS:
            fix = next(f for f in reversed(cf.fixes_tried) if f.repro_passed and f.tests_passed is False)
            text = (f"Decide which behavior to keep: fix {fix.id} makes the repro pass but breaks "
                    f"{', '.join(fix.broken_tests) or 'existing tests'}. Either update those tests or change the fix.")
        elif r is FailureReason.FIX_FAILS_REPRO:
            cause = next(c for c in cf.causes if c.status == "confirmed")
            tried = ", ".join(f.id for f in cf.fixes_tried) or "none"
            text = (f"Cause {cause.id} is confirmed ({cause.description}). Fixes tried: {tried}. "
                    "Write a fix for it starting from the notes on the last attempt.")
            if not self.ports.verifier.can_verify and cf.fixes_tried:
                text += " The proposed patch was never verified; applying it and re-running the repro is the quickest check."
        elif r is FailureReason.CANT_REPRODUCE:
            text = ("Reproduce it with the admin directly (screen share or their tenant config); "
                    "the agent's attempts are listed under Repro.")
        elif cf.remaining_guesses:
            top = cf.remaining_guesses[0]
            text = f"Check {top.id} next ({top.description})" + (f"; it needs {top.access_needed}." if top.access_needed else ".")
        else:
            text = "Every listed cause was ruled out. Point the agent at a new area, or take over."
        if self.agent_next_step and outcome != "fixed":
            text += f"\n\nAgent's suggestion: {self.agent_next_step}"
        return text

    def _finish(self, outcome: str, rule: str | None, cls: Classification) -> TroubleshootResult:
        questions: list[str] = []
        if outcome == "waiting_on_admin":
            questions = self.ports.admin_questions() if self.ports.admin_questions else []
            message = status.waiting_on_admin(questions)
        elif outcome == "fixed":
            message = status.fixed()
        else:
            message = status.handed_off(self.cf, self.case.reproduced)
        self._emit(message)
        self.cf.recommended_next_step = self._recommend(outcome, cls)
        if questions:
            self.cf.recommended_next_step += "\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        self._step(f"Finished: {outcome}" + (f" ({rule})" if rule else ""))
        self._save()
        return TroubleshootResult(
            outcome=outcome, failure_reason=cls.reason, priority=self.case.priority,
            engineer_involved_from_start=self.case.priority in URGENT, stop_rule=rule,
            budget_used=self.allow.used(), steps=self.steps, admin_questions=questions,
            admin_status=message, admin_status_log=self.status_log,
            winning_fix=next((f for f in self.cf.fixes_tried if f.counts), None), case_file=self.cf,
        )
