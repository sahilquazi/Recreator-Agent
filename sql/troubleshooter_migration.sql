-- Run once in the Supabase SQL Editor if recreator_runs already exists.
-- Adds the column the troubleshooter writes to. Safe to run twice.
alter table public.recreator_runs add column if not exists troubleshooting jsonb;

-- Earlier runs of a ticket are read to enforce "one access request / one admin round per case".
create index if not exists recreator_runs_ticket_id_idx on public.recreator_runs (ticket_id);
