-- Run once in the Supabase SQL Editor.

-- 1) Where the pipeline writes its results. The ticket table is never modified.
create table if not exists public.recreator_runs (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  ticket_id   bigint not null,
  status      text   not null,   -- triage_only | reproduced | not_reproduced | diagnosed | blocked_pii | error
                                 -- with the troubleshooter: fixed | handed_off | waiting_on_admin
  triage      jsonb,
  repro       jsonb,
  handoff     jsonb,
  diagnosis   jsonb,
  troubleshooting jsonb,         -- failure reason, steps, admin status and the case file
  solution_summary jsonb,        -- the engineer write-up (recreator/solution_summary); same shape the portal reads
  error       text
);
-- Already created the table before the troubleshooter? Run sql/troubleshooter_migration.sql instead.
-- Before the Solution Summary? Run sql/solution_summary_migration.sql.

-- 2) Turn on row-level security. With no policies, only the service role
--    (used by the pipeline on a trusted machine) can read or write.
alter table public.recreator_runs enable row level security;
alter table public."ReportedIssues" enable row level security;
