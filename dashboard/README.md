# Wallapop Tracker Dashboard

A read-only monitoring dashboard for the [Wallapop GPU deal-tracker](../README.md). It
reads the same Supabase project the tracker writes to — `searches`, `listings`,
`observations`, `model_prices`, `sent_alerts`, `junk_exclusions` — and renders
five views: Overview, Deals & Alerts, Models, Listings, and Searches & Junk.

The only write in the whole app is toggling `searches.active`.

It re-implements two pieces of tracker logic, both in `src/lib/constants.ts`:
the fee model and deal gate (`config.py`'s `FeeModel` + `pricing.evaluate`),
because those numbers are needed to render a margin and are not persisted
anywhere the dashboard can read them; and the scope rejections `alert_loop`
applies before a listing is ever evaluated. That port is the app's main
maintenance hazard; see [Fee model](#fee-model-must-match-the-tracker) and
[Alert scope](#alert-scope-a-margin-is-not-enough).

## Stack

Next.js (App Router) + TypeScript, Tailwind CSS + shadcn/ui (Base UI primitives),
Recharts, lucide-react, `@supabase/supabase-js`. Deployed on Vercel.

## Setup

### 1. Environment

```bash
cp .env.example .env.local
```

Fill in:

- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` — same project as the tracker
  (Project Settings → API → service_role key). This key is read only on the
  server (`src/lib/supabase/server.ts` imports `server-only` to make it a
  build error to pull it into a client component) and must never be exposed
  to the browser.
- `DASHBOARD_PASSWORD` / `SESSION_SECRET` — **both required**; see
  [Authentication](#authentication) below. Without them the app serves 503 on
  every route.
- The fee-model constants, and the alert-scope constants (`ALERTING_FAMILIES`,
  `BLOCKED_CONDITIONS`, `BLOCKED_SELLERS`) — **these must match what the
  tracker is actually running with**, not just the defaults here. Check the tracker's own
  `.env.local` / deployed env before you rely on any net-margin figure or deal
  in this dashboard. See [Fee model](#fee-model-must-match-the-tracker).

### 2. Database views (optional but recommended)

`supabase-views.sql` adds a read-only view that replaces a ~130-request
fan-out on the Searches page with one grouped query. Apply it by hand in the
Supabase SQL editor, the same way the tracker's own `../schema.sql` is
deployed. The dashboard detects whether the view exists and falls back to
computing the same numbers with paged scans, so this is a performance fix
rather than a correctness one — both paths are exact.

### 3. Install & run

```bash
npm install
npm run dev
```

### 4. Deploy

```bash
npx vercel link      # first time only
npx vercel deploy     # preview
npx vercel deploy --prod
```

Set the same env vars in the Vercel project (Settings → Environment
Variables) — they won't be picked up from `.env.local`.

## Design decisions worth knowing about

- **No Supabase Realtime subscription in the browser.** The spec asked for a
  live-updating alerts feed via Realtime, but that requires shipping a
  Supabase key to the client, and this project's tables have no RLS
  policies or anon grants set up (only the service-role key can read them).
  Rather than either exposing the service-role key or opening up anonymous
  read access to the whole schema, the Overview page's alert feed instead
  polls a server-side route (`/api/alerts/recent`, itself behind the same
  password gate) every 8 seconds and animates in new rows. No credentials
  ever reach the browser. If you do want true Realtime, add an anon key +
  RLS read policies scoped to what the dashboard needs and swap the poller
  for a `supabase-js` client-side subscription.
- **Per-search listing/alert counts are approximate — but exact for what they
  measure.** `listings` and `sent_alerts` aren't tagged with the search that
  discovered them (no `search_id` column in `schema.sql`), so there's no exact
  join available; the Searches page matches on each search's `model_key` and
  shows `—` for searches without one (the broad "Discovery …" searches). That
  approximation is called out in the UI. What is *not* approximate any more is
  the counting itself: it used to fetch every `item_id` for a model in one
  un-paged request, so any model with more listings than PostgREST's row cap
  silently reported too few alerts. Counts now come from a grouped view, or
  from a complete paged scan when the view isn't installed.
- **Ceilings come from `model_prices` where possible.** `buy_ceiling` and
  `buy_ceiling_in_person` are read straight off the row the tracker wrote, so
  they can't drift. They are only recomputed locally in two cases: when the
  column is null, and when `is_seed` is true — the tracker itself recomputes a
  seeded ceiling with `SEED_MARGIN_MULTIPLIER` rather than trusting the stored
  one, because that number was derived from a hand-written guess and has to
  clear a higher bar until real comps replace it. The deal list mirrors that.
- **The live-deals list is complete, or says so.** It used to fetch the 500
  most recently seen active listings and filter them in JavaScript, so past 500
  active rows deals silently vanished from the page based on nothing but
  `last_seen` ordering. Everything expressible in PostgREST (status, sanity
  band, price cap) is now pushed into the query and the remainder is read in
  full, in pages. If the `MAX_SCAN_ROWS` safety bound in `src/lib/queries.ts`
  is ever hit, the Overview panel says the list may be incomplete rather than
  quietly dropping rows.
- **The deal list applies the tracker's scope rejections, not just its margin
  gate.** `listings` holds every row *both* loops write, and the comps loop
  writes plenty the tracker would never trade. Clearing the margin gate was
  therefore necessary but nowhere near sufficient, and the gap was not
  academic: measured against the live database, 220 listings were being shown
  as live deals and only 37 of them were ones the bot would actually alert on.
  The other 183 were almost all iPhones — 1,043 of the 1,354 candidate rows are
  handsets, tracked for their comps since the registry gained them and barred
  from alerting by `alert_loop.ALERTING_FAMILIES`. Family, whole machines,
  blocked sellers and blocked conditions are all recorded on the `listings`
  row, so these are applied exactly rather than approximated. See
  [Alert scope](#alert-scope-a-margin-is-not-enough).
- Soft caps on a few queries (alerts, junk log, listings) keep the personal-
  scale dataset this is built for fast; raise them in `src/lib/queries.ts`
  if the tracker runs long enough to need it.
- **Comp counts prefer `n_own` over `n_comps`.** `n_comps` includes comps
  borrowed from sibling SKUs; `n_own` is what the model actually has of its
  own. "12 real 4060 Ti 16GB comps" and "1 of its own plus 11 borrowed from the
  8GB card" are very different claims about how much to trust a ceiling, so the
  Models page and the confidence badge use `n_own` and show the borrowed
  remainder separately. The column is a recent tracker addition, so every read
  falls back to `n_comps` when it is absent or null.

## Authentication

A single shared password gates the whole app. Every page is a `force-dynamic`
service-role read of the owner's full listing history, alert history and
learned prices, and `toggleSearchActive` writes to the live bot configuration,
so none of it is safe to leave on a public URL.

Two env vars are required, and the app **fails closed** without them: if
either is missing, `src/proxy.ts` returns 503 on every route rather than
serving anything. Neither value is ever logged.

| Variable | Purpose |
| --- | --- |
| `DASHBOARD_PASSWORD` | The shared password. Compared in constant time. |
| `SESSION_SECRET` | HMAC key for the session cookie. `openssl rand -hex 32`. |

How it fits together:

- **`src/proxy.ts`** gates every route. This is a *proxy*, not a
  `middleware.ts` — Next 16 renamed the file convention and the exported
  function, and a file named `middleware.ts` is simply never loaded, which is a
  silent failure mode for a security control. Its matcher excludes only build
  assets and the favicon; `/api` is deliberately **not** excluded, since
  `/api/alerts/recent` returns the same data the pages do.
- **`/login`** takes the password, compares it against `DASHBOARD_PASSWORD`
  with a constant-time comparison of SHA-256 digests (so neither the length nor
  the position of the first wrong character leaks through timing), and on
  success sets the session cookie.
- **The cookie** is `wp_dash_session`, HttpOnly, SameSite=Lax, `Secure` in
  production, valid 12 hours. Its value is
  `base64url(payload).base64url(HMAC-SHA256(payload))`, where the payload
  carries its own `exp`. Verification checks the signature *before* parsing
  the payload, and re-checks the expiry from inside the signed data — a client
  can rewrite a cookie's own attributes, but not the payload the HMAC covers.
- **Signing uses Web Crypto**, not `node:crypto`, so one implementation stays
  valid whether Proxy runs on the Node runtime (the Next 16 default) or at the
  edge.
- **Server Actions re-check the session themselves**
  (`src/lib/auth/require-session.ts`). Proxy gating alone is not enough: a
  Server Action is a POST to whichever route imports it, with an action id that
  ships in the client bundle, and Next's own docs warn that a matcher change or
  a refactor can silently remove Proxy coverage. `toggleSearchActive` mutates
  the owner's bot config, so it gets its own check; the read-only actions and
  the polled API route do too.
- **Logout** is a Server Action (`logout`), wired to a control in the sidebar
  and the mobile nav.

If the deployment URL was ever public, **rotate
`SUPABASE_SERVICE_ROLE_KEY`** — see the note at the end of this file.

## Fee model: must match the tracker

`src/lib/constants.ts` is a hand-maintained TypeScript port of `config.py`'s
`FeeModel` plus the deal gate in `pricing.py`'s `evaluate()`. Its header
comment lists every env var that has to agree between the two. **Two
independent implementations of the same maths is a standing source of bugs**,
and it has already drifted once: the in-person ceiling ignored `MARGIN_RATE`
(every ceiling above ~278 EUR was too high — 61 EUR too high at a 620 EUR
reference), the deal gate compared the raw asking price rather than the
haggled offer, and `MIN_PLAUSIBLE_RATIO` was not applied at all, which on a
list sorted by margin descending is precisely the ordering that puts replicas
and empty boxes at the top.

The real fix is for the tracker to persist or expose these numbers so there is
one source of truth. Until then, check both environments whenever either
changes.

## Alert scope: a margin is not enough

`pricing.evaluate()` is not the only gate the tracker applies, and porting only
that function is how this dashboard spent a long time disagreeing with the
alert feed about what a deal is. Before a listing reaches `evaluate()`,
`alert_loop`'s candidate loop rejects four classes of listing outright:

| Rejection | Tracker | `listings` column |
| --- | --- | --- |
| Non-alerting family | `alert_loop.ALERTING_FAMILIES` | `family` |
| Whole machines (prebuilts, laptops) | `item.whole_machine` | `whole_machine` |
| Blocked sellers | `config.BLOCKED_SELLERS` | `seller_id` |
| Bottom condition tier | `config.BLOCKED_CONDITIONS` | `condition` |

None of these live in `pricing.py`, and none of them are visible to margin
maths — a 300 EUR iPhone against a 415 EUR reference clears a buy ceiling just
as cleanly as a graphics card does. The comps loop writes those rows *on
purpose*: iPhone 15–17 are in the registry so the tracker can learn what they
resell for, which the owner asked for as data without alerts. They are input to
the pricing engine, never candidates for a trade.

`alertScopeRejection()` in `src/lib/constants.ts` is the port, and
`getLiveDeals` applies it. Two of the four are pushed into PostgREST so they
don't burn `MAX_SCAN_ROWS`; all four are re-checked in JavaScript.

Every check needs *positive* evidence to reject, which matters more than it
looks: `family` is a recent column, so a GPU row written before it existed
carries null. A bare `family = 'gpu'` filter would enforce a rule aimed at
handsets by silently hiding real deals — the same shape of bug as the one it
was written to fix.

Three env vars keep this in step with the tracker — `ALERTING_FAMILIES`,
`BLOCKED_CONDITIONS`, `BLOCKED_SELLERS`. `BLOCKED_SELLERS` is the one that will
drift in practice, since it grows whenever a replica seller is spotted; a seller
worth blocking from the alert feed is one worth hiding here.

## Outstanding operational tasks

- **Rotate `SUPABASE_SERVICE_ROLE_KEY` if this dashboard was ever deployed
  without the password gate.** Before the gate existed, anyone with the URL
  could read the tracker's entire history and toggle its searches. The key
  itself was never sent to the browser — all Supabase access happens in Server
  Components and Server Actions — so it was not directly exposed, but if the
  deployment URL was ever public the prudent assumption is that the data behind
  it was too. Rotate in Supabase (Project Settings → API → service_role →
  rotate), then update the env var in both Vercel and the tracker's own
  environment, since they share the project.
- ~~This app is not in version control yet.~~ **Done** — `dashboard/` is
  tracked in the parent repo (86 files as of this writing) and the working tree
  is clean. `.gitignore` excludes `.env*`, with an explicit `!.env.example`
  negation so the template stays tracked, and `.vercel`.
- **Apply `supabase-views.sql`** in the Supabase SQL editor when convenient.
  Optional — see step 2 of Setup.
