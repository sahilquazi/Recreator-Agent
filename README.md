# Bug Recreator

Takes a ticket from Supabase, reproduces the bug in a throwaway sandbox, and
traces it to the code that causes it. Mattermost is the first supported app.
Other M365 apps are triaged now and can be reproduced once an adapter exists.

## How it works

```
ReportedIssues (Supabase)
   │  allowlisted columns only (no customer_id, no assigned_to)
   ▼
Scrubber ── names, emails, IDs, URLs, secrets → placeholders
   ▼
Triage ── claim, environment shape, repro plan, hypotheses, missing info   (every app)
   ▼
Agent A: Recreator ── drives a fresh Docker sandbox: API, browser, logs     (Mattermost)
   │      writes a repro script: exit 1 = bug present, 0 = absent
   ▼
Verification ── pipeline re-runs the script on a fresh sandbox,
   │             and on the last good build if known (should exit 0)
   ▼
Handoff ── technical findings only, no ticket text
   ▼
Agent B: Diagnosis ── reads the source tree (read-only), ranks suspects, proposes a patch
   ▼
Privacy gate ── any real value found in the output → results discarded
   ▼
recreator_runs (Supabase) + runs/<ticket>_<time>/ (screenshots, repro.py, result.json)
```

Agent A never sees source code and Agent B never sees the ticket, same as the pilot.

## Setup

You need Python 3.11+, Docker, and a Claude API key.

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env          # then fill it in
```

In the Supabase SQL Editor, run `sql/schema.sql` (results table + row-level security),
then `sql/mock_mattermost_ticket.sql` to add a Mattermost test ticket.

For code diagnosis, check out the Mattermost source at the ticket's version and
set `MM_SOURCE_DIR` to it:

```bash
git clone https://github.com/mattermost/mattermost
cd mattermost && git checkout v10.5.0
```

## Run

```bash
python -m recreator.pipeline --ticket 2      # one ticket
python -m recreator.pipeline --all-open      # every open ticket
```

Ticket 1 (Outlook) will stop after triage, because there's no Outlook sandbox yet.
That's expected.

## Files

| File | What it does |
|---|---|
| `recreator/pipeline.py` | Runs the steps above, saves results |
| `recreator/agents.py` | Instructions for triage, Agent A and Agent B |
| `recreator/llm.py` | The tool-use loop every agent runs on |
| `recreator/scrubber.py` | Placeholders and the final leak check |
| `recreator/store.py` | Supabase read (allowlist) and write |
| `recreator/schemas.py` | The shape of every input and output |
| `recreator/adapters/mattermost.py` | Docker sandbox + API, browser and log tools |
| `recreator/source_tools.py` | Read-only code search for Agent B |
| `recreator/troubleshooter/` | Takes over when no fix counts: classify, bounded retry, handoff case file |

## Adding another app

Write a class in `recreator/adapters/` with the same methods as `AppAdapter`
(`handles`, `images_for`, `reset`, `tools`, `run_repro_script`, `close`) and add it
to `ADAPTERS` in `adapters/__init__.py`. The rest of the pipeline doesn't change.

## Troubleshooter

If no fix counts (the repro must pass with it and the existing tests must still pass), the
troubleshooter takes over: it tags the failure with one of five reasons, tries once more within
strict limits (one self-recovery pass, one access request, one Reviewer retry), then hands the case
to an engineer with a case file (`runs/<ticket>_<time>/case_file.md`). It never loops.

```bash
python -m recreator.troubleshooter.demo     # every route, no API key or Docker needed
python -m pytest tests -q
```

Already created `recreator_runs`? Run `sql/troubleshooter_migration.sql` once.
How it works: [recreator/troubleshooter/README.md](recreator/troubleshooter/README.md) ·
Spec mapping and open decisions: [docs/troubleshooter.md](docs/troubleshooter.md).

## Before real customer tickets go in

- Install `presidio-analyzer`. Without it, plain first names ("Bob") in free text
  are not caught; emails, IDs, URLs and secrets are.
- Keep the service role key on a trusted machine only.
- Run repro scripts inside a container rather than on the host (see the note in
  `run_repro_script`).
- Measure it: replay tickets with known root causes and score whether Agent B's
  top suspect matches.
