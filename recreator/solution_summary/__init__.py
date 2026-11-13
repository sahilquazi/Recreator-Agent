"""
Solution Summary: the engineer write-up of a run. What's broken, the root cause (never one that was ruled out), every
fix tried and how it was tested, the fix to use or why there is none yet, and what is still unknown.

Built after the troubleshooter (pipeline.py step 5c) with no model call, saved as result["solution_summary"] and as
solution_summary.json / .html / .pdf in the run folder. Same rules and JSON shape as the Engineering Direct portal's
Solution Summary (hnd.summarize). See docs/solution-summary.md.
"""
from .build import build_summary, headline, known_files, report_evidence, root_cause_of
from .checks import RECREATION_TEXT, TAG_VOCABULARY, validate_summary, validate_tags
from .models import ReportEvidence, SolutionSummary
from .report import render_html, save_report

__all__ = [
    "build_summary", "headline", "known_files", "report_evidence", "root_cause_of",
    "RECREATION_TEXT", "TAG_VOCABULARY", "validate_summary", "validate_tags",
    "ReportEvidence", "SolutionSummary", "render_html", "save_report",
]
