"""
build.py - Turns one run (the `result` dict pipeline.py saves) into a Solution Summary for engineers.

Plain code, no model call: it moves and labels what the run already says, picks the fix that passed every check (or
says why none did and what happens next), and never names a cause the troubleshooter ruled out.
Python port of the portal's hnd.summarize and src/lib/recreator-run.ts (mchung042/product_gt_microsoft_fall_2026);
tests/test_solution_summary.py checks both give the same summary for the same runs.
"""
import re
from pathlib import Path

from .checks import RECREATION_TEXT
from .models import (
    Claim, Environment, Evidence, KnownGap, NextStep, Recommendation, Recreation, ReportEvidence, RootCause,
    SolutionCheck, SolutionSummary, SolutionTested, SummaryTag,
)

# The troubleshooter's bar for a fix that counts (FixAttempt.counts): the repro passes AND the existing tests pass.
RUN_CHECKS = [
    SolutionCheck(id="repro-passes", name="Repro passes with the fix", description="The Recreator's repro script exits 0 with the patch applied in the sandbox."),
    SolutionCheck(id="tests-pass", name="Existing tests still pass", description="The product's existing tests still pass with the patch applied."),
]

# Same wording as REASON_LABELS in troubleshooter/models.py, without the numbers.
REASONS = {
    "all_causes_ruled_out": "All likely causes ruled out",
    "fix_fails_repro": "Cause found, but no fix passes",
    "fix_breaks_tests": "Fix passes the repro but breaks other tests",
    "cant_reproduce": "Can't reproduce",
    "out_of_time_or_urgent": "Out of time, or P0/P1",
}

AREAS = {"client": "web app front end", "server": "server", "config": "configuration", "plugin": "integrations", "unknown": None}


def confidence_of(n: float) -> str:
    return "high" if n >= 0.8 else "medium" if n >= 0.5 else "low"


def fix_counts(f: dict) -> bool:
    return bool(f.get("verified")) and f.get("repro_passed") is True and f.get("tests_passed") is True


def _check(f: dict, v) -> str:
    return "not_run" if not f.get("verified") or v is None else "pass" if v else "fail"


def patch_files(patch: str) -> list[str]:
    """Files a unified diff changes (its +++ lines), in order."""
    return list(dict.fromkeys(re.findall(r"^\+\+\+ b/(\S+)", patch or "", re.M)))


def fix_size(patch: str) -> str:
    """small <= 20 changed lines, medium <= 100, large above."""
    changed = sum(1 for line in (patch or "").split("\n") if re.match(r"^[+-](?![+-]{2})", line))
    return "small" if changed <= 20 else "medium" if changed <= 100 else "large"


def _why(f: dict) -> str:
    broken = ", ".join(f.get("broken_tests") or []) or "unnamed tests"
    if not f.get("verified"):
        return f.get("notes") or "Not verified: no patch verifier is configured."
    if f.get("repro_passed") is False:
        return "The repro still fails with this fix."
    if f.get("tests_passed") is False:
        return f"The repro passes but existing tests break: {broken}."
    return (f.get("notes") or "Passed the repro and the existing tests.") if fix_counts(f) else "Result unknown."


def _fixes(run: dict) -> list[dict]:
    """Fixes the troubleshooter checked; without it, only Agent B's proposed patch, unchecked. Solution ids are 1-based in this order."""
    ts = run.get("troubleshooting")
    if ts:
        return ts["case_file"].get("fixes_tried") or []
    patch = (run.get("diagnosis") or {}).get("proposed_patch")
    return [{"id": "F1", "by": "diagnosis", "target_cause": None, "patch": patch, "verified": False, "repro_passed": None,
             "tests_passed": None, "broken_tests": [], "notes": "Not checked: the troubleshooter did not run."}] if patch else []


def _locate(location: str) -> tuple[str, str]:
    """'path/file.go:fn:10-20' -> (file, fn); ('', '') when the cause has no location yet."""
    parts = (location or "").split(":")
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def root_cause_of(run: dict) -> dict:
    """The cause the winning fix targets, else the first confirmed cause, else the strongest untested guess (marked
    not confirmed), else none. Without the troubleshooter: Agent B's strongest suspect not ruled out."""
    none = {"text": "No cause confirmed yet: every likely cause was ruled out.", "file": "", "fn": "", "confidence": "low", "confirmed": False, "cause_id": None}
    ts = run.get("troubleshooting")
    if ts:
        causes = ts["case_file"].get("causes") or []
        winner = next((f for f in _fixes(run) if fix_counts(f)), None)
        c = next((x for x in causes if winner and x["id"] == winner.get("target_cause") and x["status"] != "ruled_out"), None) \
            or next((x for x in causes if x["status"] == "confirmed"), None)
        if c:
            file, fn = _locate(c.get("location", ""))
            return {"text": c["description"], "file": file, "fn": fn, "confidence": confidence_of(c.get("confidence", 0)), "confirmed": True, "cause_id": c["id"]}
        untested = sorted((x for x in causes if x["status"] == "untested"), key=lambda x: -x.get("confidence", 0))
        if untested:
            g = untested[0]
            file, fn = _locate(g.get("location", ""))
            return {"text": f"Not confirmed yet. Strongest remaining guess: {g['description']}", "file": file, "fn": fn, "confidence": "low", "confirmed": False, "cause_id": g["id"]}
        return none
    s = next((x for x in (run.get("diagnosis") or {}).get("suspects", []) if x.get("status") != "ruled_out"), None)
    if s:
        return {"text": s["explanation"], "file": s["file"], "fn": s["function"], "confidence": confidence_of(s.get("confidence", 0)), "confirmed": s.get("status") == "confirmed", "cause_id": None}
    return none


def known_files(run: dict) -> list[str]:
    """Files the run knows exist: the suspects Agent B read, the causes' locations, the files the patches change."""
    ts = run.get("troubleshooting")
    causes = ts["case_file"].get("causes", []) if ts else []
    files = [s["file"] for s in (run.get("diagnosis") or {}).get("suspects", [])]
    files += [_locate(c.get("location", ""))[0] for c in causes]
    files += [p for f in _fixes(run) for p in patch_files(f.get("patch", ""))]
    return [f for f in dict.fromkeys(files) if f]


def build_summary(run: dict) -> SolutionSummary | None:
    """The Solution Summary for one run, or None when the run stopped before a diagnosis (no sandbox for the app,
    not reproduced, no source code): there is nothing to summarize yet; the case file covers it."""
    triage, repro, handoff, diagnosis = (run.get(k) for k in ("triage", "repro", "handoff", "diagnosis"))
    if not (triage and repro and handoff and diagnosis):
        return None
    root = root_cause_of(run)
    t = run["ticket_id"]
    ts = run.get("troubleshooting")
    ids = {k: f"run-{t}-{k.replace('_', '-')}" for k in ("triage", "repro", "diagnosis", "case_file")}
    evidence = [Evidence(id=ids["triage"], kind="triage", label=f"Ticket {t} triage"),
                Evidence(id=ids["repro"], kind="repro", label=f"Ticket {t} repro"),
                Evidence(id=ids["diagnosis"], kind="diagnosis", label=f"Ticket {t} diagnosis")]
    if ts:
        evidence.append(Evidence(id=ids["case_file"], kind="case_file", label=f"Ticket {t} case file"))
    fix_source = [ids["case_file"]] if ts else [ids["diagnosis"]]
    fixes = _fixes(run)
    causes = ts["case_file"].get("causes", []) if ts else []

    # ---- every fix, judged on the same two checks
    candidates = []
    for i, f in enumerate(fixes):
        cause = next((c for c in causes if c["id"] == f.get("target_cause")), None)
        files = patch_files(f.get("patch", ""))
        if not cause:
            about = ""
        elif cause["id"] == root["cause_id"] and root["confirmed"]:
            about = f", for cause {cause['id']} (the root cause)"
        else:
            about = f", for cause {cause['id']}: {cause['description']}"
        candidates.append({
            "solution": SolutionTested(
                id=i + 1, approach=f"Patch {f['id']}{about}",
                results={"repro-passes": _check(f, f.get("repro_passed")), "tests-pass": _check(f, f.get("tests_passed"))},
                overall="pass" if fix_counts(f) else "fail" if f.get("verified") else "not_run",
                why=_why(f), basis="sandbox" if f.get("verified") else "reasoned", source_ids=fix_source),
            "change": f"Apply patch {f['id']}" + (", which changes " + ", ".join(f"`{x}`" for x in files) if files else "") + ".",
        })

    # ---- what is still unknown
    gaps: list[KnownGap] = []
    if not root["confirmed"]:
        gaps.append(KnownGap(kind="inferred", text="No cause is confirmed yet, so the root cause above is not proven.", source_ids=[ids["case_file"]] if ts else [ids["diagnosis"]]))
    gaps += [KnownGap(kind="unchecked", text=c, source_ids=[ids["diagnosis"]]) for c in diagnosis.get("caveats", [])]
    gaps += [KnownGap(kind="unchecked", text=f"Untested cause {c['id']}: {c['description']}" + (f" Needs: {c['access_needed']}." if c.get("access_needed") else ""), source_ids=[ids["case_file"]])
             for c in causes if c["status"] == "untested"]
    gaps += [KnownGap(kind="not_validated", text=f"Patch {f['id']} was not run against the repro or the tests.", source_ids=fix_source) for f in fixes if not f.get("verified")]
    if repro.get("verified_on_current") is not True:
        gaps.append(KnownGap(kind="not_validated", text="The repro script was not confirmed on a fresh sandbox.", source_ids=[ids["repro"]]))
    if not handoff.get("last_good_version"):
        gaps.append(KnownGap(kind="unseen", text="No last good version is known, so the repro was not checked on a clean build.", source_ids=[ids["repro"]]))

    # ---- the recommendation: a fix that passed every check, one backed by tests that ran first
    check_ids = [c.id for c in RUN_CHECKS]
    passing = [c for c in candidates if c["solution"].overall == "pass" and all(c["solution"].results.get(k) == "pass" for k in check_ids)]
    pick = next((c for c in passing if c["solution"].basis != "reasoned"), None) or (passing[0] if passing else None)
    dx_sources = [ids["case_file"], ids["diagnosis"]] if ts else [ids["diagnosis"]]
    root_conf = root["confidence"]

    sized = next((f for f in fixes if fix_counts(f)), None) or (fixes[0] if fixes else None)
    area = AREAS.get(repro.get("layer"))
    version = handoff.get("version") or ""
    tags = ([SummaryTag(category="area", value=area)] if area else []) \
        + ([SummaryTag(category="fix_size", value=fix_size(sized.get("patch", "")))] if sized else []) \
        + ([SummaryTag(category="version", value=version)] if version else [])

    if pick:
        next_step = None
    elif ts and ts.get("outcome") != "fixed":
        next_step = NextStep(reason=REASONS.get(ts.get("failure_reason") or "", "No fix counts yet"), text=ts["case_file"].get("recommended_next_step", ""), source_ids=[ids["case_file"]])
    else:
        next_step = NextStep(reason="No candidate fix passed every check", text="An engineer reviews the fixes tried and the known gaps.", source_ids=dx_sources)

    script = repro.get("repro_script")
    return SolutionSummary(
        problem=Claim(text=diagnosis["summary"], source_ids=list(dict.fromkeys([ids["triage"], *dx_sources]))),
        environment=Environment(product=handoff.get("app", ""), version=version, surfaces=[repro.get("location", "")], source_ids=[ids["triage"]]),
        root_cause=RootCause(text=root["text"], file=root["file"], fn=root["fn"], confidence=root_conf, source_ids=dx_sources),
        checks=RUN_CHECKS,
        solutions_tested=[c["solution"] for c in candidates],
        outcome="fix_found" if pick else "no_fix",
        recommended_solution=Recommendation(solution_id=pick["solution"].id, change=pick["change"],
                                            confidence=root_conf if pick["solution"].basis != "reasoned" else "low",
                                            source_ids=pick["solution"].source_ids) if pick else None,
        next_step=next_step,
        tags=tags,
        evidence=evidence,
        known_gaps=gaps,
        recreation=Recreation(attached=True, text=RECREATION_TEXT, script=script, verified=repro.get("verified_on_current") is True)
        if script else Recreation(attached=False, text=RECREATION_TEXT),
    )


def headline(s: SolutionSummary) -> str:
    """One line: the problem, then the recommendation or why there is none."""
    if s.recommended_solution:
        passing = [x for x in s.solutions_tested if x.overall == "pass"]
        which = "the only one" if len(passing) == 1 else f"one of {len(passing)}"
        return f"{s.problem.text} Recommended: solution {s.recommended_solution.solution_id} of {len(s.solutions_tested)}, {which} that passed all {len(s.checks)} checks."
    return f"{s.problem.text} No fix has passed all {len(s.checks)} checks yet ({s.next_step.reason.lower()}), so an engineer takes it from here."


def report_evidence(run: dict, run_dir: Path | None = None) -> ReportEvidence:
    """What the report shows beside the summary: the run's screenshots, the code Agent B read, each patch and how it
    was tested. Screenshot names are looked up in the run folder (the adapter saves them as <name>.png)."""
    t, repro, diagnosis = run["ticket_id"], run.get("repro") or {}, run.get("diagnosis") or {}
    shots = []
    for name in repro.get("screenshots", []):
        for cand in (name, re.sub(r"[^\w-]", "_", Path(name).stem)[:50] + ".png"):
            p = (run_dir / cand) if run_dir else None
            if p and p.is_file():
                shots.append({"path": str(p), "caption": f"Screenshot: {name}", "by": "Recreator sandbox", "sourceId": f"run-{t}-repro"})
                break
    code = [{"file": s["file"], "fn": s["function"], "startLine": s["excerpt"]["start_line"], "lines": s["excerpt"]["lines"],
             "faulty": s["excerpt"]["faulty_lines"], "note": s["explanation"], "sourceId": f"run-{t}-diagnosis"}
            for s in diagnosis.get("suspects", []) if s.get("excerpt")]   # excerpt: proposed field, see docs/solution-summary.md

    def outcome(v, good, bad):
        return "no result" if v is None else good if v else bad

    how = {}
    for i, f in enumerate(_fixes(run)):
        if f.get("verified"):
            broken = ", ".join(f.get("broken_tests") or []) or "unnamed tests"
            how[i + 1] = [f"Applied patch {f['id']} in a fresh sandbox.",
                          f"Re-ran the repro script (exit 0 = bug gone): {outcome(f.get('repro_passed'), 'the bug is gone', 'the bug is still there')}.",
                          f"Ran the existing tests: {outcome(f.get('tests_passed'), 'all pass', f'broken: {broken}')}."]
        else:
            by = "the diagnosis agent" if f.get("by") == "diagnosis" else f.get("by", "")
            how[i + 1] = [f"Proposed by {by} from reading the code.", "Not run: no patch verifier is configured, so neither the repro nor the tests ran with it."]
    steps = list((run.get("triage") or {}).get("repro_plan", []))
    if repro.get("repro_script"):
        steps.append("Run the repro script: it exits 1 while the bug is present and 0 once it's fixed.")
    return ReportEvidence(screenshots=shots, faulty_code=code, patches={i + 1: f.get("patch", "") for i, f in enumerate(_fixes(run))},
                          how_tested=how, repro_steps=steps, call_path=diagnosis.get("call_path", []))
