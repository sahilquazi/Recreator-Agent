-- Run once in the Supabase SQL Editor.

-- 1) Where the pipeline writes its results. The ticket table is never modified.
create table if not exists public.recreator_runs (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  ticket_id   bigint not null,
  status      text   not null,   -- triage_only | reproduced | not_reproduced | diagnosed | blocked_pii | error
  triage      jsonb,
  repro       jsonb,
  handoff     jsonb,
  diagnosis   jsonb,
  error       text
);

-- 2) Turn on row-level security. With no policies, only the service role
--    (used by the pipeline on a trusted machine) can read or write.
alter table public.recreator_runs enable row level security;
alter table public."ReportedIssues" enable row level security;
