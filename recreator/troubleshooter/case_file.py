"""
case_file.py - Renders and saves the handoff case file.

Saved after every step (case_file.md + case_file.json in the run folder), so a
crash halfway through still leaves the engineer everything learned so far.
Before anything is written it goes through the same leak check as the final
privacy gate; if a real personal value shows up, a placeholder is written instead.
"""
import json
from pathlib import Path
from typing import Callable

from .models import REASON_LABELS, CaseFile


def render_markdown(cf: CaseFile) -> str:
    out: list[str] = [f"# Case file: ticket {cf.ticket_id}", ""]
    if cf.priority:
        out += [f"**Priority:** {cf.priority}", ""]

    out += ["## 1. Problem summary", cf.problem_summary or "_(missing)_", ""]
    out += ["## 2. Repro", cf.repro or "_(missing)_", ""]

    out += ["## 3. Failure reason"]
    if cf.failure_reason:
        out.append(REASON_LABELS[cf.failure_reason])
        if cf.failure_reason_also_matched:
            also = "; ".join(REASON_LABELS[r] for r in cf.failure_reason_also_matched)
            out.append(f"\nAlso matched (took the more cautious route): {also}")
    else:
        out.append("None: a fix passed the repro and the existing tests.")
    out.append("")

    out += ["## 4. Causes tested"]
    if cf.causes_tested:
        for c in cf.causes_tested:
            out.append(f"- **{c.id} ({c.status.replace('_', ' ')})**: {c.description}"
                       + (f"  \n  _Where:_ `{c.location}`" if c.location else ""))
            for e in c.evidence:
                out.append(f"  - Evidence: {e}")
    else:
        out.append("- None confirmed or ruled out yet.")
    out.append("")

    out += ["## 5. Access used"] + [f"- {line}" for line in cf.access_used]
    if cf.access_request:
        r = cf.access_request
        out += ["", "**Access request**", "",
                f"- Ruled out: {r.ruled_out}", f"- Points to: {r.points_to}",
                f"- Asking for: {r.asking_for}", f"- Expects to find: {r.expects_to_find}",
                f"- How it will test it: {r.how_to_test}", f"- Limits: {r.limits}",
                f"- Decision: {r.decision or 'pending'}" + (f" by {r.decided_by}" if r.decided_by else "")
                + (f", expired {r.expires_at}" if r.expires_at else "")]
    out.append("")

    out += ["## 6. Fixes tried"]
    if cf.fixes_tried:
        for f in cf.fixes_tried:
            target = f" for {f.target_cause}" if f.target_cause else ""
            out.append(f"- **{f.id}** ({f.by}{target}): {f.why_failed()}" + (f". {f.notes}" if f.notes else ""))
            out += ["", "  ```diff", *[f"  {line}" for line in f.patch.strip().splitlines()[:60]], "  ```", ""]
    else:
        out.append("- No fix was proposed.")
    out.append("")

    out += ["## 7. Remaining guesses"]
    if cf.remaining_guesses:
        for c in cf.remaining_guesses:
            need = c.access_needed or "current access"
            out.append(f"- **{c.id}** ({c.confidence:.0%}): {c.description}"
                       + (f" `{c.location}`" if c.location else "") + f"  \n  _Needs:_ {need}")
    else:
        out.append("- None left: every cause on the list was tested.")
    out.append("")

    out += ["## 8. Recommended next step", cf.recommended_next_step or "_(missing)_", ""]
    out += ["### Engineer options"] + [f"- {o}" for o in cf.engineer_options] + [""]
    out += ["### Timeline"] + [f"- {t}" for t in cf.timeline] + [""]
    return "\n".join(out)


def save(cf: CaseFile, run_dir: Path | None, leak_check: Callable[[str], list] | None = None) -> None:
    if run_dir is None:
        return
    md = render_markdown(cf)
    js = cf.model_dump_json(indent=2)
    if leak_check and leak_check(md + js):
        md = js = json.dumps({"ticket_id": cf.ticket_id,
                              "error": "Case file withheld: a personal value was detected."})
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "case_file.md").write_text(md)
    (run_dir / "case_file.json").write_text(js)
