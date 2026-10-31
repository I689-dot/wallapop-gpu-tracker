# Wallapop GPU Deal-Tracker & Flip-Margin Engine

Watches Wallapop — across Spain, Portugal, Italy and everywhere else it
operates, since it's one shared marketplace — for secondhand GPUs and pushes an
instant Discord notification whenever a listing is a genuine flip. "Genuine" means all
of:

- a plausible negotiated offer (asking price minus a haggle discount, default
  20%) would clear the required margin after fees;
- the asking price is at or under **€350** (`MAX_ALERT_PRICE`) — above that the
  capital at risk stops being worth it, and it's also what keeps whole PCs and
  gaming laptops out of the feed without having to identify them;
- the price isn't *implausibly* low. Under `MIN_PLAUSIBLE_RATIO` (35%) of the
  reference it is a replica, an empty box, a spare part or bait — never a
  bargain;
- Wallapop's own condition field isn't the bottom tier (`has_given_it_all`).
  Every other tier is allowed, `fair` included — a working card with a cosmetic
  flaw is exactly the discount a flip is built on.

The fair resale price is *learned* from sold + reserved comps, never from what
active listings are asking: posting is free, so an asking price is an opinion,
while a reservation is someone actually agreeing to pay.

Runs free on GitHub Actions + Supabase, unattended.

```
             ┌──────────────────────────────┐
             │        Supabase (PG)         │
             │ searches · listings ·        │
             │ observations · model_prices ·│
             │ sent_alerts · junk_exclusions│
             └───────────────┬──────────────┘
       ~every 5 min          │         ~every 60 min
┌──────────────────┐         │        ┌───────────────────┐
│   ALERT LOOP     │◄────────┴───────►│    COMPS LOOP     │
│ nationwide/intl  │                  │ nationwide/intl   │
│ → classify       │                  │ → record reserved │
│ → junk filter    │                  │ → confirm sold    │
│ → per-family cap │                  │ → decay-weighted  │
│ → offer margin   │                  │   quantile        │
│ → condition gate │                  │ → shrink to prior │
│ → Discord        │                  │ → buy ceiling     │
└────────┬─────────┘                  └───────────────────┘
         ▼
    Discord (embed)
```

Both loops run as a **self-dispatch chain**: each GitHub Actions run pads out
to a ~5-min (alert) / ~60-min (comps) cycle, then triggers its own next run
directly through the GitHub API. This is a workaround for a measured, real
problem — GitHub's `schedule` cron trigger fired only ~once/hour on this repo
regardless of the requested interval, while every `workflow_dispatch` call
fired instantly. The original schedule stays wired up as a low-frequency
safety net in case the chain ever breaks (crash, timeout, platform outage) —
but strictly as a *revival* mechanism: a scheduled run stands down unless the
last dispatched run is older than a full cycle, because firing one while the
chain is alive forks it into two. See Known limits.

---

## Setup

### 1. Supabase

1. Create a free project at [supabase.com](https://supabase.com).
2. SQL Editor → paste all of [schema.sql](schema.sql) → Run.
3. Project Settings → API → copy the **Project URL** and the **`service_role`**
   key (server-side only — it bypasses row-level security; never ship it to a
   browser).

### 2. Discord

1. [discord.com/developers/applications](https://discord.com/developers/applications)
   → **New Application** → **Bot** → **Reset Token** → copy it.
2. **OAuth2 → URL Generator**: scope `bot`, permissions **View Channel**,
   **Send Messages** and **Embed Links**. Open the generated URL and add the bot
   to your server. No privileged intents are needed — the bot only sends.
3. Discord → Settings → Advanced → **Developer Mode** on, then right-click the
   target channel → **Copy Channel ID**.

If the bot cannot post, Discord answers 403 with a numeric error code that names
the missing permission; it is logged verbatim. A private channel needs the bot
granted access to it explicitly.

### 3. Local config

```bash
cp .env.example .env          # or .env.local — both are read, .env.local wins
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python seed.py      # loads the searches + day-one seed prices
```

### 4. Verify before deploying

```bash
.venv/bin/python -m pytest tests -q            # 233 tests, no network or DB needed
.venv/bin/python smoke_test.py "rtx 4070"      # hits the live API, touches no DB
DRY_RUN=1 .venv/bin/python alert_loop.py       # full loop, logs alerts instead of sending
```

`smoke_test.py` prints the raw response envelope plus every parsed listing with
its classification and junk verdict. **Run it first whenever alerts go quiet** —
it tells you immediately whether the API changed shape.

### 5. GitHub Actions

Push to a **public** repo (private repos only get 2,000 free minutes/month,
which can't sustain near-continuous polling). Then Settings → Secrets and
variables → Actions → add:

| Secret | |
|---|---|
| `DISCORD_TOKEN` | the bot token from step 2 |
| `DISCORD_CHANNEL_ID` | the numeric channel id from step 2 |
| `SUPABASE_URL` | project URL |
| `SUPABASE_KEY` | service_role key |
| `GH_DISPATCH_TOKEN` | a token with `repo`+`workflow` scope — needed so each run can trigger its own successor (the auto `GITHUB_TOKEN` is blocked from doing this, to prevent recursive-run loops) |

Two workflows then keep themselves running via the self-dispatch chain
described above: [alert.yml](.github/workflows/alert.yml) and
[comps.yml](.github/workflows/comps.yml). Trigger either manually from the
Actions tab once to start the chain.

---

## How the margin works

```
net = ref_price·(1 − seller_fee) − [ buy·(1 + buyer_fee) + buyer_fixed + shipping_in ]

buy_ceiling = ( ref_price·(1 − seller_fee) − buyer_fixed − shipping_in − target_margin )
              ─────────────────────────────────────────────────────────────────────────
                                        1 + buyer_fee
```

| Constant | Default | |
|---|---|---|
| `SELLER_FEE` | **0** | Wallapop charges the seller nothing — only the buyer pays a protection fee on shipped sales |
| `BUYER_FEE` | 0.075 | buyer protection % (paid when *you* are the buyer) |
| `BUYER_FIXED` | 0.69 | buyer protection fixed part |
| `SHIPPING_IN` | 4.50 | inbound shipping, boxed GPU |
| `TARGET_MARGIN` | 50 | your minimum net profit |
| `OFFER_DISCOUNT` | **0.20** | how far below asking you could realistically haggle a seller down |
| `MARGIN_RATE` | **0.18** | minimum return as a fraction of the item's value |
| `MAX_ALERT_PRICE` | **350** | cap on the asking price of anything that may alert |
| `MIN_PLAUSIBLE_RATIO` | **0.35** | below this fraction of ref, it's fraud, not a deal |
| `SEED_MARGIN_MULTIPLIER` | **1.6** | extra margin demanded while a price is still a guess |

**The margin is the greater of `TARGET_MARGIN` and `MARGIN_RATE × ref_price.`**
A flat €50 is a 25% return on a €200 card and 8% on a €620 one — nearly free at
the expensive end for the same work and the same capital at risk. The crossover
is €278, so cheap cards are governed by the flat floor exactly as before; above
it the percentage binds (a 4070 at ref €330 must clear €59 rather than €50).
`MARGIN_RATE=0` restores the old behaviour.

**A seeded price demands 1.6× that margin.** A seed is an educated guess, and
the feed is only as good as the guess: one that is 25% too high makes every
ordinary listing look like a bargain. Demanding more while confidence is low,
and relaxing automatically once `MIN_COMPS` real comps exist, is the honest way
to express that.

The gate checks a **negotiated offer**, not the raw asking price: a listing
qualifies if `asking · (1 − OFFER_DISCOUNT)` would clear `buy_ceiling`, even
when the asking price alone wouldn't — you can always propose the discount and
see if it lands. Every alert shows the suggested offer and the net margin both
at the offer and at asking.

`MAX_ALERT_PRICE` sits *above* that, on the raw asking price, before any margin
maths. Note what it is not: it's a cap on price, not on model tier. A 4090
listed at €340 still gets through and is exactly the listing worth catching —
which is why the comps loop keeps learning prices for cards nobody would ever
buy at market rate.

In-person meetups (all fees zero) use `ref − target_margin` instead — both
numbers appear in every alert, though with searches now nationwide/
international, most flips will realistically be shipped.

## What gets filtered out

Configured in [junk.py](junk.py); every exclusion is written to
`junk_exclusions` with the phrase that matched, so you can audit and tune:

```sql
select category, phrase, count(*) from junk_exclusions group by 1,2 order by 3 desc;
```

- **DEFECT** — `no funciona`, `no da video`, `para piezas`, `no enciende`,
  `caja vacia`, …
- **LEER** — a title shouting `LEER` / `LEERRR` ("read!"). On Spanish
  marketplaces that means "there's a catch, read the description", and in
  practice it flags a defect. **Title only** — descriptions say "leer la
  descripción" constantly, and matching there would gut the feed.
- **WANTED** — titles *starting* with `busco` / `compro` (so "…no busco cambios" survives)
- **TRADE** — `cambio por`, `solo cambio`
- **NOT_A_CARD** — `waterblock`, `backplate`, `soporte grafica`, …
- **BUNDLE** — `pc gaming`, `pc gamer`, `ordenador gaming`, …
- **LAPTOP** — `legion`, `portatil`, `proart p16`/`px13`, `aorus 17`, `tuf a15`, mobile CPU suffixes, …
- **CPU** — `procesador`, `microprocesador` — catches AMD Ryzen listings whose
  model number numerically collides with a Radeon GPU's (Ryzen 5 "7600" vs
  Radeon RX "7600", both bare numbers with no differentiating suffix). This one
  is deliberately narrow: not the bare `ryzen`/`amd` brand words, since those
  can legitimately appear in a real GPU title mentioning CPU compatibility.

Only the DEFECT/WANTED/TRADE lists follow the strict phrase-only rule. BUNDLE
and LAPTOP were added after live testing showed they're the dominant problem:
a "PC Gaming … RTX 4070 Super" at €850 and a "Lenovo Legion Pro 5 rtx 4070" at
€899 both classify perfectly as a 4070 and would roughly **double** its median.
The laptop list uses bare tokens, but only laptop *product lines* that can never
appear on a card — the AIB brand words (`nitro`, `rog`, `tuf`, `strix`, `aorus`,
`pulse`, `ventus`, `hellhound`) are deliberately excluded, and there are tests
pinning that.

Both form-factor checks are skipped when a title *opens* by naming a card, so
"Gráfica RTX 4070 para PC gaming" is safe.

Separately, [models.py](models.py) requires **brand-consistent** evidence before
a match is priceable. Model numbers collide across vendors — AMD's RX 7600 is
2023, NVIDIA's GeForce 7600 GS is 2006 — and a rival's branding used to count
as proof. "PC de escritorio HP P4 + Nvidia 7600GS" at €60 was classifying as a
high-confidence RX 7600 and reporting €143 of margin on a twenty-year-old card.
A wrong-vendor title now drops to `low` confidence, which the margin engine
refuses. It isn't rejected outright, because titles legitimately name both
("cambio mi RX 7600 por una Nvidia") — own-brand evidence still wins.

Every phrase is matched **on word boundaries, never as a bare substring**. This
is not pedantry: `normalise()` strips the tilde from "año", so the utterly
ordinary sentence "comprada hace 1 año, funciona perfecta" becomes
"… 1 ano funciona perfecta", in which `a[no funciona]` matches DEFECT's
"no funciona". That is the worst failure the filter can have — a working card,
described as working, silently dropped — and it's invisible, because exclusions
never produce an alert.

## What the API tells us, so we don't have to guess

Wallapop returns structured fields the bot used to re-derive from free text, or
ignore. All verified live 2026-08-02:

| Field | Used for |
|---|---|
| `type_attributes.condition.value` | the dead-card gate; 100% populated |
| `type_attributes.brand.value` | AIB brand (ASUS/MSI/Sapphire…) shown in the alert |
| `taxonomy[].id` | real category. `10304` is loose components; `24115/24116/24117/10309/10310` are whole machines |
| `created_at` (epoch ms) | true listing age, and the alert-latency metric |
| `shipping.user_allows_shipping` | whether the seller will *actually* ship — `item_is_shippable` is only a category capability, and they do disagree |
| `GET /api/v3/items/{id}` | detail; **404s once a listing is gone**, which turns sale inference from a guess into a fact |

Three of these are load-bearing enough to call out:

- **`condition` is sent as a search parameter too**, so the bottom tier never
  even travels. Belt and braces: the gate re-checks locally.
- **`category_ids` is silently ignored by the API** — a request for `10304`
  still returns gaming laptops. Category filtering only works client-side off
  `taxonomy`, which is why the alert path uses a price cap instead.
- **`order_by=newest` alone truncates the result set.** Bare, it returns ~13
  items instead of 40; with a geo and category filter it collapsed to 1.
  Pairing it with any `time_filter` restores a full page — the alert loop went
  from 34 items to 80 over the same two requests. This is *not* symmetric: the
  comps loop sorts by `most_relevance`, which already returns full pages, and
  adding a time filter there only narrows the pool (183 → 166 over five
  pages), so it deliberately doesn't send one.

## Pricing intelligence

- **Comps pool** = reserved listings ∪ listings inferred sold, one price per
  item (a card sitting reserved for weeks contributes once, not 500×). Active
  asking prices are *never* included: posting is free, so an asking price is an
  opinion, not evidence.
- **Sale inference** is now a direct check. A reserved listing missing from a
  run is looked up by id: 404 → closed immediately at its last reserved price;
  still alive → the miss counter resets, which stops a listing that merely slid
  down the search ranking from being booked as a phantom sale. Only when the
  request itself fails does it fall back to the old
  "absent from `MISSING_RUNS_FOR_SALE` runs" heuristic — a network blip must
  never read as a sale.
- **Whole machines never contribute a comp.** A prebuilt that sells for €900 is
  a real transaction, just not one in the loose card its title names, so its
  observation is written with a null `model_key`. This is the *only* place form
  factor matters; the alert path doesn't care.
- **`ref_price`** = decay-weighted trimmed quantile (`REF_PERCENTILE`, default
  the median). Each comp's weight halves every `COMPS_HALFLIFE_DAYS` (14) and a
  confirmed sale outranks a reservation (`SOLD_WEIGHT` vs `RESERVED_WEIGHT`) —
  reservations do fall through. Decay is what lets the window widen to 60 days
  without stale prices dragging the reference down.
- **Shrinkage instead of a cliff.** `MIN_COMPS` used to mean n=4 got the seed
  price and n=5 got total trust. Now the estimate blends toward a sibling-SKU
  prior, `(n·observed + PRIOR_WEIGHT·prior) / (n + PRIOR_WEIGHT)`, so
  confidence grows smoothly with evidence. `raw_ref` and `shrunk` record what
  happened.
- **Days-to-sale** is tracked per model. €50 of margin in 6 days and €50 in 45
  days are not the same trade.
- Split-VRAM SKUs (4060 Ti / 5060 Ti / 9060 XT 8 vs 16GB) get their own pools;
  a listing that omits VRAM lands in a generic key that borrows from both when
  it's short of comps.

Check on progress with:

```sql
select model_key, ref_price, raw_ref, shrunk, n_comps, n_sold, n_reserved,
       median_days_to_sale, buy_ceiling, is_seed, updated_at
from model_prices order by is_seed, n_comps desc;
```

## Tuning

| Want to | Do |
|---|---|
| Change how much you'd haggle | `OFFER_DISCOUNT` in `.env` (default 0.20) |
| Spend more (or less) per card | `MAX_ALERT_PRICE` (default 350) |
| Demand a bigger % return | `MARGIN_RATE` (default 0.18); set 0 for a flat target only |
| Quieten a noisy new category | raise `SEED_MARGIN_MULTIPLIER` (default 1.6) |
| Accept riskier bargains | lower `MIN_PLAUSIBLE_RATIO` (default 0.35) |
| Ignore cheap junk/scam listings | `MIN_SANE_PRICE` (default 50 — a real GPU below that is never genuine) |
| Also block worn cards | add `fair` to `BLOCKED_CONDITIONS` (default blocks only `has_given_it_all`) |
| Favour cards that sell fast | lower `REF_PERCENTILE` to ~0.35 — prices in selling quickly rather than eventually |
| Trust thin comps more/less | `PRIOR_WEIGHT` (default 5); lower = a handful of comps moves the price further |
| Weight recent comps harder | lower `COMPS_HALFLIFE_DAYS` (default 14) |
| Add a GPU model | one entry in `models.REGISTRY` (most-specific first) |
| Add/disable a search | edit the `searches` table, or `seed.py` and re-run it |
| Buy in person only | set `BUYER_FEE=0 BUYER_FIXED=0 SHIPPING_IN=0` (`SELLER_FEE` is always 0) |
| Silence a bad filter | remove the phrase/token from `junk.py` (check `junk_exclusions` first) |
| Run continuously instead of on cron | `python alert_loop.py --loop` on any always-on host |

## Speed

Measured end to end, 2026-08-02:

| Stage | Cost |
|---|---|
| Wallapop's own search indexing | **~150–200 s** — a new listing simply does not exist in search results before this |
| Poll gap (mean wait) | ~155 s on the 5-min Actions chain; ~22 s with `--loop` at 45 s |
| Loop execution | ~15 s |
| Discord send | <1 s |

Indexing dominates, and nothing in this repo can shorten it — which is exactly
why `LOOP_INTERVAL_SECONDS` defaults to 45 s rather than something heroic.
Polling faster than the index updates just burns requests. Realistic totals:
**~5.3 min mean on Actions, ~3.2 min on a persistent host**, against a hard
floor of ~2.5 min.

Every alert now logs its own latency (`created_at` → send), and the median lands
in `run_log.notes`, so this is measured rather than assumed.

## Known limits

- **GitHub Actions' `schedule` trigger is unreliable in practice, not just
  "best-effort."** Measured on this repo: over ~4.5 hours, the 5-minute alert
  cron and the 60-minute comps cron both fired only about once per hour —
  GitHub was silently throttling scheduled triggers regardless of the
  requested interval, while every manual/API `workflow_dispatch` fired
  instantly. Both workflows now self-dispatch their own next run instead of
  depending on `schedule` (see above); the cron stays wired up only as a
  low-frequency fallback. If this ever regresses, check the Actions tab for a
  broken chain (a run that didn't trigger its successor) before assuming the
  code is at fault.
- **The `schedule` trigger can fork the chain.** Observed live: a scheduled run
  fired while the self-dispatch chain was already alive, producing two
  independent comps chains 8 minutes out of phase for over an hour. That
  doubles API load and double-increments `missing_runs`, so a reserved listing
  could reach "sold" in half the intended number of runs. Worse, the
  concurrency group resolves it by *cancelling* a run, and a cancelled run does
  not execute `if: always()` steps — so the losing fork dies without chaining.
  Both workflows now stand down on a scheduled trigger unless the last
  dispatched run is older than a full cycle, making `schedule` purely a
  revival mechanism.
- **Scheduled workflows auto-disable after 60 days of repo inactivity.** The
  comps workflow pushes a daily `.keepalive` commit to prevent that.
- **Silence is the realistic failure, not a crash.** If the API changes shape,
  parsing yields nothing, every run "succeeds" with zero items and the feed
  just stops. A dead-man switch notifies you after `DEAD_MAN_RUNS` (3)
  consecutive empty runs.
- **`observations` grows fast** — roughly one row per listing per run, ~100k
  rows/day at a 5-minute cadence, which would fill a free Supabase project in
  weeks. The comps loop deletes anything older than
  `OBSERVATION_RETENTION_DAYS` (90).
- **Sold data is inferred, not given** — Wallapop exposes no sold feed.
  Reserved-then-vanished is a proxy; it sharpens over 1–2 weeks. Reserved prices
  alone are already a strong signal.
- **The API drifts.** `source=search_box` is currently mandatory (without it
  every request 400s), `reserved` arrives as `{"flag": false}`, and the item list
  sits at `data.section.payload.items`. All of this is read through fallback
  chains. If fetches start failing, copy fresh headers from DevTools → Network
  into `wallapop_client.HEADERS`.
- **There's no API country filter.** Wallapop runs one shared marketplace
  across Spain, Portugal, Italy etc. rather than per-country endpoints —
  `country_code`/`country` query params are silently ignored (verified live).
  Dropping lat/lon/distance from the request is what actually surfaces
  cross-border listings, which is why both loops search nationwide now.
  Distance is still computed locally from each listing's coordinates, purely
  informational since most flips will now be shipped rather than in-person.
- Automated querying is a grey area under Wallapop's ToS. This is deliberately
  personal-scale: browser-like UA, modest polling cadence, modest pagination.

## Layout

| | |
|---|---|
| [config.py](config.py) | constants, fee models, secrets |
| [wallapop_client.py](wallapop_client.py) | search API client + defensive parsing |
| [models.py](models.py) | title → canonical `model_key` registry |
| [junk.py](junk.py) | phrase/form-factor filters |
| [pricing.py](pricing.py) | comps pool, decay-weighted quantile, shrinkage, margin gate |
| [alerts.py](alerts.py) | Alert formatting + Discord delivery |
| [db.py](db.py) | Supabase persistence |
| [alert_loop.py](alert_loop.py) · [comps_loop.py](comps_loop.py) | the two entrypoints |
| [seed.py](seed.py) | searches + seed prices |
| [smoke_test.py](smoke_test.py) | live API diagnostic, no DB |
