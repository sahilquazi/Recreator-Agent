"""
store.py - Reads tickets from Supabase and writes results back.

* Reads ONLY an allowlist of columns, so new sensitive columns added to the
  ticket table later are never picked up by accident.
* Never edits the ticket table. Results go to a separate table (see sql/schema.sql).
"""
from supabase import create_client

from .config import Settings
from .schemas import Ticket

ALLOWED_COLUMNS = ",".join(Ticket.model_fields)  # id,title,issue_description,... (no customer_id/assigned_to)


class Store:
    def __init__(self, settings: Settings):
        self.db = create_client(settings.supabase_url, settings.supabase_key)
        self.tickets = settings.tickets_table
        self.runs = settings.runs_table

    def get_ticket(self, ticket_id: int) -> Ticket:
        rows = self.db.table(self.tickets).select(ALLOWED_COLUMNS).eq("id", ticket_id).execute().data
        if not rows:
            raise LookupError(f"No ticket with id {ticket_id}")
        return Ticket.model_validate(rows[0])

    def open_ticket_ids(self, limit: int = 20) -> list[int]:
        rows = (self.db.table(self.tickets).select("id,status")
                .not_.in_("status", ["Resolved", "Closed"]).order("id").limit(limit).execute().data)
        return [r["id"] for r in rows]

    def save_run(self, row: dict) -> None:
        try:
            self.db.table(self.runs).insert(row).execute()
        except Exception as err:  # noqa: BLE001
            # A database without the solution_summary column yet (sql/solution_summary_migration.sql): save the run
            # without it rather than lose it. The summary is still in the run folder.
            if "solution_summary" not in row or "solution_summary" not in str(err):
                raise
            print("recreator_runs has no solution_summary column yet; saved the run without it. "
                  "Run sql/solution_summary_migration.sql once.")
            self.db.table(self.runs).insert({k: v for k, v in row.items() if k != "solution_summary"}).execute()
