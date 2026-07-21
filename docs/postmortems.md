# Post-mortems

Three real failures encountered during this build. Each was found by running the real
pipeline against the live 50-ticker universe, not by a unit test — see
[docs/limitations.md](limitations.md) for related context.

---

## 1. `entity_ticker.knowledge_from` silently zeroed every historical point-in-time query (M5)

**Impact:** every `PointInTimeReader` query with `as_of` before this project's own first
ingestion run returned zero rows, for every entity, regardless of how old the underlying
fact actually was. Undetected, this would have silently invalidated M7's entire premise —
a backtest that can't read fundamentals for any historical rebalance date has nothing to
measure.

**Detection:** not a test failure. M5's own accept criteria is a live demo against a real
restatement (GE's FY2011 revenue). Building that demo — querying `as_of=2012-03-01` — returned
an empty result where a real, filed value should have been there.

**Root cause:** `core.entity_ticker.knowledge_from` was set (in M3's `_upsert_entities_and_tickers`)
to the time the ticker↔CIK map was *fetched*, not any date meaningfully tied to the ticker
itself. `PointInTimeReader` applies its `as_of` predicate uniformly, including to the join
that resolves ticker → entity — so a ticker whose mapping opened in mid-2026 (this project's
own ingestion date) could never resolve for an `as_of` before that moment, no matter how far
back the underlying fundamental or price fact went.

Every unit test passed throughout M3, M4, and M5, because every test that needed a ticker
mapping constructed one directly with a realistic `knowledge_from` of its own choosing —
the bug only exists in the gap between what a *hand-built fixture* would naturally do and
what the *real M3 pipeline* actually did.

**Fix:** a brand-new entity's *first-ever* ticker mapping now opens at a fixed sentinel
(`2000-01-01T00:00Z`), not the ingestion timestamp — SEC's ticker map is current-state-only
regardless (there is no true historical assignment date to recover), so treating it as
"always true absent better information" is the more useful reading of that same limitation.
A genuine *reassignment*, once one is ever detected, still opens at real detection time.
See `src/pdw/parse.py`'s `_upsert_entities_and_tickers`.

**Follow-up:** none needed beyond the fix itself — re-verified live against GE's real,
multiply-restated FY2011 revenue after the change. The clearest example yet of why a live
demo against real historical data, not just synthetic fixtures, is required before
signing off a point-in-time-sensitive milestone.

---

## 2. A real 10-Q collided two simultaneously-true facts under one bitemporal key (M4)

**Impact:** the very first live load of `pdw load-fundamentals` against the full universe
raised a `psycopg.errors.CheckViolation` on invariant 4 (`knowledge_from < knowledge_to`) —
the loader could not complete for the affected entity, blocking promotion from `stg` to
`core` for that company entirely.

**Detection:** every synthetic bitemporal fixture (simple amendment, double amendment,
out-of-order arrival, no-change re-fetch) passed. The failure only appeared on the first
real load against Verizon's actual EDGAR filing history.

**Root cause:** invariant 1's key was originally `(entity_id, metric_code, period_end,
source)`, per the spec as first written. A real Verizon 10-Q (accession
`0000732712-19-000052`) reports revenue for *both* the 3-month quarter and the 6-month
year-to-date window ending on the same `period_end`, under the same accession — two
genuinely different, simultaneously-true facts, not one restating the other. Keying on
`period_end` alone collided them: the loader tried to open two rows with the same key and
overlapping knowledge windows, which is exactly what invariant 1's `EXCLUDE USING gist`
constraint exists to catch.

Hand-built fixtures never exercised this because whoever writes a synthetic amendment
fixture already has the "one company, one metric, one period" mental model in mind — the
shape that breaks this key is a specific, easy-to-not-think-of quirk of how 10-Qs actually
disclose cumulative figures.

**Fix:** widened the key to `(entity_id, metric_code, period_start, period_end, source)`.
`period_start` is `NULL` for instant concepts (`Assets`, `StockholdersEquity`), so the
`EXCLUDE` constraint coalesces it to a fixed sentinel date rather than comparing raw `NULL`s
(which Postgres treats as never equal to each other, silently defeating the constraint for
exactly the rows most likely to collide). See `migrations/sql/0004_core_facts.sql`.

**Follow-up:** this same EDGAR shape (a duration fact sharing its `fiscal_period` label with
a differently-scoped fact for the same company) resurfaced twice more at M6 (`revenue_sanity`
comparing a YTD-cumulative figure against a trailing quarterly median) and M7 (`_ttm_net_income`
needed the identical duration filter to avoid double-counting). Each fix references this
incident in its own code comment — worth remembering as a *class* of EDGAR quirk, not a
one-off.

---

## 3. Full-universe price ingestion silently never completed for three milestones (M6)

**Impact:** `core.price_fact` held real data for only 2 of 50 tickers (yfinance) and zero
rows at all for Tiingo, discovered only when M6's `pdw dq run` was pointed at the live
50-ticker universe. M2 through M5 had each claimed "verified live," but none of that
verification happened to touch price data at scale — M4's live-load numbers and M5's GE
demo are both fundamentals-only.

**Detection:** `pdw dq run`'s `price_close_cross_vendor` and `price_staleness` checks did
not error — they vacuously passed with `"no comparable rows yet"` and `"no price facts
loaded yet"` respectively (correct behavior for a genuinely-empty table — a check that only
records failures cannot support a coverage metric). Nothing in the
check output distinguished "this feed is healthy and current" from "this feed has never
actually been run for the full universe." Only a direct `SELECT count(distinct entity_id)
FROM core.price_fact` — run because the *volume* of check results looked implausibly small
for 50 tickers × 10 years — surfaced the gap.

**Root cause:** M2's own accept criteria only names EDGAR explicitly ("`pdw ingest --source
edgar --universe ...` populates `raw.payload` for 50 tickers"); the two price adapters were
built and smoke-tested against a couple of tickers each, then every subsequent milestone's
"live verification" happened to reach for EDGAR-backed features (M3's coverage report, M4's
Verizon bug, M5's GE restatement) without anyone re-checking whether the price feeds had
ever been run to completion. Separately, `.env`'s `PDW_TIINGO_API_TOKEN` was still the
placeholder value from M2 — Tiingo had never been called with real credentials at all, so
even a full-universe ingest attempt would have failed with HTTP 403 until that was set.

**Fix:** ran `pdw ingest`/`pdw load-prices` for both yfinance and tiingo across the full
50-ticker universe (124,237 and 124,948 new rows respectively), after the user supplied a
real Tiingo API token.

**Follow-up:** this is a monitoring-coverage gap as much as a data gap — a vacuous pass and
a genuine "everything is fine" pass are indistinguishable in the current check output.
`pdw ops status` (this milestone) closes part of that gap by reporting `no_data` as a
distinct status from `ok`, specifically so "this feed has zero fetches ever" can never again
be silently indistinguishable from "this feed is healthy." See the triage step for exactly
this scenario in [docs/runbook.md](runbook.md).
