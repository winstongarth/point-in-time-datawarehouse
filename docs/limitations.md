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

This list will grow as later milestones surface further caveats worth naming.
