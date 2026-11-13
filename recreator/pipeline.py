"""
pipeline.py - Runs one ticket (or every open ticket) end to end.

    python -m recreator.pipeline --ticket 2
    python -m recreator.pipeline --all-open

Steps:
  1. Load the ticket from Supabase (allowlisted columns only).
  2. Scrub personal details.
  3. Triage: claim, environment, plan, hypotheses, missing info.
  4. If we have a sandbox for this app: reproduce it (Agent A), then re-run
     the repro script ourselves on the current build and the last good build.
  5. If reproduced and source code is available: diagnose (Agent B).
  6. Privacy gate: block the results if any real personal value leaked.
  7. Save results to Supabase and to runs/<ticket>_<time>/ on disk.
"""
import argparse
import json
from datetime import datetime, timezone

import anthropic

from .adapters import adapter_for
from .agents import build_handoff, diagnose, recreate, triage
from .config import Settings, load_settings
from .schemas import Ticket
from .scrubber import Scrubber
from .source_tools import SourceTools
from .store import Store


def run_ticket(ticket_id: int, settings: Settings, store: Store, client: anthropic.Anthropic) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = settings.runs_dir / f"ticket{ticket_id}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"ticket_id": ticket_id, "status": "error", "triage": None,
                    "repro": None, "handoff": None, "diagnosis": None, "error": None}
    scrubber = Scrubber()
    adapter = None

    try:
        # 1-2. Load and scrub. Only the scrubbed version is ever written to disk.
        raw = store.get_ticket(ticket_id)
        ticket = Ticket.model_validate(scrubber.scrub(raw.model_dump()))
        (run_dir / "ticket_scrubbed.json").write_text(ticket.model_dump_json(indent=2))
        print(f"Ticket {ticket_id}: {ticket.title!r} ({ticket.app or 'unknown app'})")

        # 3. Triage
        print("Triage...")
        plan = triage(client, settings.model, ticket)
        result["triage"] = plan.model_dump()
        result["status"] = "triage_only"

        # 4. Reproduce, if an adapter exists for this app
        adapter_cls = adapter_for(ticket.app)
        if adapter_cls is None:
            print(f"No sandbox for '{ticket.app}' yet; stopping after triage.")
        else:
            adapter = adapter_cls(settings, run_dir)
            build, last_good = adapter.images_for(ticket)
            print(f"Starting sandbox on {build}...")
            adapter.reset(build)

            print("Reproducing (Agent A)...")
            report = recreate(client, settings.model, ticket, plan, adapter.tools(),
                              adapter.environment_notes, settings.max_recreator_turns)

            # Don't trust the agent's word: re-run its script ourselves on a FRESH sandbox.
            if report.repro_script:
                adapter.reset(build)
                rc, _ = adapter.run_repro_script(report.repro_script)
                report.verified_on_current = rc == 1
                if last_good:
                    print(f"Checking last good build {last_good}...")
                    adapter.reset(last_good)
                    rc, _ = adapter.run_repro_script(report.repro_script)
                    report.verified_clean_on_last_good = rc == 0

            result["repro"] = report.model_dump()
            result["status"] = report.status
            print(f"Reproduction: {report.status} (verified on current: {report.verified_on_current}, "
                  f"clean on last good: {report.verified_clean_on_last_good})")

            # 5. Diagnose, if reproduced and we have the source
            if report.status != "not_reproduced":
                handoff = build_handoff(ticket, plan, report)
                result["handoff"] = handoff.model_dump()
                if settings.mm_source_dir:
                    print("Diagnosing (Agent B)...")
                    diag = diagnose(client, settings.model, handoff,
                                    SourceTools(settings.mm_source_dir).tools(),
                                    settings.max_diagnosis_turns)
                    result["diagnosis"] = diag.model_dump()
                    result["status"] = "diagnosed"
                else:
                    print("MM_SOURCE_DIR not set; skipping code diagnosis.")

    except Exception as err:  # noqa: BLE001 - record any failure instead of crashing a batch
        result["error"] = f"{type(err).__name__}: {err}"
        print(f"Error: {result['error']}")
    finally:
        if adapter:
            adapter.close()

    # 6. Privacy gate: if a real value slipped through, keep nothing but the fact that it happened.
    leaks = scrubber.leaked(json.dumps(result, default=str))
    if leaks:
        print(f"BLOCKED: {len(leaks)} personal value(s) found in the output. Results not saved.")
        result = {"ticket_id": ticket_id, "status": "blocked_pii",
                  "error": f"{len(leaks)} personal value(s) detected in output; results discarded."}

    # 7. Save
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    store.save_run(result)
    print(f"Saved. Local copy: {run_dir}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproduce and diagnose tickets from Supabase.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticket", type=int, help="ID of one ticket to run")
    group.add_argument("--all-open", action="store_true", help="Run every open ticket")
    args = parser.parse_args()

    settings = load_settings()
    store = Store(settings)
    client = anthropic.Anthropic()
    ids = [args.ticket] if args.ticket else store.open_ticket_ids()
    for ticket_id in ids:
        run_ticket(ticket_id, settings, store, client)


if __name__ == "__main__":
    main()
