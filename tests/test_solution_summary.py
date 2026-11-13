"""
Solution Summary (recreator/solution_summary): every troubleshooter route, the checker, the report files, saving
without the new column, and the same output as the Engineering Direct portal's agent (hnd.summarize).
"""
import base64
import json
from pathlib import Path

import pytest

from recreator.solution_summary import (
    RECREATION_TEXT, build_summary, known_files, render_html, report_evidence, save_report, validate_summary, validate_tags,
)
from recreator.solution_summary.build import fix_counts
from recreator.solution_summary.demo import demo_result
from recreator.solution_summary.models import SummaryTag
from recreator.troubleshooter.demo import scenarios

FIXTURES = Path(__file__).parent / "fixtures" / "solution_summary"


# ---------------------------------------------------------------- every route
@pytest.mark.parametrize("name", list(scenarios()))
def test_every_route_gets_a_sound_summary(name):
    result = demo_result(name)
    s = build_summary(result)
    if result["diagnosis"] is None:
        assert s is None, "not reproduced: nothing to summarize"
        return
    assert validate_summary(s, known_files(result)) + validate_tags(s.tags) == []
    ts = result["troubleshooting"]
    if ts["outcome"] == "fixed":
        winner = next(i for i, f in enumerate(ts["case_file"]["fixes_tried"]) if fix_counts(f))
        assert s.outcome == "fix_found" and s.recommended_solution.solution_id == winner + 1
    else:
        assert s.outcome == "no_fix" and s.recommended_solution is None and s.next_step.text
    ruled_out = {c["description"] for c in ts["case_file"]["causes"] if c["status"] == "ruled_out"}
    assert s.root_cause.text not in ruled_out, "never names a cause the troubleshooter ruled out"
    assert s.recreation.text == RECREATION_TEXT


# ---------------------------------------------------------------- same as the portal
@pytest.mark.parametrize("path", sorted((FIXTURES / "runs").glob("*.json")), ids=lambda p: p.stem)
def test_same_summary_as_the_portal(path):
    """expected/ holds what the portal's TypeScript agent made from the same runs (see fixtures/solution_summary/README.md)."""
    run = json.loads(path.read_text(encoding="utf-8"))
    expected = json.loads((FIXTURES / "expected" / path.name).read_text(encoding="utf-8"))
    s = build_summary(run)
    assert (s.to_json() if s else None) == expected


# ---------------------------------------------------------------- the checker
def _fixed():
    result = demo_result("access_finds_fix")
    return result, build_summary(result)


def _has(problems, bit):
    assert any(bit in p for p in problems), problems


def test_checker_catches_broken_summaries():
    result, good = _fixed()
    known = known_files(result)
    assert validate_summary(good, known) == []

    s = good.model_copy(deep=True)
    s.recommended_solution.solution_id = 1                     # fix 1 failed the repro
    _has(validate_summary(s, known), "which failed or was not run")

    s = good.model_copy(deep=True)
    del s.solutions_tested[0].results["tests-pass"]
    _has(validate_summary(s, known), "no result for check tests-pass")

    s = good.model_copy(deep=True)
    s.recreation.text = "Attached is the code recreation."
    _has(validate_summary(s, known), "recreation text")

    s = good.model_copy(deep=True)
    s.root_cause.source_ids = []
    _has(validate_summary(s, known), "rootCause has no source")

    s = good.model_copy(deep=True)
    s.root_cause.file = "made/up.go"
    _has(validate_summary(s, known), "not a known file")

    s = good.model_copy(deep=True)
    s.recommended_solution, s.outcome = None, "no_fix"           # a fix passed but none is recommended
    _has(validate_summary(s, known), "none is recommended")

    _has(validate_tags([SummaryTag(category="cause", value="cosmic rays")]), "unknown cause")
    _has(validate_tags([SummaryTag(category="fix_size", value="small"), SummaryTag(category="fix_size", value="large")]), "more than one")


# ---------------------------------------------------------------- the report
def test_report_files(tmp_path):
    result, s = _fixed()
    written = save_report(s, report_evidence(result, tmp_path), tmp_path, result["triage"]["claim"], known_files(result), pdf=False)
    assert [p.name for p in written] == ["solution_summary.json", "solution_summary.html"]
    saved = json.loads((tmp_path / "solution_summary.json").read_text(encoding="utf-8"))
    assert saved["summary"]["recommendedSolution"]["solutionId"] == 2 and saved["headline"]
    page = (tmp_path / "solution_summary.html").read_text(encoding="utf-8")
    for part in ("The bug, recreated", "Diagnosis", "How it was tested", "The solution", "Known gaps", RECREATION_TEXT):
        assert part in page


def test_report_embeds_screenshots_and_marks_faulty_lines(tmp_path):
    result, s = _fixed()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    (tmp_path / "header_wrapped.png").write_bytes(png)            # the adapter saves screenshots as <name>.png
    result["repro"]["screenshots"] = ["header wrapped"]
    result["diagnosis"]["suspects"][0]["excerpt"] = {"start_line": 10, "lines": ["a", "b", "c"], "faulty_lines": [11]}
    ev = report_evidence(result, tmp_path)
    assert len(ev.screenshots) == 1 and len(ev.faulty_code) == 1
    page = render_html(s, ev, "t", known_files(result))
    assert "data:image/png;base64," in page and 'class="bad"' in page


def test_a_failure_in_the_summary_never_stops_the_run():
    """pipeline.py wraps step 5c; a run with no triage just gives no summary."""
    assert build_summary({"ticket_id": 1, "triage": None, "repro": None, "handoff": None, "diagnosis": None}) is None


# ---------------------------------------------------------------- saving
class _FakeTable:
    def __init__(self, has_column):
        self.has_column, self.rows, self._row = has_column, [], None

    def insert(self, row):
        self._row = row
        return self

    def execute(self):
        if "solution_summary" in self._row and not self.has_column:
            raise RuntimeError("Could not find the 'solution_summary' column of 'recreator_runs' in the schema cache")
        self.rows.append(self._row)


class _FakeDb:
    def __init__(self, table):
        self.t = table

    def table(self, name):
        return self.t


@pytest.mark.parametrize("has_column", [True, False])
def test_save_run_with_or_without_the_new_column(has_column):
    from recreator.store import Store
    table = _FakeTable(has_column)
    store = Store.__new__(Store)
    store.db, store.runs = _FakeDb(table), "recreator_runs"
    store.save_run({"ticket_id": 2, "status": "fixed", "solution_summary": {"outcome": "fix_found"}})
    assert len(table.rows) == 1
    assert ("solution_summary" in table.rows[0]) == has_column
