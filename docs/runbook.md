# Runbook

Triage procedure for each `BREAK`-severity check, the exception lifecycle's escalation
path, and the common failure modes actually seen while building this project
(cross-referenced to [docs/postmortems.md](postmortems.md)).

## Escalation path

Every `dq` check writes a `dq.check_result` row on every `pdw dq run`, pass or fail.
A failure opens a `dq.exception` row if none is already open or in
triage for that `(check_name, dimension_key)`; a recurring failure is a no-op (it's
still open); the check passing again auto-closes it.

1. `pdw dq status` — list every open/in-triage exception, most severe first.
2. `pdw dq triage <exception_id> --note "..."` — acknowledge you're looking at it. Use
   the note to record what you've found so far, even mid-investigation.
3. Resolve one of two ways:
   - The check passes again on a later `pdw dq run` → auto-closed, no action needed.
   - You've determined it's not a real defect (a known limitation, a one-off data
     quirk) → `pdw dq resolve <exception_id> --note "..."` with your reasoning. The
     note is permanent audit trail — write it as if someone else has to trust your
     judgment without re-doing the investigation.

`INFO` and `WARN` exceptions can sit open indefinitely without blocking anything; treat
them as a backlog to periodically sweep. `BREAK` means "not fit for use" — treat every
open `BREAK` exception as blocking promotion of the affected data until triaged.

## `balance_sheet_identity` (BREAK)

**What it means:** `total_assets` doesn't match `LiabilitiesAndStockholdersEquity` within
1% for a specific `(entity, period_end)`. This is a fundamental accounting identity — a
real company's own balance sheet always balances, by construction, so a mismatch means
something is wrong with *our* data, not the company's.

**Triage steps:**
1. Pull the `observed` JSON off the failing `dq.check_result` row — it has both raw values.
2. Check the ratio. A clean multiple (10x, 100x) points at a decimal-shift or unit bug
   somewhere in parsing (`_extract_instant_value` in `src/pdw/dq_checks.py`) — compare
   against the raw EDGAR payload for that CIK/period directly (`raw.payload`, source=edgar,
   endpoint=companyfacts) to see which side is wrong.
3. A small (a few percent) mismatch is more likely a real accounting nuance (noncontrolling
   interests, restated comparatives inside a later filing) than a bug — confirm by reading
   the actual 10-K/10-Q footnotes via the accession number before assuming defect.
4. If it's a genuine parsing/mapping bug: fix it, add a regression test, re-run `pdw dq run`,
   confirm the exception auto-closes.
5. If it's a real, benign anomaly (e.g. the one-off `total_assets = 0` row found at M6,
   confirmed to be a single genuinely bad EDGAR datapoint out of 3,239): `pdw dq resolve`
   with the specific finding recorded.

## `price_staleness` (BREAK)

**What it means:** a ticker's most recent yfinance price is more than 5 business days old.
On the NYSE calendar (weekday-only approximation, no holiday calendar — see
[docs/limitations.md](limitations.md)), that's a genuinely broken feed for that name, not
noise.

**Triage steps:**
1. `pdw ops status` first — if yfinance itself shows `breach` or `no_data`, this is not a
   per-ticker problem, it's the whole feed; see the M6 postmortem for exactly this failure
   mode (a feed that silently never ran at all, vs. one that stopped).
2. If only specific tickers are stale while the feed overall is healthy: check whether the
   ticker was delisted, acquired, or renamed — SEC's ticker map is current-state-only
   (see [docs/limitations.md](limitations.md)), so a corporate action can silently orphan
   a ticker.
3. Re-run `pdw ingest --source yfinance` for the affected ticker(s) and `pdw load-prices
   --source yfinance`; confirm the exception auto-closes on the next `pdw dq run`.

## `price_close_cross_vendor` (WARN → BREAK after 3 consecutive days)

**What it means:** yfinance and Tiingo's `adj_close` disagree by more than 1.5% relative,
for 3+ consecutive trading days for the same ticker. The tolerance was set from live data
(max observed legitimate disagreement: 1.377%) — anything that reaches `BREAK` here is
outside the band two independently-computed adjustment methodologies should ever produce,
which means one vendor's feed is actually wrong for that stretch, not just
differently-rounded.

**Triage steps:**
1. Pull `observed.relative_diff` across the consecutive failing days — a value far outside
   the historical band (double digits, or a clean multiple) points at a vendor data problem,
   not normal methodology drift.
2. Check whether the ticker split or paid a special dividend recently — a *newly*
   introduced, large, sudden divergence right after a corporate action is the signature of
   one vendor's adjustment factor being wrong or late (this project already found live that
   yfinance's raw `close`, not `adj_close`, is *always* split-adjusted regardless of fetch
   flags — so double-check which field actually diverged).
3. If one vendor is confirmed wrong: that vendor's data for the affected dates should not be
   trusted for anything relying on it (the M7 backtest uses Tiingo for market cap and
   yfinance for returns specifically to have an independent check on each) — re-fetch and
   re-load once the vendor's feed is confirmed corrected, or fall back to the other vendor
   for that stretch if it doesn't correct.
4. `pdw dq resolve` once confirmed either fixed or explained.

## Common failure modes seen in this build

- **A check passes vacuously and looks identical to a genuinely healthy check.** `"no
  comparable rows yet"` is the correct response to zero data, but it reads exactly like
  success in a quick skim of `pdw dq run`'s summary line. Always cross-check `pdw ops
  status` for `no_data` before trusting a clean `pdw dq run` — see postmortem 3.
- **A hardcoded field/source assumption silently breaks once a second vendor's data
  exists.** `PointInTimeReader.prices()` had no `source` filter until M7 needed it;
  `check_cross_vendor_price` ignored its own config's `field` key until M6's live run
  exposed it. When adding a new consumer of multi-source data, explicitly decide and test
  which source/field it needs — don't assume "there's only one row" holds.
- **A restated/duplicated EDGAR shape (YTD-cumulative or multi-period facts sharing a
  `fiscal_period` label) recurs across unrelated code paths.** Seen at M4 (the bitemporal
  key), M6 (`revenue_sanity`'s trailing median), and M7 (`_ttm_net_income`) — see
  postmortem 2. Any new code that aggregates `core.fundamental_fact` by period should filter
  to genuine single-quarter durations (`period_end - period_start` in [80, 100] days) unless
  it deliberately wants the cumulative/annual figures too.
