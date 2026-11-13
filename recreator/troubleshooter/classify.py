"""
classify.py - Tags a failed run with ONE of the five failure reasons.

Plain rules, no AI, so the tag is predictable and testable (the "misrouted
failures" eval metric depends on this being deterministic).

If a run fits more than one reason it takes the most cautious route: handoff
wins over asking the admin, which wins over retrying. CAUTION_ORDER encodes that.
"""
from dataclasses import dataclass, field

from .models import Cause, FailureReason, FixAttempt

# Most cautious first. 5 and 3 go straight to a human; 4 waits on the admin
# (no agent retry); 2 allows one Reviewer retry; 1 allows recovery + access.
CAUTION_ORDER: list[FailureReason] = [
    FailureReason.OUT_OF_TIME_OR_URGENT,
    FailureReason.FIX_BREAKS_TESTS,
    FailureReason.CANT_REPRODUCE,
    FailureReason.FIX_FAILS_REPRO,
    FailureReason.ALL_CAUSES_RULED_OUT,
]

URGENT = {"P0", "P1"}


@dataclass
class Situation:
    """Everything classification looks at. Built fresh from the case before every step."""
    priority: str
    out_of_time: bool
    reproduced: bool
    causes: list[Cause] = field(default_factory=list)
    fixes: list[FixAttempt] = field(default_factory=list)


@dataclass
class Classification:
    reason: FailureReason | None        # None means a fix counts: nothing to troubleshoot
    also_matched: list[FailureReason]   # other reasons that applied (kept for the case file and evals)
    why: str


def classify(s: Situation) -> Classification:
    winner = next((f for f in s.fixes if f.counts), None)
    if winner:
        return Classification(None, [], f"Fix {winner.id} passes the repro and the existing tests.")

    matched: dict[FailureReason, str] = {}

    if s.priority in URGENT:
        matched[FailureReason.OUT_OF_TIME_OR_URGENT] = f"Case is {s.priority}: an engineer leads from the start."
    elif s.out_of_time:
        matched[FailureReason.OUT_OF_TIME_OR_URGENT] = f"The {s.priority} time budget ran out."

    if not s.reproduced:
        matched[FailureReason.CANT_REPRODUCE] = "The bug was not reproduced (or the repro script didn't hold up on a fresh sandbox)."
    else:
        breaks = [f for f in s.fixes if f.verified and f.repro_passed and f.tests_passed is False]
        if breaks:
            matched[FailureReason.FIX_BREAKS_TESTS] = (
                f"Fix {breaks[-1].id} makes the repro pass but breaks: {', '.join(breaks[-1].broken_tests) or 'other tests'}.")

        confirmed = [c for c in s.causes if c.status == "confirmed"]
        if confirmed:
            matched[FailureReason.FIX_FAILS_REPRO] = (
                f"Cause {confirmed[0].id} is confirmed, but no fix makes the repro pass.")
        else:
            # Spec wording is "every cause is ruled out". Untested causes count as
            # "not confirmed" here: if nothing is confirmed, the agent hasn't found the
            # cause, so the route (recovery pass, then access request) is the same.
            untested = sum(c.status == "untested" for c in s.causes)
            detail = f" ({untested} still untested)" if untested else ""
            matched[FailureReason.ALL_CAUSES_RULED_OUT] = f"No cause is confirmed{detail}."

    reason = next(r for r in CAUTION_ORDER if r in matched)
    also = [r for r in CAUTION_ORDER if r in matched and r != reason]
    return Classification(reason, also, matched[reason])
