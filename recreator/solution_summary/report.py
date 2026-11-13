"""
report.py - The engineer report: the Solution Summary as one HTML page and a PDF, saved in the run folder.

Reading order an engineer needs after the fact: verdict, the bug recreated (steps, screenshots), the diagnosis with the
faulty code, every fix tried and how it was tested, the fix, what's still unknown, the repro script and sources.
Screenshots are embedded, so the HTML and PDF stand alone. The PDF uses Playwright (already a dependency); if it can't
run, the JSON and HTML are still saved. Same layout as the portal's report.
"""
import base64
import html
import json
from pathlib import Path

from .build import headline
from .checks import validate_summary, validate_tags
from .models import ReportEvidence, SolutionSummary

_CSS = """
@page { size: Letter; margin: 14mm 13mm; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font: 14px/20px system-ui, -apple-system, "Segoe UI", sans-serif; color: #1c1b19; margin: 0; padding: 32px; max-width: 900px; }
h1 { font-size: 24px; line-height: 30px; margin: 4px 0 8px; } h2 { font-size: 17px; margin: 28px 0 10px; break-after: avoid; }
.caps { font-size: 11px; letter-spacing: .06em; font-weight: 600; text-transform: uppercase; color: #6b6a64; }
.muted { color: #6b6a64; } .small { font-size: 12px; line-height: 16px; } code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; }
code { font-size: 12.5px; background: #efebe0; border-radius: 4px; padding: 0 3px; }
.verdict { display: grid; grid-template-columns: 130px 1fr; gap: 6px 16px; background: #fbf7ee; border: 1px solid #e8dcc2; border-radius: 8px; padding: 14px 18px; margin: 12px 0; }
.verdict dt { color: #8a5a12; font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
.verdict dd { margin: 0; }
.keep { break-inside: avoid; } figure { margin: 12px 0; } figure img { max-width: 100%; border: 1px solid #e3e0d7; border-radius: 6px; }
pre { font-size: 11px; line-height: 15px; background: #f4f2ec; border: 1px solid #e3e0d7; border-radius: 6px; padding: 8px 0; overflow-x: auto; white-space: pre; margin: 6px 0; }
pre div { padding: 0 12px; } .ln { display: inline-block; width: 40px; color: #6b6a64; } .bad { background: #fbf7ee; border-left: 3px solid #c2410c; }
.add { color: #4f6b45; } .del { color: #c2410c; } .hunk { color: #6b6a64; } .meta { font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; } th { text-align: left; font-size: 11px; color: #6b6a64; font-weight: 600; padding: 4px 8px 4px 0; }
td { border-top: 1px solid #eeece5; padding: 6px 8px 6px 0; vertical-align: top; } .rec td { background: #fbf7ee; }
.fail { color: #b8781f; font-weight: 600; } .notrun { color: #6b6a64; }
.fix { border: 1px solid #e3e0d7; border-radius: 8px; padding: 10px 14px; margin: 10px 0; }
.tag { display: inline-block; background: #f4f2ec; border-radius: 4px; padding: 1px 7px; font-size: 11px; font-weight: 600; text-transform: uppercase; margin: 2px 4px 2px 0; }
.problems { color: #c2410c; }
"""


def _e(s) -> str:
    return html.escape(str(s or ""))


def _code(s: str) -> str:
    """Escape, then show `backtick` spans as code."""
    parts = _e(s).split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


def _result(r: str | None) -> str:
    return {"pass": "Pass", "fail": '<span class="fail">Fail</span>', "not_run": '<span class="notrun">Not run</span>'}.get(r or "", "—")


_BASIS = {"reasoned": "Reasoned · not run", "pr_tests": "PR tests", "sandbox": "Sandbox tests"}
_GAP = {"inferred": "Inferred", "not_validated": "Not validated", "unseen": "Not seen", "unchecked": "Unchecked"}


def _diff(d: str) -> str:
    rows = []
    for line in (d or "").split("\n"):
        cls = "meta" if line.startswith(("+++", "---")) else "hunk" if line.startswith("@@") else "add" if line.startswith("+") else "del" if line.startswith("-") else ""
        rows.append(f'<div class="{cls}">{_e(line) or "&nbsp;"}</div>')
    return f'<pre class="keep">{"".join(rows)}</pre>'


def _img(path: str) -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return f"data:image/png;base64,{data}"


def render_html(s: SolutionSummary, ev: ReportEvidence, title: str, known: list[str], note: str = "") -> str:
    problems = validate_summary(s, known) + validate_tags(s.tags)
    rec = s.recommended_solution
    rec_row = next((x for x in s.solutions_tested if rec and x.id == rec.solution_id), None)
    label = {e.id: e.label for e in s.evidence}
    fixed = s.outcome == "fix_found"
    where = (f"<code>{_e(s.root_cause.file)}</code>" + (f" · <code>{_e(s.root_cause.fn)}</code>" if s.root_cause.fn else "")) if s.root_cause.file else '<span class="muted">Location not found yet</span>'
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title><style>{_CSS}</style></head><body>",
           '<p class="caps">Solution Summary · engineer side</p>', f"<h1>{_code(title)}</h1>",
           f'<p class="muted small">{_e(s.environment.product)} {_e(s.environment.version)} · {"Fix found" if fixed else "No fix yet"} · checker: {f"{len(problems)} problems" if problems else "all rules pass"}</p>',
           '<dl class="verdict">',
           f"<dt>Verdict</dt><dd><b>{f'Fix {rec.solution_id} passed all {len(s.checks)} checks. Confidence {rec.confidence}.' if fixed and rec else _e(s.next_step.reason) + '. An engineer takes it from here.'}</b></dd>",
           f"<dt>Problem</dt><dd>{_code(s.problem.text)}</dd>", f"<dt>Where</dt><dd>{where}</dd>",
           f"<dt>{'Fix' if fixed else 'Next step'}</dt><dd>{_code(rec.change if fixed and rec else s.next_step.text)}</dd>",
           f"<dt>Fixes tried</dt><dd>{len(s.solutions_tested)}, judged on {len(s.checks)} checks · {sum(x.overall == 'pass' for x in s.solutions_tested)} passed</dd></dl>",
           f'<p class="muted small">Built from the run with no model call, then checked by plain-code rules. {_e(note)}</p>']

    out.append("<h2>1 · The bug, recreated</h2><ol>" + "".join(f"<li>{_code(x)}</li>" for x in ev.repro_steps) + "</ol>")
    out.append(f'<p class="muted small">Affected: {_e(", ".join(s.environment.surfaces))}</p>')
    for shot in ev.screenshots:
        out.append(f'<figure class="keep"><img src="{_img(shot["path"])}" alt="{_e(shot["caption"])}"><figcaption class="small muted">{_e(shot["caption"])} · {_e(shot["by"])}</figcaption></figure>')
    if not ev.screenshots:
        out.append('<p class="muted small">No screenshots in this run.</p>')

    out.append(f"<h2>2 · Diagnosis</h2><p>{_code(s.root_cause.text)}</p><p class='muted small'>Confidence {s.root_cause.confidence} · {_e(' · '.join(label.get(i, i) for i in s.root_cause.source_ids))}</p>")
    if ev.call_path:
        out.append('<p class="small"><span class="caps">Call path</span> ' + " → ".join(f"<code>{_e(c)}</code>" for c in ev.call_path) + "</p>")
    for c in ev.faulty_code:
        rows = "".join(f'<div class="{"bad" if c["startLine"] + i in c["faulty"] else ""}"><span class="ln">{c["startLine"] + i}</span>{_e(line) or "&nbsp;"}</div>' for i, line in enumerate(c["lines"]))
        out.append(f'<figure class="keep"><figcaption class="small"><code>{_e(c["file"])}</code> · <code>{_e(c["fn"])}</code> · lines {c["startLine"]}–{c["startLine"] + len(c["lines"]) - 1}</figcaption>'
                   f'<pre>{rows}</pre><p class="small"><b class="problems">Faulty: line{"s" if len(c["faulty"]) > 1 else ""} {", ".join(map(str, c["faulty"]))}.</b> {_code(c["note"])}</p></figure>')
    if not ev.faulty_code:
        out.append('<p class="muted small">' + (f"The code excerpt isn't in this run. Open {where}." if s.root_cause.file else "No code location has been found yet.") + "</p>")

    out.append("<h2>3 · Fixes tried, and how each was tested</h2><ol>" + "".join(f"<li><b>{_e(c.name)}.</b> {_e(c.description)}</li>" for c in s.checks) + "</ol>")
    out.append("<table class='keep'><tr><th>Fix</th>" + "".join(f"<th>Check {i + 1}</th>" for i in range(len(s.checks))) + "<th>Overall</th><th>Basis</th></tr>")
    for x in s.solutions_tested:
        is_rec = rec is not None and x.id == rec.solution_id
        out.append(f'<tr class="{"rec" if is_rec else ""}"><td>{x.id}{" · recommended" if is_rec else ""}</td>' + "".join(f"<td>{_result(x.results.get(c.id))}</td>" for c in s.checks)
                   + f"<td>{_result(x.overall)}</td><td>{_BASIS[x.basis]}</td></tr>")
    out.append("</table>")
    for x in s.solutions_tested:
        steps = "".join(f"<li>{_code(t)}</li>" for t in ev.how_tested.get(x.id, []))
        result = "passed every check" if x.overall == "pass" else "failed" if x.overall == "fail" else "not run"
        patch = _diff(ev.patches[x.id]) if ev.patches.get(x.id) and not (rec and x.id == rec.solution_id) else ""
        out.append(f'<div class="fix keep"><b>Fix {x.id}: {_code(x.approach)}</b> <span class="tag">{_BASIS[x.basis]}</span>'
                   f'<p class="caps">How it was tested</p><ol class="small">{steps}</ol><p class="small"><b>Result:</b> {result}. {_code(x.why)}</p>{patch}</div>')

    if fixed and rec:
        basis = {"reasoned": "reasoned, not run", "pr_tests": "the fixing PR's tests", "sandbox": "the repro and tests, run in the sandbox"}[rec_row.basis] if rec_row else ""
        out.append(f"<h2>4 · The solution</h2><p><b>Fix {rec.solution_id}.</b> {_code(rec.change)}</p><p class='muted small'>Confidence {rec.confidence} · basis: {basis}</p>")
        out.append(_diff(ev.patches[rec.solution_id]) if ev.patches.get(rec.solution_id) else '<p class="muted small">No patch is attached for this fix.</p>')
    else:
        out.append(f"<h2>4 · No fix yet</h2><p><b>{_e(s.next_step.reason)}.</b> No fix passed every check, so nothing is recommended.</p><p><span class='caps'>Next step</span> {_code(s.next_step.text)}</p>")

    out.append("<h2>5 · Known gaps and risks</h2><ul class='small'>" + "".join(f"<li><b>{_GAP[g.kind]}.</b> {_code(g.text)}</li>" for g in s.known_gaps) + "</ul>")
    out.append("<p>" + "".join(f'<span class="tag">{_e(t.category.replace("_", " "))}: {_e(t.value)}</span>' for t in s.tags) + "</p>")
    out.append(f"<hr><p>{_e(s.recreation.text)}</p>")
    if s.recreation.script:
        out.append(f'<p class="muted small">Repro script · {"confirmed on a fresh sandbox" if s.recreation.verified else "not confirmed on a fresh sandbox"} · exits 1 while the bug is present</p><pre class="keep">{_e(s.recreation.script)}</pre>')
    out.append('<p class="caps">Sources</p><ul class="small">' + "".join(f"<li>{_e(e.label)}</li>" for e in s.evidence) + "</ul>")
    if problems:
        out.append('<p class="caps problems">Checker problems</p><ul class="small problems">' + "".join(f"<li>{_e(p)}</li>" for p in problems) + "</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def save_report(s: SolutionSummary, ev: ReportEvidence, run_dir: Path, title: str, known: list[str], pdf: bool = True) -> list[Path]:
    """Writes solution_summary.json, .html and (if Playwright can run) .pdf into the run folder. Returns what it wrote."""
    run_dir.mkdir(parents=True, exist_ok=True)
    written = []
    js = run_dir / "solution_summary.json"
    js.write_text(json.dumps({"headline": headline(s), "summary": s.to_json()}, indent=2), encoding="utf-8")
    written.append(js)
    page = render_html(s, ev, title, known)
    htm = run_dir / "solution_summary.html"
    htm.write_text(page, encoding="utf-8")
    written.append(htm)
    if pdf:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch()
                tab = browser.new_page()
                tab.set_content(page, wait_until="load")
                out = run_dir / "solution_summary.pdf"
                tab.pdf(path=str(out), format="Letter", print_background=True, margin={"top": "14mm", "bottom": "14mm", "left": "13mm", "right": "13mm"})
                browser.close()
            written.append(out)
        except Exception as err:  # noqa: BLE001 - the PDF is a nice-to-have; the JSON and HTML are already saved
            # First line only, ASCII-safe: Playwright's messages draw boxes that a Windows console can't print.
            reason = (str(err).strip().splitlines() or [""])[0][:200].encode("ascii", "replace").decode()
            print(f"Solution summary PDF skipped ({type(err).__name__}: {reason}). The HTML version is in {htm.name}."
                  " For PDFs, run `playwright install chromium` once.")
    return written
