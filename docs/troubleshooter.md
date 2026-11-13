# Troubleshooter (Diagnosis Agent: Troubleshooting System)

**Owner:** Tvisha Talwar · **Code:** `recreator/troubleshooter/` · **Spec:** Diagnosis Agent: Troubleshooting System Spec

## What it does

Takes over when the diagnosis plan ends without a fix that counts. It works out why the run failed, tries once more within strict limits, then hands the case to an engineer with a case file. It never loops.

A fix only **counts** if the repro passes with it applied in the sandbox **and** the existing tests still pass.

## Where it sits

```
Pattern agent ─► Recreator (Agent A) ─► Diagnosis/Reviewer (Agent B) ─► TROUBLESHOOTER ─► Kate's solution summary
                 reproduces the bug      reads code, proposes a fix      (this doc)          files to GitHub
```

In code it's step **5b** of `recreator/pipeline.py`. It runs when Agent B returns a diagnosis, or when the bug couldn't be reproduced. Its first job is to check Agent B's proposed fix. If that fix counts, the outcome is `fixed` and nothing else happens.

## Inputs and outputs

| | |
|---|---|
| **Reads** | Triage, the repro report, the Handoff (technical findings only) and Agent B's diagnosis. Also earlier runs of the same ticket, because limits are per case. |
| **Writes** | `result["troubleshooting"]` in Supabase (`recreator_runs.troubleshooting`), plus `case_file.md` and `case_file.json` in the run folder. The case file is updated after every step. |
| **Outcome** | `fixed`, `handed_off` or `waiting_on_admin` (also written to `recreator_runs.status`) |

Agent isolation stays as it was: the code-side steps never see the ticket text, and the admin questions never see source code.

## The five failure reasons

Each failed run gets exactly **one** reason (`classify.py`). The tagging is plain code, not AI, so it's predictable and testable. If a run fits more than one reason, the most cautious route wins: handoff beats asking the admin, which beats retrying.

| # | Reason | Detected when (in code) | Route |
|---|---|---|---|
| 5 | Out of time, or P0/P1 | Ticket severity maps to P0/P1, or the priority's time budget has run out | Hand off now. For P0/P1 an engineer leads from the start. |
| 3 | Fix passes repro, breaks tests | A verified fix has `repro_passed=true` and `tests_passed=false` | Hand off now. Which behavior to keep is a human call. |
| 4 | Can't reproduce | Repro status is `not_reproduced`, **or** the repro script didn't hold up when the pipeline reran it on a fresh sandbox | Ask the admin specific questions (one round per case), then hand off |
| 2 | Cause found, no fix passes | At least one cause is `confirmed` and no fix counts | One Reviewer retry with the failure details, then hand off |
| 1 | All likely causes ruled out | No cause is `confirmed` | One self-recovery pass, then one access request, then hand off |

The table is in caution order, which is the order ties are broken in.

## Limits and stop rules

Per **case** (carried across reruns of the same ticket via earlier `recreator_runs` rows):

- 1 self-recovery pass · 1 access request · 1 Reviewer retry · 1 round of admin questions
- At most 5 steps per run as a hard backstop

It stops and hands off as soon as any of these is true:

- It has used its self-recovery pass and its access request
- An access request was denied. A denial is final for that resource on that case.
- New access produced no new evidence (no cause confirmed or ruled out)
- The time budget for the case's priority ran out
- Once the access request is used, anything short of a fix is a handoff

**Evidence rule:** a cause only becomes `confirmed` or `ruled_out` when the step gives evidence. Status changes without evidence are ignored and logged.

## Self-recovery pass

This pass uses only the access the agent already has: the read-only source tree plus a new `recent_commits` tool.

1. Re-rank remaining guesses based on what was ruled out
2. Check recent commits and changes since the last good version on the affected code, since many bugs are regressions
3. Check the pattern agent's near-misses. This is a stub until the pattern agent exists (`near_misses` hook in `agent_steps.make_self_recovery`).

## Access request

A one-time ask in the spec's fixed format: **ruled out · points to · asking for · expects to find · how it will test it · limits**. If a field is missing, the request isn't sent.

- **What can be asked for** is limited to a **catalog** (`TROUBLESHOOT_ACCESS_CATALOG` JSON file). The agent can't name arbitrary paths. Every kind is read-only: `source_module`, `logs`, `tenant_config`, `deploy_history`. There is no write kind at all.
- **Built in:** `sandbox_server_logs` and `sandbox_server_config`. Agent B normally can't see these by design, so they're the natural first request.
- **Grants** are tied to one case, and every granted tool rechecks the grant before running. The grant is revoked when the investigation ends.
- **Who approves:** `TROUBLESHOOT_APPROVER=terminal` asks the person running the pipeline, who stands in for the case engineer (and the owning team, for cross-team resources). In batch runs with no terminal it denies. `deny` and `approve` exist for scripted demos.
- **Too slow:** if a resource's `approval_eta_hours` is longer than the time left in the budget, the request is skipped. The case file then lists the access needed under remaining guesses.

## Handoff case file

All eight sections are always present (an empty section says "none"):

1. Problem summary · 2. Repro · 3. Failure reason · 4. Causes tested (with evidence) · 5. Access used (including the access request and its decision) · 6. Fixes tried (and why each failed) · 7. Remaining guesses (ranked, with the access each needs) · 8. Recommended next step

It also lists the engineer's options (point the agent at a new area / work alongside / take over) and a timeline. Before the file is written, it goes through the same leak check as the pipeline's privacy gate.

## Admin-facing status

Plain language, never "the agent failed" (Bala's 10/23 feedback). See `status.py`, which is kept separate so it can move to Kate's tracker.

- Investigating: "We're still investigating. Here's what we've checked so far."
- Can't reproduce: "We need a few details to reproduce this:" followed by specific questions
- Handed off: "An engineer has your case. Here's what's already been checked, so you won't be asked again."
- Fixed: "We found the cause and a fix that passes our checks. An engineer will review it before it's released."

## Verifying fixes

Building Mattermost from source with a patch is slow, so the verifier is a slot (`verifier.py`):

- **Default (`NoVerifier`):** no fix ever counts. Cases with a confirmed cause are handed off with the patch marked "not verified", and the Reviewer retry is skipped because it couldn't prove anything.
- **`TROUBLESHOOT_VERIFY_CMD`:** a command that takes `<patch file> <repro script>` and prints `{"repro_passed": bool, "tests_passed": bool, "broken_tests": [...], "notes": "..."}`. The "apply patch → build image → rerun repro → run tests" work goes in that script.

## Running it

```bash
python -m recreator.troubleshooter.demo                  # every route, no API key or Docker needed
python -m recreator.troubleshooter.demo access_too_slow  # one scenario with its full case file
python -m pytest tests -q                                # the P0 checks
python -m recreator.pipeline --ticket 2                  # the real pipeline, troubleshooter included
```

Before the first real run, add the new column in Supabase: `sql/troubleshooter_migration.sql`.

## Spec checklist

| Priority | Requirement | Where |
|---|---|---|
| P0 | Every failed run gets one of five reasons and follows its route | `classify.py`, `engine.py`; tests `test_route`, `test_classify_*` |
| P0 | At most one self-recovery pass and one access request; stop rules enforced | `budget.py`, `engine.py`; tests `test_limits_are_never_exceeded`, `test_history_*` |
| P0 | Every handoff has all eight case file sections | `case_file.py`; test `test_every_handoff_has_all_eight_sections` |
| P1 | Access request has all six fields, expires, records the decision | `access.py`; tests `test_request_has_all_six_fields`, `test_grant_*` |
| P1 | Admin-facing status messages | `status.py`; test `test_admin_never_sees_failure_language` |

**Eval hooks:** each run stores `failure_reason`, `failure_reason_also_matched` (misrouting), `budget_used`, the access `decision` (approval rate), the fixes with verification flags (false "fix found" rate) and the remaining guesses (whether the real cause was in them at handoff). Replaying past cases is the next step once Bala shares them.

## Decisions made where the spec was open (team: please check)

1. **Untested causes count toward reason 1.** The spec says "every cause ruled out". In code, reason 1 means nothing is confirmed, since the route is the same either way.
2. **Can't reproduce gets one round of admin questions per case**, then a handoff, so reason 4 can't loop.
3. **After access is used, no Reviewer retry.** The spec says approved access "either produces a passing fix or hands off".
4. **The Reviewer retry is skipped when fixes can't be verified**, because a retry couldn't prove anything.
5. **Severity → priority:** Critical = P0, High = P1, Medium = P2, Low = P3, unknown = P2. Time budgets are placeholders (P2 60 min, P3 120 min) until Bala confirms.
6. **A "can't reproduce" case parks as `waiting_on_admin`.** Once the admin's answers are added to the ticket, rerunning the pipeline continues the same case with the same limits. How answers get from the admin into the ticket isn't built yet, and may belong in Kate's tracker.

## Open questions (from the spec, for Bala)

- How does the team handle cases today when they can't find the root cause, and when do engineers escalate?
- What permissions do the existing agents have on the codebase?
- Who grants access to another product team's code, and how long does it usually take? (This sets `approval_eta_hours` in the catalog.)
- What's a reasonable time budget per priority (P0 to P3)?
- Are there past cases, resolved and unresolved, we can replay for evals?
