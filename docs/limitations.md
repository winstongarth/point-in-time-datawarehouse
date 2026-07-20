# Limitations

Stated plainly, up front, rather than left for a reviewer to discover:

- **The universe is survivorship-biased.** The 50 tickers in
  `config/universe.yaml` are a fixed list of currently-listed large-cap US
  names, not reconstructed from a historical index membership. Any backtest
  run against this universe overstates returns relative to what an investor
  living through the period would actually have earned, because it excludes
  every name that was delisted, acquired, or dropped from large-cap status
  along the way.
- **Free vendors offer no delivery SLA.** yfinance is unofficial and can
  change shape or availability without notice; Stooq and SEC EDGAR are best
  effort. `dq` checks and `ops` freshness monitoring exist to surface when a
  feed goes stale or silent, but there is no contractual guarantee behind any
  of the three sources this project depends on.
- **SEC's ticker↔CIK map is current-state-only.** `company_tickers.json`
  reflects today's mapping, not history — a company that changed ticker
  symbols mid-history is only partially reconstructed in
  `core.entity_ticker`, based on what can be inferred from filing history
  rather than an authoritative historical mapping table.

- **Stooq was dropped as the secondary price vendor (M2).** The spec
  originally named Stooq's CSV endpoint for cross-vendor price reconciliation.
  During the M2 build, that endpoint was found to sit behind a site-wide
  JavaScript proof-of-work bot challenge (confirmed with multiple
  User-Agents — not a header issue), which a plain HTTP client cannot pass
  without solving the challenge programmatically. This project does not
  build anti-bot-evasion tooling, so Stooq was replaced with
  [Tiingo](https://www.tiingo.com/)'s free-tier EOD prices API. See
  CLAUDE.md 4.3.

- **Fundamentals coverage lands at 87.6%, not the ≥90% target (M3).** Verified
  live against the full 50-ticker universe. Two specific, diagnosed causes
  account for most of the shortfall, neither fixable by adding more tags to
  `config/metric_map.yaml` without guessing at unverified proxies:
  - **Bank holding companies have no unified "Revenue" GAAP concept.**
    WFC, JPM, and BAC report net interest income and fee income as separate
    line items; there is no single XBRL tag equivalent to an industrial
    company's "Revenue". Their real reported `Revenues` tag (where present)
    covers only part of their filing history.
  - **A few large-caps have no XBRL fact for diluted shares outstanding
    for most of their history, under any tag, in any taxonomy namespace.**
    Confirmed for BRK.B (dual-class share structure) and GOOGL (its
    `WeightedAverageNumberOfDilutedSharesOutstanding` fact only starts in
    fiscal 2024 in the data fetched; no other `dei`/`us-gaap`/`ecd`/`ffd`
    fact in its companyfacts response covers the earlier years).
  - The remaining shortfall is scattered 1-4-entity-quarter edge cases
    across many companies (typically sparse tagging in a company's earliest
    XBRL-era history), not a systemic issue.

This list will grow as later milestones surface further caveats worth naming.
