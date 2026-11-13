"""
Tests for the troubleshooter. No Claude, Docker or Supabase needed:

    pip install pytest && python -m pytest tests -q

They check the spec's P0s: every failed run gets one of the five reasons and
follows its route; at most one self-recovery pass, one access request and one
Reviewer retry per case, with the stop rules enforced; every handoff has all
eight case file sections. Plus the safety rules around access.
"""
import json
import sys
import textwrap

import pytest

from recreator.schemas import Diagnosis, ReproReport, Suspect, Ticket, TriageResult
from recreator.troubleshooter import demo
from recreator.troubleshooter.access import AccessCatalog, Grant, GrantExpired
from recreator.troubleshooter.budget import CaseHistory, priority_for
from recreator.troubleshooter.case_file import render_markdown
from recreator.troubleshooter.classify import Situation, classify
from recreator.troubleshooter.config import TroubleshootSettings
from recreator.troubleshooter.engine import Engine, Ports, StepResult
from recreator.troubleshooter.models import AccessKind, CauseUpdate, FailureReason, FixAttempt
from recreator.troubleshooter.runner import build_case_input, troubleshoot
from recreator.troubleshooter.verifier import CommandVerifier, RecordedVerifier

R = FailureReason
SCENARIOS = demo.scenarios()


def run(name, tmp_path=None):
    sc = demo.scenarios()[name]           # fresh fakes and counters every time
    return demo.run(sc, run_dir=tmp_path), sc.calls.counts


# ---------------------------------------------------------------- routing (P0)
EXPECTED = {
    "fixed_first_try": ("fixed", None),
    "recovery_finds_regression": ("fixed", None),
    "access_denied": ("handed_off", R.ALL_CAUSES_RULED_OUT),
    "access_no_new_evidence": ("handed_off", R.ALL_CAUSES_RULED_OUT),
    "access_finds_fix": ("fixed", None),
    "access_too_slow": ("handed_off", R.ALL_CAUSES_RULED_OUT),
    "retry_then_handoff": ("handed_off", R.FIX_FAILS_REPRO),
    "retry_fixes_it": ("fixed", None),
    "breaks_tests": ("handed_off", R.FIX_BREAKS_TESTS),
    "cant_reproduce": ("waiting_on_admin", R.CANT_REPRODUCE),
    "cant_reproduce_again": ("handed_off", R.CANT_REPRODUCE),
    "urgent_p1": ("handed_off", R.OUT_OF_TIME_OR_URGENT),
    "out_of_time": ("handed_off", R.OUT_OF_TIME_OR_URGENT),
    "no_verifier": ("handed_off", R.FIX_FAILS_REPRO),
    "never_loops": ("handed_off", R.ALL_CAUSES_RULED_OUT),
}


def test_every_scenario_has_an_expectation():
    assert set(EXPECTED) == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_route(name):
    res, _ = run(name)
    assert (res.outcome, res.failure_reason) == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_limits_are_never_exceeded(name):
    res, calls = run(name)
    assert all(n <= 1 for n in calls.values()), calls
    assert all(v <= 1 for v in res.budget_used.values()), res.budget_used


@pytest.mark.parametrize("name", [n for n, (o, _) in EXPECTED.items() if o == "handed_off"])
def test_every_handoff_has_all_eight_sections(name, tmp_path):
    res, _ = run(name, tmp_path)
    assert res.case_file.missing_sections() == []
    md = (tmp_path / "case_file.md").read_text()
    for n, title in enumerate(["Problem summary", "Repro", "Failure reason", "Causes tested", "Access used",
                               "Fixes tried", "Remaining guesses", "Recommended next step"], 1):
        assert f"## {n}. {title}" in md
    saved = json.loads((tmp_path / "case_file.json").read_text())
    for key in ["problem_summary", "repro", "failure_reason", "causes_tested", "access_used",
                "fixes_tried", "remaining_guesses", "recommended_next_step"]:
        assert key in saved


# ---------------------------------------------------------------- specific routes
def test_breaks_tests_goes_straight_to_a_human():
    res, calls = run("breaks_tests")
    assert "reviewer" not in calls
    assert "TestGetChannelStats" in res.case_file.recommended_next_step


def test_urgent_case_does_nothing_on_its_own():
    res, calls = run("urgent_p1")
    assert calls == {} and res.engineer_involved_from_start


def test_no_verifier_never_counts_a_fix_and_skips_the_retry():
    res, calls = run("no_verifier")
    assert "reviewer" not in calls
    assert res.winning_fix is None and not res.case_file.fixes_tried[0].verified


def test_reviewer_retry_runs_once_then_hands_off():
    res, calls = run("retry_then_handoff")
    assert calls == {"reviewer": 1}
    assert [f.by for f in res.case_file.fixes_tried] == ["diagnosis", "reviewer_retry"]


def test_recovery_then_access_order():
    res, calls = run("access_denied")
    assert calls == {"recovery": 1, "planner": 1}          # denied: investigator never runs
    assert res.case_file.access_request.decision == "denied"
    assert "denial is final" in res.stop_rule


def test_too_slow_lists_the_access_needed():
    res, calls = run("access_too_slow")
    assert "investigator" not in calls
    top = res.case_file.remaining_guesses[0]
    assert "notification_service_logs" in top.access_needed
    assert "notification_service_logs" in res.case_file.recommended_next_step


def test_access_with_no_new_evidence_stops():
    res, calls = run("access_no_new_evidence")
    assert calls["investigator"] == 1
    assert "no new evidence" in res.stop_rule
    assert any("granted" in line for line in res.case_file.access_used)


def test_cant_reproduce_asks_specific_questions():
    res, _ = run("cant_reproduce")
    assert len(res.admin_questions) == 2
    assert res.admin_status.startswith("We need a few details to reproduce this")


def test_admin_never_sees_failure_language():
    for name in EXPECTED:
        res, _ = run(name)
        for msg in [res.admin_status, *res.admin_status_log]:
            assert "fail" not in msg.lower() and "agent" not in msg.lower(), (name, msg)


# ---------------------------------------------------------------- classification
def fix(**kw):
    base = dict(id="F1", by="diagnosis", patch="x", verified=True)
    base.update(kw)
    return FixAttempt(**base)


def test_classify_takes_the_most_cautious_reason():
    s = Situation(priority="P1", out_of_time=False, reproduced=False)
    c = classify(s)
    assert c.reason is R.OUT_OF_TIME_OR_URGENT and R.CANT_REPRODUCE in c.also_matched


def test_breaking_tests_beats_retry():
    s = Situation(priority="P3", out_of_time=False, reproduced=True,
                  causes=demo.causes("confirmed"), fixes=[fix(repro_passed=True, tests_passed=False)])
    c = classify(s)
    assert c.reason is R.FIX_BREAKS_TESTS and R.FIX_FAILS_REPRO in c.also_matched


def test_a_passing_fix_means_nothing_to_troubleshoot():
    s = Situation(priority="P1", out_of_time=True, reproduced=True,
                  fixes=[fix(repro_passed=True, tests_passed=True)])
    assert classify(s).reason is None


def test_unverified_fix_never_counts():
    assert not fix(verified=False, repro_passed=True, tests_passed=True).counts


# ---------------------------------------------------------------- per-case history
def test_history_carries_limits_across_runs():
    prior = [{"outcome": "waiting_on_admin", "budget_used": {"self_recovery": 1, "access_request": 1},
              "case_file": {"access_request": {"decision": "denied", "resource": "notification_service_logs"}}}]
    h = CaseHistory.from_prior_results(prior)
    assert (h.self_recovery_used, h.access_requests_used, h.admin_rounds_used) == (1, 1, 1)
    assert h.denied_resources == {"notification_service_logs"}

    sc = demo.scenarios()["access_denied"]
    sc.case.history = h
    res = demo.run(sc)
    assert sc.calls.counts == {} and res.outcome == "handed_off"


def test_denied_resource_is_never_asked_again():
    sc = demo.scenarios()["access_finds_fix"]
    sc.case.history = CaseHistory(denied_resources={"notification_service_logs"})
    res = demo.run(sc)
    assert "investigator" not in sc.calls.counts
    assert res.case_file.access_request.decision == "denied"


def test_status_change_without_evidence_is_ignored():
    sc = demo.scenarios()["access_denied"]
    sc.ports.recovery = lambda ctx: StepResult(demo.out([CauseUpdate(id="C1", status="confirmed")]))
    res = demo.run(sc)
    assert res.case_file.causes[0].status == "ruled_out"


# ---------------------------------------------------------------- access safety
def test_grant_is_single_case_and_expires():
    cat = demo.notification_catalog()
    grant = Grant(case_id=2, entry=cat.entries["notification_service_logs"], ttl_minutes=30)
    tool = grant.tools(cat)[0]
    assert "dropped" in tool.fn()
    grant.revoke()
    with pytest.raises(GrantExpired):
        tool.fn()
    with pytest.raises(GrantExpired):
        Grant(case_id=2, entry=cat.entries["notification_service_logs"], ttl_minutes=30).check(case_id=3)


def test_unlisted_resource_is_not_granted():
    sc = demo.scenarios()["access_finds_fix"]
    sc.ports.planner = lambda ctx, cat: demo.draft(resource="prod_database")
    res = demo.run(sc)
    assert "investigator" not in sc.calls.counts
    assert res.case_file.access_request.decision == "not_available"


def test_access_kinds_are_read_only():
    assert {k.value for k in AccessKind} == {"source_module", "logs", "tenant_config", "deploy_history"}


def test_request_has_all_six_fields():
    res, _ = run("access_no_new_evidence")
    req = res.case_file.access_request
    assert req.missing_fields() == [] and req.decision == "approved" and req.expires_at


def test_catalog_file(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"notifications": {"kind": "source_module", "description": "d",
                                                  "path": str(tmp_path), "owner_team": "Notif"}}))
    entry = AccessCatalog.load(str(path)).entries["notifications"]
    assert entry.cross_team and entry.kind is AccessKind.SOURCE_MODULE


# ---------------------------------------------------------------- case file privacy
def test_case_file_is_withheld_if_a_real_value_leaks(tmp_path):
    sc = demo.scenarios()["breaks_tests"]
    Engine(sc.case, sc.ports, run_dir=tmp_path, leak_check=lambda text: ["alice@contoso.com"],
           log=lambda m: None).run()
    assert "withheld" in (tmp_path / "case_file.md").read_text()


# ---------------------------------------------------------------- verifier
def test_command_verifier(tmp_path):
    script = tmp_path / "verify.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        patch, repro = open(sys.argv[1]).read(), open(sys.argv[2]).read()
        print(json.dumps({"repro_passed": "good" in patch, "tests_passed": False, "broken_tests": ["T1"]}))
    """))
    v = CommandVerifier(f"{sys.executable} {script}", repro_script="print(1)")
    r = v.verify("good patch", "C1")
    assert (r.verified, r.repro_passed, r.tests_passed, r.broken_tests) == (True, True, False, ["T1"])
    assert not CommandVerifier("x", repro_script=None).verify("p", None).verified


# ---------------------------------------------------------------- glue with the real pipeline objects
def pipeline_objects(reproduced=True):
    ticket = Ticket(id=2, title="Wrong count", severity="Low",
                    app_build_details={"app": "Mattermost", "version": "10.5.0"})
    plan = TriageResult(claim="Member count is one short", expected="4", actual="3",
                        environment={"users": [], "structure": [], "config": []}, repro_plan=[],
                        variations_to_try=[], hypotheses=[], missing_info=[],
                        follow_up_questions=["Which channels?"])
    report = ReproReport(status="reproduced" if reproduced else "not_reproduced", location="header",
                         trigger="open channel", expected="4", observed="3", layer="server", pattern="always -1",
                         attempts=["tried as admin"], confidence=0.9, repro_script="import sys; sys.exit(1)",
                         verified_on_current=reproduced)
    diag = Diagnosis(summary="s", call_path=[], proposed_patch="diff",
                     suspects=[Suspect(file="a.go", function="f", lines="1-2", confidence=0.8, explanation="e",
                                       status="confirmed", evidence="read a.go")])
    return ticket, plan, report, diag


def test_build_case_input_from_pipeline_objects():
    ticket, plan, report, diag = pipeline_objects()
    case = build_case_input(ticket, plan, report, diag, None, TroubleshootSettings(), 0.0, CaseHistory())
    assert case.priority == "P3" and case.reproduced and case.budget_minutes == 120
    assert case.causes[0].status == "confirmed" and case.initial_target == "C1"


def test_troubleshoot_end_to_end_with_fake_ports(tmp_path):
    ticket, plan, report, diag = pipeline_objects()

    class FakeStore:  # history lookup fails like an un-migrated database would
        @property
        def db(self):
            raise RuntimeError("column troubleshooting does not exist")
        runs = "recreator_runs"

    ports = Ports(verifier=RecordedVerifier({"C1": demo.BREAKS}), approver=demo.StaticApprover("denied"))
    res = troubleshoot(client=None, model="m", ticket=ticket, plan=plan, report=report, handoff=None,
                       diagnosis=diag, run_dir=tmp_path, store=FakeStore(), ports=ports)
    assert res.outcome == "handed_off" and res.failure_reason is R.FIX_BREAKS_TESTS
    assert json.loads(res.model_dump_json())["case_file"]["failure_reason"] == "fix_breaks_tests"
    assert "Member count is one short" in render_markdown(res.case_file)


def test_old_diagnosis_results_still_load():
    s = Suspect(file="a", function="f", lines="1", confidence=0.5, explanation="e")
    assert s.status == "untested" and s.evidence == ""


@pytest.mark.parametrize("severity,expected", [("Low", "P3"), ("high", "P1"), ("P0", "P0"),
                                                ("Critical", "P0"), ("", "P2"), ("weird", "P2")])
def test_priority_for(severity, expected):
    assert priority_for(severity) == expected


# ---------------------------------------------------------------- Claude-backed steps (fake client)
class _Block:
    def __init__(self, name, data):
        self.type, self.name, self.input, self.id = "tool_use", name, data, "t1"


class FakeClaude:
    """Answers every request by calling the submit tool with a canned payload."""
    def __init__(self, payload):
        self.payload, self.seen = payload, []
        self.messages = self

    def create(self, **kw):
        self.seen.append(kw)
        submit = kw["tools"][-1]["name"]
        return type("R", (), {"stop_reason": "tool_use", "content": [_Block(submit, self.payload)]})()


def test_agent_steps_run_on_the_real_loop(tmp_path):
    from recreator.agents import build_handoff
    from recreator.troubleshooter import agent_steps
    from recreator.troubleshooter.engine import StepContext

    ticket, plan, report, diag = pipeline_objects()
    handoff = build_handoff(ticket, plan, report)
    ctx = StepContext(R.FIX_FAILS_REPRO, "why", demo.causes("confirmed"), [])
    client = FakeClaude({"cause_updates": [{"id": "C1", "status": "confirmed", "evidence": "read it"}],
                         "proposed_patch": "diff", "target_cause": "C1", "new_evidence": True})
    tools = lambda: []  # noqa: E731
    for make in (agent_steps.make_reviewer, agent_steps.make_self_recovery):
        result = make(client, "m", handoff, tools, 3)(ctx)
        assert result.output.target_cause == "C1"
    # Code-side steps never see the customer's ticket text.
    assert all(ticket.title not in json.dumps(c["messages"], default=str) for c in client.seen)

    planner_client = FakeClaude({"resource": "x", "ruled_out": "a", "points_to": "b",
                                 "expects_to_find": "c", "how_to_test": "d"})
    assert agent_steps.make_access_planner(planner_client, "m", handoff)(ctx, AccessCatalog()).resource == "x"

    q_client = FakeClaude({"questions": ["Which channels?"]})
    assert agent_steps.make_admin_questions(q_client, "m", ticket, plan, report)() == ["Which channels?"]
