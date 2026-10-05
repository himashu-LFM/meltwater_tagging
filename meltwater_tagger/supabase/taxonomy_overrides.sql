-- Bentley — client-edited tag list (Brand studio → "Tag list")
-- Run this ONCE in Supabase SQL Editor. Creates ONE new table; does not touch
-- any existing table, row or policy. Safe to re-run.
--
-- One row per brand; `overrides` uses the taxonomy_overrides.json format:
--   {"add":    {"PRODUCT": [{"label": "Product - X", "aliases": ["X"]}]},
--    "remove": {"PRODUCT": ["Product - Y"]},
--    "spokespeople_add": [{"name": "..."}], "spokespeople_remove": ["..."]}
-- The app applies it on top of the code's base taxonomy at classify/apply time.
-- No row (the default) = the tag list in the code, unchanged.

create table if not exists taxonomy_overrides (
  brand_name  text primary key,
  overrides   jsonb not null default '{}'::jsonb,
  updated_by  uuid references auth.users(id),
  updated_at  timestamptz not null default now()
);

alter table taxonomy_overrides enable row level security;

drop policy if exists "taxonomy_overrides readable by signed-in users" on taxonomy_overrides;
create policy "taxonomy_overrides readable by signed-in users" on taxonomy_overrides
  for select using (auth.role() = 'authenticated');

drop policy if exists "taxonomy_overrides writable by signed-in users" on taxonomy_overrides;
create policy "taxonomy_overrides writable by signed-in users" on taxonomy_overrides
  for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');
