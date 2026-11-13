"""
checks.py - Plain rules every Solution Summary must pass (no AI). Each returns a list of problems; empty = passes.
Same rules as the portal's src/lib/solution-summary.ts (hnd.validate, hnd.tag).
"""
import re

from .models import SolutionSummary, SummaryTag

RECREATION_TEXT = "*Attached is the code recreation."

TAG_VOCABULARY: dict[str, tuple[str, ...]] = {
    "component": ("SizeAwareImage",),
    "area": ("web app front end", "server", "configuration", "mobile app", "desktop app", "integrations"),
    "cause": ("inline style overrides stylesheet", "missing null check", "race condition", "wrong default", "permission check"),
    "symptom": ("layout wraps early, content hidden", "crash", "wrong data shown", "slow", "action fails"),
    "surface": ("channel header", "system message", "Markdown", "message list", "sidebar", "settings"),
    "fix_size": ("small", "medium", "large"),
}
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def validate_summary(s: SolutionSummary, known_files: list[str]) -> list[str]:
    """Every claim cites evidence; the recommendation passed every check, or, when nothing passed, there is a next
    step instead; every check has a result; the root-cause file is known; the recreation line is exact."""
    problems: list[str] = []
    evidence = {e.id for e in s.evidence}
    claims = [("problem", s.problem.source_ids), ("environment", s.environment.source_ids), ("rootCause", s.root_cause.source_ids)]
    if s.recommended_solution:
        claims.append(("recommendedSolution", s.recommended_solution.source_ids))
    if s.next_step:
        claims.append(("nextStep", s.next_step.source_ids))
    claims += [(f"solution {x.id}", x.source_ids) for x in s.solutions_tested]
    claims += [(f"knownGaps[{i}]", g.source_ids) for i, g in enumerate(s.known_gaps)]
    for where, ids in claims:
        if not ids:
            problems.append(f"{where} has no source")
        problems += [f"{where} cites {i}, which is not in evidence" for i in ids if i not in evidence]

    check_ids = [c.id for c in s.checks]

    def passed_all(x) -> bool:
        return x.overall == "pass" and all(x.results.get(i) == "pass" for i in check_ids)

    if s.recommended_solution:
        rec = next((x for x in s.solutions_tested if x.id == s.recommended_solution.solution_id), None)
        if not rec:
            problems.append(f"recommendedSolution points to solution {s.recommended_solution.solution_id}, which was not tested")
        elif not passed_all(rec):
            problems.append(f"recommendedSolution points to solution {rec.id}, which failed or was not run")
        if s.outcome != "fix_found":
            problems.append(f'outcome is "{s.outcome}" but a solution is recommended')
    else:
        if any(passed_all(x) for x in s.solutions_tested):
            problems.append("a solution passed every check but none is recommended")
        if not s.next_step:
            problems.append("no solution is recommended and there is no next step")
        if s.outcome != "no_fix":
            problems.append(f'outcome is "{s.outcome}" but no solution is recommended')

    for x in s.solutions_tested:
        problems += [f"solution {x.id} has no result for check {i}" for i in check_ids if i not in x.results]
        problems += [f"solution {x.id} has a result for unknown check {i}" for i in x.results if i not in check_ids]

    # An empty file means the cause has no location yet; any file given must be one the run knows.
    if s.root_cause.file and s.root_cause.file not in known_files:
        problems.append(f"rootCause file {s.root_cause.file} is not a known file")
    if s.recreation.text != RECREATION_TEXT:
        problems.append(f'recreation text must be exactly "{RECREATION_TEXT}"')
    if s.recreation.attached and not (s.recreation.script or "").strip():
        problems.append("recreation is marked attached but has no script")
    return problems


def validate_tags(tags: list[SummaryTag]) -> list[str]:
    """Every tag is in the vocabulary (version by format); at most one per category except surfaces."""
    problems = []
    for t in tags:
        ok = bool(_VERSION.match(t.value)) if t.category == "version" else t.value in TAG_VOCABULARY.get(t.category, ())
        if not ok:
            problems.append(f'unknown {t.category} tag "{t.value}"')
    for c in [*TAG_VOCABULARY, "version"]:
        if c != "surface" and sum(t.category == c for t in tags) > 1:
            problems.append(f"more than one {c} tag")
    return problems
