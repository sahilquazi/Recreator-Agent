-- Run once in the Supabase SQL Editor if recreator_runs already exists.
-- Adds the column the Solution Summary step writes to (recreator/solution_summary). Safe to run twice.
-- Until it exists, store.save_run saves runs without the summary and prints a reminder.
alter table public.recreator_runs add column if not exists solution_summary jsonb;
