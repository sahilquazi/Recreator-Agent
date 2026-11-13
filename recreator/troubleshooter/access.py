"""
access.py - The one-time, read-only access request.

Safety rules enforced in code (not just in the prompt):
  * The agent can only ask for a resource that is listed in the catalog. It can't
    name an arbitrary folder, URL or database.
  * Every catalog kind is read-only. There is no "write" kind to ask for.
  * A grant is tied to one case and expires: every granted tool checks that
    before it runs, and the grant is revoked as soon as the investigation ends.
  * A denial is final for that resource on that case.

The catalog is a JSON file (TROUBLESHOOT_ACCESS_CATALOG), for example:

    {
      "notification-service": {
        "kind": "source_module",
        "description": "Source of the notification service the messaging code calls",
        "path": "/src/notification-service",
        "owner_team": "Notifications",
        "approval_eta_hours": 48
      }
    }

The pipeline also adds sandbox resources at runtime (sandbox server logs and config).
"""
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel

from ..llm import Tool
from .models import AccessKind, AccessRequest, Decision, now_iso

HOME_TEAM = "ED"   # the Engineering Direct team: its own resources need only the case engineer


class CatalogEntry(BaseModel):
    name: str
    kind: AccessKind
    description: str
    owner_team: str = HOME_TEAM
    path: str | None = None              # for source_module / deploy_history (a git checkout)
    approval_eta_hours: float = 0.0      # how long approval usually takes (cross-team can be days)

    @property
    def cross_team(self) -> bool:
        return self.owner_team != HOME_TEAM


@dataclass
class AccessCatalog:
    entries: dict[str, CatalogEntry] = field(default_factory=dict)
    # Read-only functions for resources that aren't folders (logs, config), by resource name.
    readers: dict[str, Callable[..., str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None) -> "AccessCatalog":
        catalog = cls()
        if path and Path(path).is_file():
            raw = json.loads(Path(path).read_text())
            for name, spec in raw.items():
                catalog.entries[name] = CatalogEntry(name=name, **spec)
        return catalog

    def add_reader(self, name: str, kind: AccessKind, description: str, fn: Callable[..., str]) -> None:
        self.entries[name] = CatalogEntry(name=name, kind=kind, description=description)
        self.readers[name] = fn

    def describe(self) -> str:
        if not self.entries:
            return "(empty: nothing outside the current scope can be requested)"
        return "\n".join(f"- {e.name} [{e.kind.value}, owner: {e.owner_team}]: {e.description}"
                         for e in self.entries.values())


class GrantExpired(PermissionError):
    pass


@dataclass
class Grant:
    """Read-only, single-case, expiring access to one catalog resource."""
    case_id: int
    entry: CatalogEntry
    ttl_minutes: float
    _expires: float = 0.0
    _revoked: bool = False

    def __post_init__(self) -> None:
        self._expires = time.monotonic() + self.ttl_minutes * 60

    @property
    def expires_at_iso(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=self.ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def check(self, case_id: int) -> None:
        if self._revoked or time.monotonic() > self._expires:
            raise GrantExpired(f"Access to {self.entry.name} has expired.")
        if case_id != self.case_id:
            raise GrantExpired(f"Access to {self.entry.name} belongs to another case.")

    def revoke(self) -> None:
        self._revoked = True

    def tools(self, catalog: AccessCatalog) -> list[Tool]:
        """The granted resource as agent tools, each re-checking the grant before it runs."""
        name = self.entry.name

        def guard(fn: Callable[..., str]) -> Callable[..., str]:
            def guarded(**kwargs):
                self.check(self.case_id)       # raises once expired or revoked
                return fn(**kwargs)
            return guarded

        if name in catalog.readers:
            reader = catalog.readers[name]
            return [Tool(f"granted_{_slug(name)}", f"Read-only: {self.entry.description}. "
                         "Optional 'grep' filters lines.",
                         {"type": "object", "properties": {"grep": {"type": "string"}}, "required": []},
                         guard(reader))]

        if self.entry.path and self.entry.kind in (AccessKind.SOURCE_MODULE, AccessKind.DEPLOY_HISTORY):
            from .history_tools import HistoryTools  # local import: needs the source folder to exist
            tools = HistoryTools(Path(self.entry.path)).tools()
            return [Tool(f"granted_{_slug(name)}_{t.name}", f"[{name}, read-only] {t.description}",
                         t.input_schema, guard(t.fn)) for t in tools]

        raise ValueError(f"Resource {name} has no reader or path configured.")


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower())[:30]


# ---------------------------------------------------------------- approvers
class Approver(Protocol):
    def decide(self, request: AccessRequest, entry: CatalogEntry) -> tuple[Decision, str]:
        """Return (decision, who decided)."""


class StaticApprover:
    """Always the same answer. For tests and scripted demos."""
    def __init__(self, decision: Decision, who: str = "static policy"):
        self.decision, self.who = decision, who

    def decide(self, request: AccessRequest, entry: CatalogEntry) -> tuple[Decision, str]:
        return self.decision, self.who


class TerminalApprover:
    """
    Asks whoever is running the pipeline, standing in for the case engineer.
    With no terminal attached (batch runs), it denies: a denial is the safe default.
    """
    def decide(self, request: AccessRequest, entry: CatalogEntry) -> tuple[Decision, str]:
        if not sys.stdin.isatty():
            return "denied", "auto-denied (no one at the terminal to approve)"
        print(format_request(request, entry))
        teams = "case engineer" + (f" + {entry.owner_team} team" if entry.cross_team else "")
        while True:
            answer = input(f"Approve as {teams}? [a]pprove / [d]eny / [s]low (approval would take too long): ")
            choice = answer.strip().lower()[:1]
            if choice in {"a", "d", "s"}:
                return {"a": "approved", "d": "denied", "s": "too_slow"}[choice], f"terminal ({teams})"


def format_request(r: AccessRequest, entry: CatalogEntry | None = None) -> str:
    owner = f" (owned by {entry.owner_team})" if entry and entry.cross_team else ""
    return (
        "\n=== ACCESS REQUEST (one per case, read-only) ===\n"
        f"Ruled out:       {r.ruled_out}\n"
        f"Points to:       {r.points_to}\n"
        f"Asking for:      {r.asking_for}{owner}\n"
        f"Expects to find: {r.expects_to_find}\n"
        f"How it will test:{' ' + r.how_to_test}\n"
        f"Limits:          {r.limits}\n"
    )


def build_request(draft, entry: CatalogEntry | None, case_id: int) -> AccessRequest:
    return AccessRequest(
        ruled_out=draft.ruled_out, points_to=draft.points_to,
        asking_for=(f"Read-only access to {entry.name}: {entry.description}" if entry
                    else f"(not in the catalog) {draft.unlisted_need or draft.resource}"),
        expects_to_find=draft.expects_to_find, how_to_test=draft.how_to_test,
        case_id=case_id, resource=entry.name if entry else (draft.resource or ""),
        kind=entry.kind if entry else None, owner_team=entry.owner_team if entry else "",
        target_cause=draft.target_cause, requested_at=now_iso(),
    )
