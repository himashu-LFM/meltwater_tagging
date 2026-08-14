-- Bentley — brand row + client-feedback storage
-- Run this ONCE in Supabase SQL Editor (Project → SQL Editor → New query → Run).
-- Safe to re-run: everything is "if not exists" / "on conflict do nothing".
--
-- What this adds:
--   1) the "Bentley" brand row (so it shows in the dashboard + links run history)
--   2) feedback_docs   — raw client "Tagging Adjustments" docs we upload
--   3) feedback_rules  — the discrete rules extracted from those docs, which the
--                        Bentley classifier reads and appends to its prompt
--
-- Why NOT reuse brand_tags for this: brand_tags is locked to 3 sentiments
--   (check: sentiment in 'positive'/'negative'/'neutral'), which does not fit
--   Bentley's ~40 taxonomy tags. feedback_rules is the Bentley-shaped home.
--
-- These are ORG-SHARED (every analyst's Bentley run uses the same rules), so the
-- RLS policies mirror brand_tags: any signed-in user can read/write.

-- ---------------------------------------------------------------------------
-- 1) Bentley brand
-- ---------------------------------------------------------------------------
insert into brands (name) values ('Bentley')
on conflict (name) do nothing;

-- ---------------------------------------------------------------------------
-- 2) feedback_docs — one row per uploaded client feedback document
-- ---------------------------------------------------------------------------
create table if not exists feedback_docs (
  id          uuid primary key default gen_random_uuid(),
  brand_id    int references brands(id) on delete cascade,
  brand_name  text not null default 'Bentley',
  filename    text,
  raw_text    text,                         -- extracted text of the doc
  uploaded_by uuid references auth.users(id),
  created_at  timestamptz not null default now()
);

create index if not exists idx_feedback_docs_brand
  on feedback_docs(brand_name, created_at desc);

alter table feedback_docs enable row level security;

create policy "feedback_docs readable by signed-in users" on feedback_docs
  for select using (auth.role() = 'authenticated');
create policy "feedback_docs writable by signed-in users" on feedback_docs
  for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');

-- ---------------------------------------------------------------------------
-- 3) feedback_rules — discrete rules the classifier follows (LAYER 2, DB-backed)
--    Seeded from docs OR added by hand. `active` lets a rule be toggled off
--    without deleting it. `category` groups rules for the prompt.
-- ---------------------------------------------------------------------------
create table if not exists feedback_rules (
  id          uuid primary key default gen_random_uuid(),
  brand_id    int references brands(id) on delete cascade,
  brand_name  text not null default 'Bentley',
  doc_id      uuid references feedback_docs(id) on delete set null,  -- which doc it came from (nullable)
  category    text,             -- scope | region | coverage | pillar | product | industry | corporate | spokesperson | general
  rule_text   text not null,    -- the actual instruction the classifier reads
  example_url text,             -- optional: the article that prompted this rule (keep for the test set)
  active      boolean not null default true,
  created_by  uuid references auth.users(id),
  created_at  timestamptz not null default now()
);

create index if not exists idx_feedback_rules_brand_active
  on feedback_rules(brand_name, active);

alter table feedback_rules enable row level security;

create policy "feedback_rules readable by signed-in users" on feedback_rules
  for select using (auth.role() = 'authenticated');
create policy "feedback_rules writable by signed-in users" on feedback_rules
  for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');
