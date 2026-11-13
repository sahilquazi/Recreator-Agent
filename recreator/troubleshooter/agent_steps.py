"""
agent_steps.py - The Claude-backed steps the engine can call.

Same isolation as the rest of the pipeline:
  * Code-side steps (Reviewer retry, self-recovery, access planning, access
    investigation) get the Handoff (technical findings) and never the ticket text.
  * The admin questions are written on the Recreator side, from the scrubbed
    ticket, triage and repro attempts, and never see source code.
Every step runs on the same tool-use loop (llm.run_agent) with read-only tools.
"""
import json
from typing import Callable

import anthropic

from ..agents import DIAGNOSIS_SYSTEM, UNTRUSTED, _ticket_block
from ..llm import Tool, run_agent, structured_call
from ..schemas import Handoff, ReproReport, Ticket, TriageResult
from .access import AccessCatalog
from .engine import StepContext, StepResult
from .models import AccessRequest, AccessRequestDraft, StepOutput

EVIDENCE_RULES = """
Reporting rules:
- In cause_updates, refer to existing causes by id (C1, C2 ...). Use id null for a new cause.
- Only mark a cause confirmed or ruled_out if you can give the evidence (code you read, a commit, a log line).
  A status change without evidence is ignored.
- A proposed_patch must be a unified diff for ONE cause (set target_cause). It will be applied in the
  sandbox; it only counts if the repro passes AND the existing tests still pass.
- Set new_evidence to true only if you confirmed or ruled out at least one cause.
- If something you'd need is outside what you can read, say so in that cause's access_needed."""


def _case_block(handoff: Handoff, ctx: StepContext) -> str:
    causes = [c.model_dump(include={"id", "description", "location", "confidence", "status", "evidence",
                                    "access_needed"}) for c in ctx.causes]
    fixes = [{"id": f.id, "by": f.by, "target_cause": f.target_cause, "result": f.why_failed(),
              "notes": f.notes, "patch": f.patch[:4000]} for f in ctx.fixes]
    return (f"<findings>\n{handoff.model_dump_json(indent=1)}\n</findings>\n\n"
            f"<why_troubleshooting>{ctx.why}</why_troubleshooting>\n\n"
            f"<causes>\n{json.dumps(causes, indent=1)}\n</causes>\n\n"
            f"<fixes_tried>\n{json.dumps(fixes, indent=1)}\n</fixes_tried>")


class _ReadTracker:
    """Wraps tools to record which files/paths a step looked at (for the case file's 'access used')."""
    def __init__(self, label: str):
        self.label, self.reads = label, []

    def wrap(self, tools: list[Tool]) -> list[Tool]:
        def track(tool: Tool) -> Tool:
            def fn(**kwargs):
                target = kwargs.get("path") or kwargs.get("pattern") or kwargs.get("grep") or ""
                entry = f"{self.label}: {tool.name} {target}".strip()
                if entry not in self.reads:
                    self.reads.append(entry)
                return tool.fn(**kwargs)
            return Tool(tool.name, tool.description, tool.input_schema, fn)
        return [track(t) for t in tools]


# ---------------------------------------------------------------- Reviewer retry (reason 2)
REVIEWER_RETRY_SYSTEM = DIAGNOSIS_SYSTEM + """

This is your ONE retry. A cause is confirmed but the fix that was tried did not make the repro pass
(see fixes_tried and their notes). Work out why that fix fell short, then propose ONE different,
minimal patch for the confirmed cause. Don't resubmit a patch that was already tried.""" + EVIDENCE_RULES


def make_reviewer(client: anthropic.Anthropic, model: str, handoff: Handoff,
                  tools: Callable[[], list[Tool]], max_turns: int):
    def reviewer(ctx: StepContext) -> StepResult:
        tracker = _ReadTracker("source tree (read-only)")
        out = run_agent(client=client, model=model, system=REVIEWER_RETRY_SYSTEM,
                        task=_case_block(handoff, ctx) + "\n\nPropose a different fix.",
                        tools=tracker.wrap(tools()), submit_name="submit_retry",
                        submit_description="Submit your retry result. Call once, at the end.",
                        result_model=StepOutput, max_turns=max_turns)
        return StepResult(out, tracker.reads)
    return reviewer


# ---------------------------------------------------------------- self-recovery pass (reason 1)
SELF_RECOVERY_SYSTEM = """You are a senior engineer taking ONE more pass at a bug whose likely causes were all
ruled out (or none could be confirmed). You only have the access you already had: the read-only source tree.
Work through these, in order:
1. Re-rank the remaining guesses using what was ruled out. What does the evidence ACTUALLY rule out,
   and what does it leave open? Add new causes if the ruled-out ones point somewhere new.
2. Regressions: use recent_commits (and changed_files/diff_file against the last good version, if one is
   given) on the files along the call path. Many bugs are recent changes.
3. Near-misses: similar past cases from the pattern agent (if any are listed) may give new leads.
If you confirm a cause, propose a minimal patch for it.
If the evidence points OUTSIDE the source tree you can read (another service, logs, tenant config,
deploy history), say exactly where in that cause's access_needed. That feeds the access request.""" + EVIDENCE_RULES


def make_self_recovery(client: anthropic.Anthropic, model: str, handoff: Handoff,
                       tools: Callable[[], list[Tool]], max_turns: int,
                       near_misses: Callable[[], list[dict]] = lambda: []):
    def recovery(ctx: StepContext) -> StepResult:
        tracker = _ReadTracker("source tree (read-only)")
        misses = near_misses() or []
        task = (_case_block(handoff, ctx)
                + f"\n\n<near_misses>\n{json.dumps(misses, indent=1) if misses else 'None available.'}\n</near_misses>"
                + "\n\nRun the self-recovery pass.")
        out = run_agent(client=client, model=model, system=SELF_RECOVERY_SYSTEM, task=task,
                        tools=tracker.wrap(tools()), submit_name="submit_recovery",
                        submit_description="Submit the self-recovery result. Call once, at the end.",
                        result_model=StepOutput, max_turns=max_turns)
        return StepResult(out, tracker.reads)
    return recovery


# ---------------------------------------------------------------- access request (reason 1, after recovery)
ACCESS_PLANNER_SYSTEM = """You write the ONE access request a troubleshooting agent gets per case.
Everything inside the approved scope has been ruled out, so the cause is probably outside it.
Pick the single catalog resource the evidence points to most strongly. You may only pick from the catalog;
all access is read-only, for this case only, and expires automatically. Never ask for write access,
production changes, or any other customer's data.
If nothing in the catalog fits, leave resource empty and describe the access you'd need in unlisted_need.
Be specific and evidence-based: the engineer approving this decides from these fields alone."""


def make_access_planner(client: anthropic.Anthropic, model: str, handoff: Handoff):
    def planner(ctx: StepContext, catalog: AccessCatalog) -> AccessRequestDraft | None:
        task = (_case_block(handoff, ctx) + f"\n\n<catalog>\n{catalog.describe()}\n</catalog>\n\n"
                "Write the access request.")
        return structured_call(client=client, model=model, system=ACCESS_PLANNER_SYSTEM, task=task,
                               submit_name="submit_access_request", result_model=AccessRequestDraft)
    return planner


ACCESS_INVESTIGATOR_SYSTEM = """You were granted ONE piece of read-only access for this case (tools starting
with granted_). Use it to test the ONE cause the access request names: confirm it or rule it out, with evidence.
You still have the read-only source tree. The access expires when you submit.
If you confirm the cause, propose a minimal patch. If the cause is ruled out, mark it ruled_out with the evidence.""" + EVIDENCE_RULES


def make_access_investigator(client: anthropic.Anthropic, model: str, handoff: Handoff,
                             tools: Callable[[], list[Tool]], max_turns: int):
    def investigator(ctx: StepContext, request: AccessRequest, granted: list[Tool]) -> StepResult:
        tracker = _ReadTracker("source tree (read-only)")
        granted_tracker = _ReadTracker(f"granted '{request.resource}' (read-only)")
        task = (_case_block(handoff, ctx)
                + f"\n\n<access_request>\n{request.model_dump_json(indent=1)}\n</access_request>\n\n"
                "Test the cause the request names.")
        out = run_agent(client=client, model=model, system=ACCESS_INVESTIGATOR_SYSTEM, task=task,
                        tools=tracker.wrap(tools()) + granted_tracker.wrap(granted),
                        submit_name="submit_investigation",
                        submit_description="Submit what the new access showed. Call once, at the end.",
                        result_model=StepOutput, max_turns=max_turns)
        return StepResult(out, tracker.reads + granted_tracker.reads)
    return investigator


# ---------------------------------------------------------------- admin questions (reason 4, Recreator side)
ADMIN_QUESTIONS_SYSTEM = f"""You help a QA engineer who could not reproduce a customer's bug.
{UNTRUSTED}
Write 2 to 5 SPECIFIC questions for the customer's admin that would unblock the reproduction. Base them on what
was tried and what didn't trigger the bug: ask for the one detail that separates their setup from the sandbox
(exact steps, settings, roles, versions, timing, which users are affected). No generic questions, no jargon,
no internal details. Keep using placeholders like user_1; never ask for passwords or other customers' data."""


def make_admin_questions(client: anthropic.Anthropic, model: str, ticket: Ticket, plan: TriageResult,
                         report: ReproReport | None):
    from pydantic import BaseModel

    class Questions(BaseModel):
        questions: list[str]

    def questions() -> list[str]:
        attempts = report.model_dump(include={"status", "attempts", "pattern", "observed"}) if report else {}
        task = (f"{_ticket_block(ticket)}\n\n<triage>\n{plan.model_dump_json(indent=1)}\n</triage>\n\n"
                f"<repro_attempts>\n{json.dumps(attempts, indent=1)}\n</repro_attempts>\n\nWrite the questions.")
        try:
            got = structured_call(client=client, model=model, system=ADMIN_QUESTIONS_SYSTEM, task=task,
                                  submit_name="submit_questions", result_model=Questions).questions
        except Exception:  # noqa: BLE001 - fall back to triage's questions rather than fail the case
            got = []
        return got or plan.follow_up_questions or [f"Could you tell us more about: {m}?" for m in plan.missing_info]
    return questions
