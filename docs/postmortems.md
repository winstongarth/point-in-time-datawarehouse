# Post-mortems

Three failures that shaped the design of this warehouse. Each was found by running the
real pipeline against the live 50-ticker universe rather than by a unit test, and each
left a specific, still-load-bearing decision behind — the sentinel `knowledge_from`, the
shape of the bitemporal key, and `no_data` as a status distinct from `ok`. Related
caveats that were never fixed, only documented, are in [limitations.md](limitations.md).

---

## 1. `entity_ticker.knowledge_from` silently zeroed every historical point-in-time query

**Impact:** every `PointInTimeReader` query with an `as_of` earlier than this warehouse's
own first ingestion run returned zero rows, for every entity, regardless of how old the
underlying fact actually was. Undetected, this invalidates the entire premise of the
system — a reader that cannot resolve a ticker at a historical date has nothing to read.

**Detection:** not a test failure. It surfaced on a live query against a real restatement
(GE's FY2011 revenue): `as_of=2012-03-01` returned an empty result where a real, filed
value should have been.

**Root cause:** `core.entity_ticker.knowledge_from` was set, in
`_upsert_entities_and_tickers`, to the time the ticker↔CIK map was *fetched* — not to any
date meaningfully tied to the ticker itself. `PointInTimeReader` applies its `as_of`
predicate uniformly, including to the join that resolves ticker → entity, so a mapping
that opened at ingestion time could never resolve for an `as_of` before that moment, no
matter how far back the underlying fundamental or price fact went.

Every unit test passed, because each test that needed a ticker mapping constructed one
directly with a realistic `knowledge_from` of its own choosing. The bug lived entirely in
the gap between what a hand-built fixture naturally does and what the real parse pipeline
did.

**Fix:** a brand-new entity's *first-ever* ticker mapping now opens at a fixed sentinel
(`2000-01-01T00:00Z`), not the ingestion timestamp. SEC's ticker map is current-state-only
regardless — there is no true historical assignment date to recover — so treating the
mapping as "always true absent better information" is the more useful reading of that same
limitation. A genuine *reassignment*, once one is ever detected, still opens at real
detection time. See `_upsert_entities_and_tickers` in `src/pdw/parse.py`, and its
regression test in `tests/test_parse_db.py`.

**Lesson carried forward:** a point-in-time guarantee is only as good as its weakest
`as_of`-filtered join. Any new dimension joined into a read path needs its own
defensible `knowledge_from`, and a live query against real historical data — not a
synthetic fixture — is what proves it.

---

## 2. A real 10-Q collided two simultaneously-true facts under one bitemporal key

**Impact:** the first live run of `pdw load-fundamentals` against the full universe raised
a `psycopg.errors.CheckViolation` on invariant 4 (`knowledge_from < knowledge_to`). The
loader could not complete for the affected entity, blocking promotion from `stg` to `core`
for that company entirely.

**Detection:** every synthetic bitemporal fixture — simple amendment, double amendment,
out-of-order arrival, no-change re-fetch — passed. The failure appeared only on the first
real load against Verizon's actual EDGAR filing history.

**Root cause:** invariant 1's key was originally `(entity_id, metric_code, period_end,
source)`. A real Verizon 10-Q (accession `0000732712-19-000052`) reports revenue for *both*
the 3-month quarter and the 6-month year-to-date window ending on the same `period_end`,
under the same accession — two genuinely different, simultaneously-true facts, neither
restating the other. Keying on `period_end` alone collided them: the loader tried to open
two rows with the same key and overlapping knowledge windows, which is exactly what
invariant 1's `EXCLUDE USING gist` constraint exists to catch.

Hand-built fixtures never exercised this, because anyone writing a synthetic amendment
fixture already holds a "one company, one metric, one period" mental model. The shape that
breaks the key is a specific quirk of how 10-Qs actually disclose cumulative figures.

**Fix:** widened the key to `(entity_id, metric_code, period_start, period_end, source)`.
`period_start` is `NULL` for instant concepts (`Assets`, `StockholdersEquity`), so the
`EXCLUDE` constraint coalesces it to a fixed sentinel date rather than comparing raw
`NULL`s — Postgres treats those as never equal to each other, which would silently defeat
the constraint for exactly the rows most likely to collide. See
`migrations/sql/0004_core_facts.sql`.

**Lesson carried forward:** this EDGAR shape — a duration fact sharing its `fiscal_period`
label with a differently-scoped fact for the same company — is a recurring *class* of
quirk, not a one-off. It resurfaced in the `revenue_sanity` check (a YTD-cumulative figure
compared against a trailing quarterly median) and again in `_ttm_net_income` (which needs
the same single-quarter duration filter to avoid double-counting). Both carry a comment
pointing back here.

---

## 3. Full-universe price ingestion silently never completed

**Impact:** `core.price_fact` held real data for only 2 of 50 tickers (yfinance) and zero
rows at all for Tiingo. Every reconciliation and staleness result computed against that
table was meaningless while it went unnoticed.

**Detection:** `pdw dq run`'s `price_close_cross_vendor` and `price_staleness` checks did
not error — they vacuously passed with `"no comparable rows yet"` and `"no price facts
loaded yet"` respectively. That is correct behavior for a genuinely empty table (a check
that only records failures cannot support a coverage metric), but nothing in the output
distinguished "this feed is healthy and current" from "this feed has never actually been
run." Only a direct `SELECT count(distinct entity_id) FROM core.price_fact` — run because
the *volume* of check results looked implausibly small for 50 tickers × 10 years —
surfaced the gap.

**Root cause:** the two price adapters were built and smoke-tested against a couple of
tickers each and never run to completion, while every subsequent live verification
happened to reach for EDGAR-backed features (the coverage report, the Verizon loader bug,
the GE restatement demo) without re-checking the price feeds. Separately,
`PDW_TIINGO_API_TOKEN` was still a placeholder, so Tiingo had never been called with real
credentials at all — even a full-universe attempt would have failed with HTTP 403 until
that was set.

**Fix:** ran `pdw ingest` and `pdw load-prices` for both yfinance and Tiingo across the
full 50-ticker universe (124,237 and 124,948 rows respectively), with a real Tiingo token
configured.

**Lesson carried forward:** this is a monitoring-coverage gap as much as a data gap — a
vacuous pass and a genuine "everything is fine" pass must not look alike. `pdw ops status`
reports `no_data` as a status distinct from `ok` specifically so that "this feed has zero
fetches ever" can never again be confused with "this feed is healthy," and
[runbook.md](runbook.md) carries a triage step for exactly this scenario.
