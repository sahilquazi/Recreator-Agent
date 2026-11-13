"""
demo.py - Scripted cases that walk through every route, with no Claude, Docker or Supabase.

    python -m recreator.troubleshooter.demo                 # summary of every scenario
    python -m recreator.troubleshooter.demo access_denied   # one scenario, full case file

The agent steps are fakes that return fixed answers, so you can see exactly how
the engine routes each failure reason and enforces the limits. The tests reuse
these scenarios (tests/test_troubleshooter.py).
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .access import AccessCatalog, CatalogEntry, StaticApprover
from .budget import CaseHistory
from .case_file import render_markdown
from .engine import CaseInput, Engine, Ports, StepResult
from .models import AccessKind, AccessRequestDraft, Cause, CauseUpdate, StepOutput, TroubleshootResult
from .verifier import NoVerifier, RecordedVerifier, VerifyResult

PATCH = "--- a/server/channels/app/channel.go\n+++ b/server/channels/app/channel.go\n@@ -1 +1 @@\n-old\n+{}\n"

PASS = VerifyResult(verified=True, repro_passed=True, tests_passed=True, notes="repro exits 0, tests green")
FAILS_REPRO = VerifyResult(verified=True, repro_passed=False, tests_passed=None, notes="repro still exits 1")
BREAKS = VerifyResult(verified=True, repro_passed=True, tests_passed=False,
                      broken_tests=["TestGetChannelStats", "TestChannelMemberCountGuests"])


def causes(*statuses: str) -> list[Cause]:
    texts = ["Member count query excludes the requesting user",
             "Webapp header subtracts deactivated users twice",
             "Channel stats cache isn't invalidated on join"]
    return [Cause(id=f"C{i}", description=texts[(i - 1) % 3], location=f"file{i}.go:fn{i}:10-20",
                  confidence=round(0.7 - 0.2 * (i - 1), 2), status=s,
                  evidence=[f"[diagnosis] read file{i}.go"] if s != "untested" else [])
            for i, s in enumerate(statuses, 1)]


def out(updates=(), patch=None, target=None, new_evidence=True, next_step="", notes="") -> StepOutput:
    return StepOutput(cause_updates=list(updates), proposed_patch=patch, target_cause=target,
                      new_evidence=new_evidence, next_step=next_step, notes=notes)


class SequenceVerifier:
    """Gives the recorded results in order (first fix, second fix, ...)."""
    can_verify = True

    def __init__(self, results: list[VerifyResult]):
        self.results = list(results)

    def verify(self, patch: str, target_cause: str | None) -> VerifyResult:
        return self.results.pop(0) if self.results else FAILS_REPRO


@dataclass
class Calls:
    """Counts how often each fake step ran (the tests check the limits with this)."""
    counts: dict = field(default_factory=dict)

    def wrap(self, name: str, fn: Callable) -> Callable:
        def wrapped(*args):
            self.counts[name] = self.counts.get(name, 0) + 1
            return fn(*args)
        return wrapped


def notification_catalog(eta_hours: float = 0.0) -> AccessCatalog:
    catalog = AccessCatalog()
    catalog.add_reader("notification_service_logs", AccessKind.LOGS,
                       "Logs from the notification service the channel code calls",
                       lambda grep=None: "WARN notify: dropped event channel_member_added (queue full)")
    catalog.entries["notification_service_logs"].owner_team = "Notifications"
    catalog.entries["notification_service_logs"].approval_eta_hours = eta_hours
    return catalog


def draft(resource="notification_service_logs", target="C3") -> AccessRequestDraft:
    return AccessRequestDraft(
        resource=resource, target_cause=target,
        ruled_out="C1 and C2 in the channel module, each with the code read (see causes).",
        points_to="The count goes stale right after a call to the notification service, which can't be read.",
        expects_to_find="Whether member-added events are dropped silently under load.",
        how_to_test="Trace the event for the repro's join, then rerun the repro in the sandbox.")


@dataclass
class Scenario:
    name: str
    about: str
    case: CaseInput
    ports: Ports
    calls: Calls
    clock: Callable[[], float] | None = None


def _case(**kw) -> CaseInput:
    base = dict(ticket_id=2, priority="P3", reproduced=True,
                problem_summary="Channel header member count is always one lower than the member list.",
                repro="Reproduced on Mattermost 10.5.0. Script exits 1 while the bug is present.",
                causes=causes("untested", "untested"), initial_patch=PATCH.format("first"),
                initial_target="C1", baseline_access=["Source tree, read-only (Agent B)"],
                budget_minutes=120.0, started_at=None)  # None: the engine starts the clock itself (0.0 read as hours overdue)
    base.update(kw)
    return CaseInput(**base)


def scenarios() -> dict[str, Scenario]:
    s: dict[str, Scenario] = {}

    def add(name, about, case, calls=None, clock=None, **ports):
        calls = calls or Calls()
        for key in ("reviewer", "recovery", "planner", "investigator", "admin_questions"):
            if key in ports and ports[key] is not None:
                ports[key] = calls.wrap(key, ports[key])
        ports.setdefault("verifier", RecordedVerifier({}))
        ports.setdefault("approver", StaticApprover("approved", "demo engineer"))
        s[name] = Scenario(name, about, case, Ports(**ports), calls, clock)

    add("fixed_first_try", "Agent B's fix passes the repro and the tests: nothing to troubleshoot.",
        _case(), verifier=RecordedVerifier({"C1": PASS}))

    add("recovery_finds_regression", "Reason 1: nothing confirmed. The self-recovery pass finds a recent commit and fixes it.",
        _case(causes=causes("ruled_out", "ruled_out")),
        verifier=RecordedVerifier({"C1": FAILS_REPRO, "C3": PASS}),
        recovery=lambda ctx: StepResult(out([CauseUpdate(
            description="Commit a1b2c3 changed the stats query to exclude the caller", status="confirmed",
            evidence="recent_commits: a1b2c3 'exclude bots from member count' touched GetMemberCount",
            confidence=0.8)], patch=PATCH.format("revert a1b2c3"), target="C3"),
            ["source tree: recent_commits server/channels/store"]))

    add("access_denied", "Reason 1: recovery finds nothing new, the one access request is denied, so a human takes it.",
        _case(causes=causes("ruled_out", "ruled_out")), catalog=notification_catalog(),
        verifier=RecordedVerifier({"C1": FAILS_REPRO}), approver=StaticApprover("denied", "case engineer"),
        recovery=lambda ctx: StepResult(out([CauseUpdate(
            description="Notification service drops member-added events", status="untested",
            access_needed="notification service logs", confidence=0.5)], new_evidence=False)),
        planner=lambda ctx, cat: draft(),
        investigator=lambda ctx, req, tools: StepResult(out()))

    add("access_no_new_evidence", "Reason 1: access approved, but it shows nothing new, so stop.",
        _case(causes=causes("ruled_out", "ruled_out")), catalog=notification_catalog(),
        verifier=RecordedVerifier({"C1": FAILS_REPRO}),
        recovery=lambda ctx: StepResult(out([CauseUpdate(
            description="Notification service drops member-added events", confidence=0.5)], new_evidence=False)),
        planner=lambda ctx, cat: draft(),
        investigator=lambda ctx, req, tools: StepResult(
            out(new_evidence=False, next_step="Check the cache layer"), [f"granted: {t.name}" for t in tools]))

    add("access_finds_fix", "Reason 1: access approved, the agent confirms the cause and its fix passes.",
        _case(causes=causes("ruled_out", "ruled_out")), catalog=notification_catalog(),
        verifier=RecordedVerifier({"C1": FAILS_REPRO, "C3": PASS}),
        recovery=lambda ctx: StepResult(out([CauseUpdate(
            description="Notification service drops member-added events", confidence=0.5)], new_evidence=False)),
        planner=lambda ctx, cat: draft(),
        investigator=lambda ctx, req, tools: StepResult(out(
            [CauseUpdate(id="C3", status="confirmed", evidence=tools[0].fn())],
            patch=PATCH.format("retry dropped events"), target="C3")))

    add("access_too_slow", "Reason 1: the access belongs to another team and approval takes days. Skip it and say what's needed.",
        _case(causes=causes("ruled_out", "ruled_out")), catalog=notification_catalog(eta_hours=72),
        verifier=RecordedVerifier({"C1": FAILS_REPRO}),
        recovery=lambda ctx: StepResult(out([CauseUpdate(
            description="Notification service drops member-added events", confidence=0.5)], new_evidence=False)),
        planner=lambda ctx, cat: draft(),
        investigator=lambda ctx, req, tools: StepResult(out()))

    add("retry_then_handoff", "Reason 2: cause confirmed, the fix and the one Reviewer retry both fail the repro.",
        _case(causes=causes("confirmed", "untested")), verifier=RecordedVerifier({"C1": FAILS_REPRO}),
        reviewer=lambda ctx: StepResult(out(patch=PATCH.format("second try"), target="C1",
                                            notes="first fix only patched the API layer")))

    add("retry_fixes_it", "Reason 2: the Reviewer retry produces a fix that passes.",
        _case(causes=causes("confirmed", "untested")),
        verifier=SequenceVerifier([FAILS_REPRO, PASS]),
        reviewer=lambda ctx: StepResult(out(patch=PATCH.format("second try"), target="C1")))

    add("breaks_tests", "Reason 3: the fix passes the repro but breaks other tests. Straight to a human.",
        _case(causes=causes("confirmed")), verifier=RecordedVerifier({"C1": BREAKS}),
        reviewer=lambda ctx: StepResult(out()))

    add("cant_reproduce", "Reason 4: not reproduced. Ask the admin specific questions and wait.",
        _case(reproduced=False, causes=[], initial_patch=None, initial_target=None,
              repro="Could not be reproduced: no attempt triggered the bug."),
        admin_questions=lambda: ["Does the count look wrong for every user, or only for guests?",
                                 "Were any members added through an AD/LDAP group sync?"])

    add("cant_reproduce_again", "Reason 4, second time: the admin was already asked once, so hand off.",
        _case(reproduced=False, causes=[], initial_patch=None, initial_target=None,
              repro="Could not be reproduced after the admin's answers.",
              history=CaseHistory(admin_rounds_used=1)),
        admin_questions=lambda: ["unused"])

    add("urgent_p1", "Reason 5: P1. An engineer is involved from the start; the agent does nothing on its own.",
        _case(priority="P1", causes=causes("ruled_out")), verifier=RecordedVerifier({"C1": FAILS_REPRO}),
        recovery=lambda ctx: StepResult(out()))

    now = [0.0]

    def slow_recovery(ctx):
        now[0] += 3 * 60 * 60          # the pass takes 3 hours; the P3 budget is 2
        return StepResult(out(new_evidence=False))
    add("out_of_time", "Reason 5: the P3 time budget runs out mid-case.",
        _case(causes=causes("ruled_out", "ruled_out")), verifier=RecordedVerifier({"C1": FAILS_REPRO}),
        catalog=notification_catalog(), clock=lambda: now[0], recovery=slow_recovery,
        planner=lambda ctx, cat: draft())

    add("no_verifier", "Reason 2 with no patch verifier: a retry can't prove anything, so hand off with the unverified patch.",
        _case(causes=causes("confirmed")), verifier=NoVerifier(), reviewer=lambda ctx: StepResult(out()))

    always_more = lambda ctx: StepResult(out([CauseUpdate(  # noqa: E731
        description=f"Yet another idea #{len(ctx.causes)}", confidence=0.4)], new_evidence=True))
    add("never_loops", "An agent that always claims progress still gets one of each step, then a human.",
        _case(causes=causes("ruled_out")), catalog=notification_catalog(),
        verifier=RecordedVerifier({}), recovery=always_more, reviewer=always_more,
        planner=lambda ctx, cat: draft(target=None),
        investigator=lambda ctx, req, tools: always_more(ctx))
    return s


def run(sc: Scenario, run_dir: Path | None = None, quiet: bool = True) -> TroubleshootResult:
    kwargs = {"clock": sc.clock} if sc.clock else {}
    return Engine(sc.case, sc.ports, run_dir=run_dir, log=(lambda m: None) if quiet else print, **kwargs).run()


def main() -> None:
    all_s = scenarios()
    if len(sys.argv) > 1:
        sc = all_s[sys.argv[1]]
        print(f"# {sc.name}: {sc.about}\n")
        res = run(sc, quiet=False)
        print("\n" + render_markdown(res.case_file))
        print("--- What the admin sees ---\n" + res.admin_status)
        return
    for sc in all_s.values():
        res = run(sc)
        reason = res.failure_reason.value if res.failure_reason else "-"
        print(f"{sc.name:26} {res.outcome:17} {reason:22} {res.stop_rule or ''}")
    print("\nRun one in full: python -m recreator.troubleshooter.demo <name>")


if __name__ == "__main__":
    main()
