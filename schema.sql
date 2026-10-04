-- Wallapop GPU deal-tracker — Supabase / Postgres schema
-- Run this once in the Supabase SQL editor.
--
-- Columns marked [ext] are extensions beyond the original spec that the
-- sale-inference and audit logic needs. Everything else is spec §4 verbatim.

-- ---------------------------------------------------------------- searches
create table if not exists searches (
  id            bigint generated always as identity primary key,
  label         text not null,
  role          text not null check (role in ('alert','comps')),
  keywords      text not null,
  model_key     text,              -- canonical model this search maps to, if specific
  category_ids  text,              -- e.g. '24200'
  min_price     numeric,
  max_price     numeric,           -- null for comps (uncapped)
  distance_km   int,
  active        boolean default true,
  unique (label)
);

-- ---------------------------------------------------------------- listings
create table if not exists listings (
  item_id       text primary key,
  title         text,
  description   text,
  model_key     text,              -- classified canonical model (nullable)
  confidence    text,              -- [ext] 'high' | 'medium' | 'low' | null
  first_seen    timestamptz default now(),
  last_seen     timestamptz default now(),
  last_price    numeric,
  last_status   text,              -- 'active' | 'reserved' | 'closed'
  web_url       text,
  image_url     text,
  shipping      boolean,
  location      text,              -- [ext] city / area string
  distance_km   numeric,           -- [ext] distance from search centre
  ever_reserved boolean default false,  -- [ext] sale-inference input
  missing_runs  int default 0,          -- [ext] consecutive comps runs unseen
  closed_at     timestamptz,            -- [ext] when inferred closed
  sold_price    numeric                 -- [ext] last price while reserved
);

create index if not exists listings_model_key_idx on listings (model_key);
create index if not exists listings_last_seen_idx on listings (last_seen);

-- The index db.get_open_listings_for_models actually needs.
--
-- That query is `model_key in (...) and last_status in ('active','reserved')
-- and last_seen >= cutoff`, and neither single-column index above spans it:
-- Postgres picks one, then filters the rest by hand. That was survivable at
-- 43k rows and stopped being survivable at 100k — a 50-key batch measured 7.5s
-- against Supabase's 8s statement timeout, so the comps loop crashed with
-- 57014 on roughly every other run through early October 2026.
--
-- Partial, because the rows this query wants are a small and permanently
-- shrinking fraction of the table: 2653 open listings out of 104343 on
-- 2026-10-03. Keeping the closed rows out makes the index a fraction of the
-- size and, more usefully, means it does not grow as history accumulates —
-- only as the live market does.
--
-- `last_seen` is the second column rather than the first so the index serves
-- the equality predicate before the range one, which is the order that lets a
-- single scan satisfy both.
-- Applied to the live project on 2026-10-04. db.get_open_listings_for_models
-- sorts by (model_key, last_seen, item_id) to match this column order exactly,
-- so one scan serves both the filter and the sort; ordering by last_seen first
-- instead makes the planner prefer listings_last_seen_idx and filter ~54k rows
-- by hand. If you change one, change the other.
create index if not exists listings_open_by_model_idx
  on listings (model_key, last_seen)
  where last_status in ('active', 'reserved');

-- ------------------------------------------- [ext] structured API fields
-- Wallapop returns all of these; they were previously being re-derived from
-- free text (or ignored). Written as idempotent ALTERs so an existing project
-- can be migrated by re-running this file.
alter table listings add column if not exists condition text;
  -- un_opened|in_box|new|as_good_as_new|good|fair|has_given_it_all
alter table listings add column if not exists brand text;
alter table listings add column if not exists taxonomy int[];
alter table listings add column if not exists whole_machine boolean default false;
  -- true when taxonomy lands in a laptop/prebuilt leaf: excluded from comps
  -- (its price is not a comp for a loose card) but NOT from alerts.
alter table listings add column if not exists posted_at timestamptz;
  -- the seller's real created_at, not when we first saw it
alter table listings add column if not exists user_allows_shipping boolean;
  -- this seller's choice on this listing, distinct from `shipping`
  -- (item_is_shippable), which is only the category's general capability
-- Product family and capacity. Both were removed once, when phone tracking was
-- reverted and nothing wrote them; both are back because iPhones are tracked
-- again — for comps only, never for alerts (alert_loop.ALERTING_FAMILIES).
--
-- `family` is what makes a mixed registry safe to query: "what is a 4070 worth"
-- and "what is a 15 Pro worth" are the same question over the same tables, and
-- without this column the only way to tell a card row from a handset row is to
-- pattern-match the model_key.
alter table listings add column if not exists family text;
  -- 'gpu' | 'phone' | 'console'
  -- Only 'gpu' may ever produce an alert (alert_loop.ALERTING_FAMILIES).
  -- Phones (iPhone 15-17) and consoles (PS5, Xbox Series X) are tracked so the
  -- bot learns what they resell for, and nothing more.
alter table listings add column if not exists storage text;
  -- phones: '128gb' | '256gb' | '512gb' | '1tb' — the biggest price driver
  -- within one model, recorded now so the pools can be split by capacity later
  -- without a backfill that no longer has the titles to parse.
create index if not exists listings_family_idx on listings (family);

alter table listings add column if not exists country text;
  -- 'ES' | 'PT' | 'IT' | ... Wallapop is one shared cross-border marketplace
  -- and both loops search it nationwide, so the country is the only thing
  -- distinguishing a card you can collect from one that has to ship from Milan.
  -- parse_item has extracted it (and a test has asserted it) since the
  -- nationwide switch, and alert_loop.listing_row has written it — but the
  -- column was never created, so every fresh process sent one batch that failed
  -- with PGRST204, logged the "apply the ALTER statements" warning, dropped the
  -- field and carried on. The value was never once persisted.
alter table listings add column if not exists seller_id text;
  -- the seller behind the listing, and the cheapest noise filter available:
  -- the same replica or empty-box listing reappears under a fresh item_id every
  -- few days, which defeats the (item_id, price) alert dedup completely. The
  -- seller is the only identifier that survives a relisting.
  -- config.BLOCKED_SELLERS is applied against this.
alter table listings add column if not exists modified_at timestamptz;
  -- the seller's own last-edit timestamp. This is the "just cut the price"
  -- signal; without it the alert loop can only infer a cut from its own
  -- sent_alerts history, so a price cut on a listing it never alerted on is
  -- invisible to it.

create index if not exists listings_posted_at_idx on listings (posted_at desc);

-- whole_machine feeds a `not whole_machine` predicate in the sold-comps query,
-- and in Postgres `= false` excludes NULL. Every row written before the column
-- existed — or during a window where db.Database._missing_columns had stripped
-- it from the payload — carries NULL there and was permanently invisible to
-- that query, silently shrinking the sold pool with no way to notice.
--
-- The query side is now NULL-tolerant, but a column whose meaning depends on
-- every reader remembering that is a trap, so the invariant is made true at the
-- storage layer as well. All three statements are safe to re-run: the update
-- matches nothing once it has run, and set default / set not null are no-ops on
-- a column that already has them.
update listings set whole_machine = false where whole_machine is null;
alter table listings alter column whole_machine set default false;
alter table listings alter column whole_machine set not null;

-- ------------------------------------------------------------ observations
create table if not exists observations (
  id         bigint generated always as identity primary key,
  item_id    text references listings(item_id) on delete cascade,
  model_key  text,                 -- [ext] denormalised so comps queries are one hop
  price      numeric,
  status     text,                 -- 'active' | 'reserved'
  seen_at    timestamptz default now()
);

create index if not exists observations_model_seen_idx
  on observations (model_key, status, seen_at desc);
create index if not exists observations_item_idx on observations (item_id, seen_at desc);

-- Retention needs a plain seen_at index and neither of the two above can serve
-- one: a leading model_key or item_id column is useless to
-- `order by seen_at limit 1` (db.Database._oldest) or to the bare seen_at range
-- delete each purge slice issues. On the largest table in the database that is
-- a sequential scan per slice, which is precisely the statement-timeout (57014)
-- failure the slicing logic was written to avoid — and when the purge times out
-- nothing is deleted, so the table only grows and the next run fails harder.
-- junk_exclusions has had this index since it blew the quota; observations,
-- which is bigger, did not.
create index if not exists observations_seen_at_idx on observations (seen_at);

-- ------------------------------------------------------------- model_prices
create table if not exists model_prices (
  model_key    text primary key,
  ref_price    numeric,            -- trimmed median of sold+reserved, 30d
  n_comps      int,
  buy_ceiling  numeric,            -- max buy price for >=50 EUR margin (shipped)
  buy_ceiling_in_person numeric,   -- [ext] same, zero-fee local pickup
  updated_at   timestamptz default now(),
  is_seed      boolean default false
);

-- ------------------------------------------- [ext] comps provenance
-- Asking prices are free to post and mean nothing; these split out what the
-- reference price was actually built from.
alter table model_prices add column if not exists n_sold int default 0;
alter table model_prices add column if not exists n_reserved int default 0;
alter table model_prices add column if not exists raw_ref numeric;
  -- observed quantile before shrinkage toward the sibling prior
alter table model_prices add column if not exists shrunk boolean default false;
alter table model_prices add column if not exists median_days_to_sale numeric;
  -- how long this model actually takes to move: a 50 EUR margin in 6 days and
  -- one in 45 days are not the same trade
alter table model_prices add column if not exists n_own int default 0;
  -- of the n_comps behind this price, how many are the model's own rather than
  -- borrowed from a sibling SKU via models.GENERIC_FALLBACKS. n_comps alone
  -- cannot tell "12 real 4060 Ti 16GB comps" apart from "1 of its own plus 11
  -- borrowed from the 8GB card", and those are very different claims about how
  -- much the ceiling should be trusted.

-- -------------------------------------------------------------- sent_alerts
create table if not exists sent_alerts (
  id         bigint generated always as identity primary key,
  item_id    text,
  price      numeric,
  kind       text,                 -- 'new' | 'price_drop'
  sent_at    timestamptz default now(),
  unique (item_id, price)
);

create index if not exists sent_alerts_item_idx on sent_alerts (item_id);

-- ---------------------------------------------------- [ext] junk audit log
-- Every filtered listing, so you can tune the phrase list without guessing.
create table if not exists junk_exclusions (
  id         bigint generated always as identity primary key,
  item_id    text,
  title      text,
  phrase     text,                 -- the phrase that matched
  category   text,                 -- DEFECT | WANTED | TRADE | NOT_A_CARD
  seen_at    timestamptz default now()
);

-- One row per listing, not one per listing per run. Without this the same
-- exclusion was re-inserted every 5 minutes for as long as the listing stayed
-- up (~288 rows/day each), which reached 2.86M rows in 16 days and exhausted
-- the free-tier quota.
--
-- This index is now load-bearing rather than belt-and-braces: db.log_junk()
-- upserts on item_id with resolution=ignore-duplicates and no longer reads the
-- table back first. It used to, and it lost the race routinely — both loops run
-- overlapping schedules over a shared discovery keyword space, so both see the
-- same new junk listing within seconds, and whichever inserted second hit this
-- index and crashed its whole run over a filter-tuning table. Enforcing the
-- invariant in one place that all writers share is what makes that collision a
-- no-op instead.
--
-- THE DEDUP BELOW IS NOT OPTIONAL, and it is why this file used to appear to
-- apply cleanly while leaving the index uncreated. `create unique index` fails
-- on a table that already contains duplicate item_ids — and this table was 97%
-- duplicates, which is the entire reason the index is wanted. Postgres then
-- answers the deduplicating upsert with 42P10 ("no unique or exclusion
-- constraint matching the ON CONFLICT specification"), which took the alert
-- loop down on the first run after the upsert shipped. db.log_junk now detects
-- that and falls back, so this is a performance and hygiene fix rather than an
-- outage, but the fast path stays off until the index exists.
--
-- Keeps the newest row per item_id. Safe to re-run: a deduplicated table
-- deletes nothing, and the index creation is already idempotent.
delete from junk_exclusions a
using junk_exclusions b
where a.item_id = b.item_id
  and (a.seen_at < b.seen_at or (a.seen_at = b.seen_at and a.id < b.id));

create unique index if not exists junk_exclusions_item_uidx
  on junk_exclusions (item_id);
create index if not exists junk_exclusions_seen_at_idx
  on junk_exclusions (seen_at);

-- ------------------------------------------------------- [ext] run history
create table if not exists run_log (
  id           bigint generated always as identity primary key,
  loop_name    text,               -- 'alert' | 'comps'
  started_at   timestamptz default now(),
  finished_at  timestamptz,
  items_seen   int default 0,
  alerts_sent  int default 0,
  errors       int default 0,
  notes        text
);
