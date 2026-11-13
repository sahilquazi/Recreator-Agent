# Solution Summary

**Owner:** Kate Kwon (Engineering Direct portal) · **Code:** `recreator/solution_summary/` · **Pipeline step:** 5c

The engineer write-up of a run: what's broken, the root cause, every fix tried and how it was tested, the fix to use
or why there is none yet, and what is still unknown. It is the last step after the troubleshooter:

```
Recreator (Agent A) ─► Diagnosis (Agent B) ─► Troubleshooter ─► SOLUTION SUMMARY ─► privacy gate ─► save
```

No model call: it moves and labels what the run already says. The Engineering Direct portal builds the same summary
with the same rules (`hnd.summarize`), and the tests check both give identical output for the same runs.

## What it writes

| Where | What |
|---|---|
| `result["solution_summary"]` | The summary as JSON (camelCase, the portal's `SolutionSummary` shape). Also in `recreator_runs.solution_summary`. |
| `runs/<ticket>_<time>/solution_summary.json` | The summary plus a one-line headline |
| `runs/<ticket>_<time>/solution_summary.html` | The engineer report, screenshots embedded, one file |
| `runs/<ticket>_<time>/solution_summary.pdf` | The same report as PDF (needs `playwright install chromium`; skipped with a note otherwise) |

It is added only when there is a diagnosis to summarize. A run that wasn't reproduced has none, and the case file
covers it. If building the summary fails, the run is still saved without it.

## How it decides

- **Checks:** every fix is judged on the troubleshooter's bar: the repro passes with the fix, and the existing tests
  still pass. A fix that wasn't verified shows "Not run" and never counts.
- **Recommendation:** a fix that passed both checks, preferring one that was actually run. If none passed, there is
  no recommendation; the summary gives the failure reason and the case file's recommended next step instead.
- **Root cause:** the cause the winning fix targets, else the first confirmed cause, else the strongest untested guess
  (marked "Not confirmed yet"), else "No cause confirmed yet". Never a cause the troubleshooter ruled out.
- **Known gaps:** caveats, untested causes and the access they need, unverified patches, an unconfirmed repro, no
  last good version.
- **Checker** (`checks.py`): every claim cites a run step, the recommendation passed every check (or there's a next
  step), every fix has a result for every check, the root-cause file is one the run knows, tags come from a fixed
  list, and the summary ends with the exact line `*Attached is the code recreation.`

## Before the first real run

1. In the Supabase SQL Editor, run `sql/solution_summary_migration.sql` once. Until then `store.save_run` saves runs
   without the summary and prints a reminder.
2. For PDFs: `playwright install chromium` (the Recreator already uses Playwright).

## Try it

```bash
python -m recreator.solution_summary.demo                                   # every troubleshooter scenario
python -m recreator.solution_summary.demo access_finds_fix runs/summary-demo   # one scenario's json, html, pdf
python -m pytest tests/test_solution_summary.py -q
```

## Proposed: code excerpts

The report shows the faulty code when a diagnosis suspect carries an excerpt. `schemas.py` doesn't have that yet;
the proposal is one optional field on `Suspect`, filled by Agent B from the lines it read:

```python
excerpt: dict | None = None   # {"start_line": 451, "lines": [...], "faulty_lines": [451, 470]}
```

Without it the report names the file and function only.

## Keep in step with the portal

The JSON shape is the portal's `SolutionSummary` (`src/data/types.ts` in mchung042/product_gt_microsoft_fall_2026).
`tests/fixtures/solution_summary/expected/` holds the portal agent's output for 17 runs; if either side changes how a
summary is built, regenerate those and update both.
