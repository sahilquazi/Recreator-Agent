"""
demo.py - A Solution Summary for every troubleshooter demo scenario, with no Claude, Docker or Supabase.

    python -m recreator.solution_summary.demo                          # one line per scenario
    python -m recreator.solution_summary.demo access_finds_fix runs/demo   # that scenario's json, html and pdf

Each scenario's run is assembled the way pipeline.py builds result.json: triage, repro, handoff and diagnosis from
recreator.schemas around the troubleshooter demo's real result (no handoff or diagnosis when the bug wasn't
reproduced). Demo data, not a real run. The tests reuse these runs (tests/test_solution_summary.py).
"""
import sys
from pathlib import Path

from ..schemas import Diagnosis, Handoff, ReproReport, Suspect, TriageResult
from ..troubleshooter.demo import run as run_scenario
from ..troubleshooter.demo import scenarios
from .build import build_summary, headline, known_files, report_evidence
from .checks import validate_summary, validate_tags
from .report import save_report


def demo_result(name: str) -> dict:
    """The saved run for one troubleshooter demo scenario."""
    sc = scenarios()[name]
    case = sc.case
    ts = run_scenario(sc).model_dump(mode="json")
    triage = TriageResult(
        claim=case.problem_summary, expected="The header shows the same count as the member list.",
        actual="The header count is one lower.",
        environment={"users": ["user_1 (system admin)", "user_2 (member)"], "structure": ["One team; one channel with 3 members"], "config": ["Default server settings"]},
        repro_plan=["Create a channel with three members.", "Open the channel header.", "Compare the count with the member list."],
        variations_to_try=["Add a guest", "Deactivate a member"], hypotheses=[c.description for c in case.causes],
        missing_info=[], follow_up_questions=[])
    repro = ReproReport(
        status="reproduced" if case.reproduced else "not_reproduced",
        location="Web app > channel header > member count", trigger="Open the channel",
        expected="3 members", observed="2 members" if case.reproduced else "3 members",
        layer="server", pattern="Every channel; always exactly one short." if case.reproduced else "Not seen in any attempt.",
        attempts=["Channel with 3 members"], confidence=0.8 if case.reproduced else 0.2,
        repro_script="import sys\nsys.exit(1)  # demo: the real script checks the API's member_count\n" if case.reproduced else None,
        verified_on_current=True if case.reproduced else None)
    handoff = diagnosis = None
    if case.reproduced:
        handoff = Handoff(claim=triage.claim, app="mattermost", version="10.5.0", last_good_version=None, layer=repro.layer,
                          failing_calls=[], errors=[], pattern=repro.pattern, repro_script=repro.repro_script,
                          reproduction_verified=True, confidence=repro.confidence)
        diagnosis = Diagnosis(
            summary=case.problem_summary,
            call_path=["GET /api/v4/channels/{id}/stats", "App.GetChannelMemberCount", "SqlChannelStore.GetMemberCount"],
            suspects=[Suspect(file=c.location.split(":")[0], function=c.location.split(":")[1], lines=c.location.split(":")[2],
                              confidence=c.confidence, explanation=c.description, status=c.status, evidence="; ".join(c.evidence))
                      for c in case.causes],
            proposed_patch=case.initial_patch, caveats=[])
    return {
        "ticket_id": case.ticket_id, "status": ts["outcome"], "error": None,
        "triage": triage.model_dump(mode="json"), "repro": repro.model_dump(mode="json"),
        "handoff": handoff.model_dump(mode="json") if handoff else None,
        "diagnosis": diagnosis.model_dump(mode="json") if diagnosis else None,
        "troubleshooting": ts,
    }


def main() -> None:
    if len(sys.argv) > 1:
        name = sys.argv[1]
        out = Path(sys.argv[2] if len(sys.argv) > 2 else f"runs/solution-summary-demo-{name}")
        result = demo_result(name)
        s = build_summary(result)
        if not s:
            print(f"{name}: not reproduced, so there is no diagnosis to summarize (the case file covers it).")
            return
        for f in save_report(s, report_evidence(result, out), out, result["triage"]["claim"], known_files(result)):
            print(f"wrote {f}")
        return
    for name in scenarios():
        result = demo_result(name)
        s = build_summary(result)
        if not s:
            print(f"{name:26} -              not reproduced: nothing to summarize yet")
            continue
        ok = "checks pass" if not (validate_summary(s, known_files(result)) + validate_tags(s.tags)) else "CHECKER PROBLEMS"
        print(f"{name:26} {s.outcome:14} {ok:12} {headline(s)[len(s.problem.text) + 1:]}")
    print("\nOne scenario's report: python -m recreator.solution_summary.demo <name> [out folder]")


if __name__ == "__main__":
    main()
