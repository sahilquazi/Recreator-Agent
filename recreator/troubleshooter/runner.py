"""
runner.py - Connects the troubleshooter to the real pipeline.

pipeline.py calls troubleshoot() after Agent B (or after a failed reproduction).
This file turns the pipeline's objects (ticket, triage, repro report, handoff,
diagnosis, sandbox adapter, Supabase store) into the engine's inputs and ports.
"""
import json
import time
from pathlib import Path

import anthropic

from ..schemas import Diagnosis, Handoff, ReproReport, Ticket, TriageResult
from . import agent_steps
from .access import AccessCatalog, StaticApprover, TerminalApprover
from .budget import CaseHistory, priority_for
from .config import TroubleshootSettings
from .engine import CaseInput, Engine, Ports
from .history_tools import HistoryTools
from .models import AccessKind, Cause, TroubleshootResult
from .verifier import CommandVerifier, NoVerifier


def troubleshoot(*, client: anthropic.Anthropic, model: str, ticket: Ticket, plan: TriageResult,
                 report: ReproReport | None, handoff: Handoff | None, diagnosis: Diagnosis | None,
                 adapter=None, build: str | None = None, source_dir: Path | None = None,
                 run_dir: Path | None = None, scrubber=None, store=None, started_at: float | None = None,
                 cfg: TroubleshootSettings | None = None, ports: Ports | None = None) -> TroubleshootResult:
    cfg = cfg or TroubleshootSettings.from_env()
    case = build_case_input(ticket, plan, report, diagnosis, source_dir, cfg, started_at,
                            load_history(store, ticket.id))
    if ports is None:
        ports = build_ports(client, model, ticket, plan, report, handoff, adapter, build, source_dir, cfg)
    engine = Engine(case, ports, run_dir=run_dir, leak_check=scrubber.leaked if scrubber else None)
    return engine.run()


# ---------------------------------------------------------------- inputs
def is_reproduced(report: ReproReport | None) -> bool:
    """Reproduced, and the script didn't fall apart when the pipeline re-ran it on a fresh sandbox."""
    return bool(report and report.status != "not_reproduced" and report.verified_on_current is not False)


def causes_from(diagnosis: Diagnosis | None) -> list[Cause]:
    if not diagnosis:
        return []
    return [Cause(id=f"C{i}", description=s.explanation, location=f"{s.file}:{s.function}:{s.lines}",
                  confidence=s.confidence, status=s.status,
                  evidence=[f"[diagnosis] {s.evidence}"] if s.evidence else [])
            for i, s in enumerate(diagnosis.suspects, 1)]


def build_case_input(ticket: Ticket, plan: TriageResult, report: ReproReport | None,
                     diagnosis: Diagnosis | None, source_dir: Path | None, cfg: TroubleshootSettings,
                     started_at: float | None, history: CaseHistory) -> CaseInput:
    priority = priority_for(ticket.severity)
    summary = f"{plan.claim}\n\nExpected: {plan.expected}\n\nActual: {plan.actual}"
    if report and report.trigger:
        summary += f"\n\nWhat triggers it: {report.trigger} ({report.location})"

    if report and is_reproduced(report):
        repro = (f"Reproduced ({report.status}) on {ticket.app} {ticket.version or ''}.\n\n"
                 f"- Trigger: {report.trigger}\n- Where: {report.location}\n- Pattern: {report.pattern}\n"
                 f"- Repro script re-run on a fresh sandbox: {_yes_no(report.verified_on_current)}\n"
                 f"- Clean on last good build: {_yes_no(report.verified_clean_on_last_good)}\n"
                 f"- Script: repro.py in this run folder (exit 1 = bug present, 0 = absent)")
    elif report:
        tried = "\n".join(f"- {a}" for a in report.attempts) or "- (none recorded)"
        why = ("the repro script didn't reproduce it on a fresh sandbox"
               if report.status != "not_reproduced" else "no attempt triggered the bug")
        repro = f"Could not be reproduced: {why}.\n\nWhat was tried:\n{tried}"
    else:
        repro = "No reproduction was attempted (no sandbox for this app)."

    baseline = ["Scrubbed ticket, triage and a throwaway sandbox (Recreator, Agent A)"]
    if source_dir:
        baseline.append(f"Source tree for {ticket.app} {ticket.version or ''}, read-only (Diagnosis, Agent B)")

    return CaseInput(
        ticket_id=ticket.id, priority=priority, reproduced=is_reproduced(report),
        problem_summary=summary, repro=repro, causes=causes_from(diagnosis),
        initial_patch=diagnosis.proposed_patch if diagnosis else None,
        initial_target="C1" if diagnosis and diagnosis.suspects else None,
        baseline_access=baseline, budget_minutes=cfg.budget_minutes.get(priority),
        started_at=started_at if started_at is not None else time.monotonic(),
        history=history, grant_ttl_minutes=cfg.grant_ttl_minutes,
    )


def _yes_no(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "not checked"}[value]


def load_history(store, ticket_id: int) -> CaseHistory:
    """What earlier runs of this ticket already used (allowances are per case, not per run)."""
    if store is None:
        return CaseHistory()
    try:
        rows = (store.db.table(store.runs).select("troubleshooting").eq("ticket_id", ticket_id)
                .order("id").execute().data)
        return CaseHistory.from_prior_results([r.get("troubleshooting") for r in rows if r.get("troubleshooting")])
    except Exception as err:  # noqa: BLE001 - e.g. the troubleshooting column isn't migrated yet
        print(f"  [troubleshoot] Couldn't read case history ({type(err).__name__}); treating this as a new case.")
        return CaseHistory()


# ---------------------------------------------------------------- ports
def build_ports(client, model, ticket, plan, report, handoff, adapter, build, source_dir,
                cfg: TroubleshootSettings) -> Ports:
    script = report.repro_script if report else None
    verifier = CommandVerifier(cfg.verify_cmd, script, cfg.verify_timeout_s) if cfg.verify_cmd else NoVerifier()
    approver = {"deny": StaticApprover("denied", "policy: TROUBLESHOOT_APPROVER=deny"),
                "approve": StaticApprover("approved", "demo: TROUBLESHOOT_APPROVER=approve")
                }.get(cfg.approver, TerminalApprover())

    catalog = AccessCatalog.load(cfg.catalog_path)
    if adapter is not None and build and script:
        _add_sandbox_resources(catalog, adapter, build, script)

    ports = Ports(verifier=verifier, approver=approver, catalog=catalog,
                  admin_questions=agent_steps.make_admin_questions(client, model, ticket, plan, report))
    if handoff is not None and source_dir is not None:
        tools = lambda: HistoryTools(source_dir).tools()  # noqa: E731
        n = cfg.max_step_turns
        ports.reviewer = agent_steps.make_reviewer(client, model, handoff, tools, n)
        ports.recovery = agent_steps.make_self_recovery(client, model, handoff, tools, n)
        ports.planner = agent_steps.make_access_planner(client, model, handoff)
        ports.investigator = agent_steps.make_access_investigator(client, model, handoff, tools, n)
    return ports


def _add_sandbox_resources(catalog: AccessCatalog, adapter, build: str, script: str) -> None:
    """
    Two resources Agent B normally can't see (by design) but the agent may request:
    the sandbox server logs while the bug happens, and the sandbox server config.
    Both are sandbox-only, so they hold no customer data.
    """
    state = {"fresh": False}

    def fresh_run() -> None:
        # The sandbox may have been switched to the last good build during verification,
        # so reset to the failing build and trigger the bug once before reading.
        if not state["fresh"]:
            adapter.reset(build)
            adapter.run_repro_script(script)
            state["fresh"] = True

    def logs(grep: str | None = None) -> str:
        fresh_run()
        return adapter.server_logs(lines=800, grep=grep)

    def config(grep: str | None = None) -> str:
        fresh_run()
        text = json.dumps(adapter.api_request("GET", "/api/v4/config").get("body"), indent=1)
        if grep:
            text = "\n".join(line for line in text.splitlines() if grep.lower() in line.lower())
        return text[:15000]

    catalog.add_reader("sandbox_server_logs", AccessKind.LOGS,
                       "Server logs from the sandbox while the bug happens (Agent B normally can't see these)", logs)
    catalog.add_reader("sandbox_server_config", AccessKind.TENANT_CONFIG,
                       "Server config and feature flags of the sandbox that mirrors the affected tenant", config)
