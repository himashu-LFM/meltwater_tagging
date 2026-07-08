-- Meltwater Sentiment Tagger — Supabase schema
-- Run this once in Supabase SQL Editor (Project -> SQL Editor -> New query).
-- Uses Supabase Auth (auth.users) for login; these tables hold app-specific data.

-- ---------------------------------------------------------------------------
-- 1) Brands — replaces the hardcoded Kaseya/Ninja taxonomy list.
--    Analysts pick a brand in the UI; adding a new brand is an insert here,
--    no code change needed.
-- ---------------------------------------------------------------------------
create table if not exists brands (
  id serial primary key,
  name text not null unique,                 -- canonical brand name, e.g. 'Kaseya'
  tag_format text not null default '{brand} - {sentiment}',  -- matches this account's tag convention
  roll_up_terms text[] not null default '{}', -- e.g. {datto, "it glue", autotask} for Kaseya family
  meltwater_topic_url text,                  -- saved-search/topic URL in Meltwater for this brand,
                                              -- used so "Apply to Meltwater" can jump straight there
  active boolean not null default true,
  created_at timestamptz not null default now()
);

-- seed the two brands already in use
insert into brands (name, roll_up_terms) values
  ('Kaseya', array['kaseya','datto','it glue','itglue','autotask','unitrends','rocketcyber','graphus','id agent','pulseway','saas alerts','backupify','bullphish','vonahi']),
  ('Ninja', array['ninja','ninjaone'])
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 1b) Per-brand tag list + rules. Each brand has up to three sentiment tags,
--     each with the EXACT Meltwater tag label and an optional analyst-authored
--     rule the classifier must follow for that brand. When a brand has no rows
--     here (or empty rules), classification falls back to the default behaviour
--     — so accuracy is never affected unless a rule is deliberately added.
-- ---------------------------------------------------------------------------
create table if not exists brand_tags (
  id serial primary key,
  brand_id int not null references brands(id) on delete cascade,
  sentiment text not null check (sentiment in ('positive','negative','neutral')),
  tag_label text not null,              -- exact Meltwater tag, e.g. 'Kaseya - negative'
  rule text,                            -- optional per-tag guidance for the classifier
  active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (brand_id, sentiment)
);

alter table brand_tags enable row level security;

create policy "brand_tags readable by signed-in users" on brand_tags
  for select using (auth.role() = 'authenticated');
create policy "brand_tags writable by signed-in users" on brand_tags
  for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');

-- ---------------------------------------------------------------------------
-- 2) Per-user Meltwater credentials — each analyst has their own Meltwater
--    login. Editable later from their profile page. One row per user.
--    NOTE: password is stored using Supabase Vault / pgsodium in production;
--    see note at the bottom of this file for the encrypted-column option.
-- ---------------------------------------------------------------------------
create table if not exists meltwater_credentials (
  user_id uuid primary key references auth.users(id) on delete cascade,
  meltwater_email text not null,
  meltwater_password text not null,   -- see encryption note below before going live
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 3) Per-user Reddit session cookie — fallback fetch method until the Reddit
--    Data API key is available. One cookie string per user, editable.
-- ---------------------------------------------------------------------------
create table if not exists reddit_sessions (
  user_id uuid primary key references auth.users(id) on delete cascade,
  cookie_value text not null,          -- the reddit_session cookie value the user pastes
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 4) Run history — one row per classification/apply run, so the History page
--    can list past runs per user with brand, counts, and a link to the export.
-- ---------------------------------------------------------------------------
create table if not exists tagging_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  brand_id int references brands(id),
  brand_name text not null,
  status text not null default 'classified', -- 'classified' | 'applied' | 'failed'
  total_posts int not null default 0,
  applied_count int not null default 0,
  negative_count int not null default 0,
  positive_count int not null default 0,
  neutral_count int not null default 0,
  flagged_count int not null default 0,
  results jsonb not null default '[]',       -- the per-post results (permalink, tag, reason, action)
  created_at timestamptz not null default now()
);

create index if not exists idx_tagging_runs_user on tagging_runs(user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Row Level Security — each analyst only sees their own credentials/history.
-- Brands table is readable by everyone signed in (shared taxonomy).
-- ---------------------------------------------------------------------------
alter table meltwater_credentials enable row level security;
alter table reddit_sessions enable row level security;
alter table tagging_runs enable row level security;
alter table brands enable row level security;

create policy "own creds" on meltwater_credentials
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "own reddit session" on reddit_sessions
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "own runs" on tagging_runs
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "brands readable by signed-in users" on brands
  for select using (auth.role() = 'authenticated');

-- brands is a shared, org-wide taxonomy -- any signed-in analyst can add a
-- brand or update its Meltwater topic URL. (The Flask backend always uses the
-- service_role key, which bypasses RLS entirely -- these two policies are a
-- safety net for direct/anon access and for correctness if that ever changes.)
create policy "brands insertable by signed-in users" on brands
  for insert with check (auth.role() = 'authenticated');

create policy "brands updatable by signed-in users" on brands
  for update using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');

create policy "brands deletable by signed-in users" on brands
  for delete using (auth.role() = 'authenticated');

-- ---------------------------------------------------------------------------
-- NOTE on secrets: meltwater_credentials.meltwater_password and
-- reddit_sessions.cookie_value are stored as plain text columns here to keep
-- the schema simple to stand up. Before broader rollout, either:
--   (a) enable Supabase Vault and store these as vault secrets, referencing
--       the secret id instead of the raw value, or
--   (b) encrypt at the application layer (e.g. Fernet with a server-side key
--       from an env var) before insert, and decrypt only server-side.
-- The Flask backend in this project uses the service_role key server-side
-- only, so these columns are never exposed directly to the browser -- but
-- they are still plaintext at rest in Postgres until (a) or (b) is applied.
