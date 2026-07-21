from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import yfinance as yf

from pdw.retry import retry_with_backoff
from pdw.sources import FetchResult, normalize_ticker_for_vendor

# History: 10 years or vendor availability, whichever is shorter.
DEFAULT_PERIOD = "10y"


class YFinanceSource:
    """yfinance daily price adapter. Prices, primary source.

    yfinance is not a raw HTTP client — it returns a parsed DataFrame, so
    there is no vendor wire format to store verbatim. The DataFrame's CSV
    serialization is treated as this adapter's canonical "raw" byte
    representation for hashing and storage purposes.

    `auto_adjust=False` is deliberate: the divergence between `Close` and
    `Adj Close` across fetch dates is the mechanism this project uses to
    demonstrate retroactive price adjustment. The library's
    own default, `auto_adjust=True`, would collapse that distinction by
    baking the adjustment into `Close` and dropping `Adj Close` entirely.
    """

    name = "yfinance"

    def __init__(self, *, period: str = DEFAULT_PERIOD) -> None:
        self._period = period

    def fetch_universe(self, tickers: list[str]) -> Iterator[FetchResult]:
        for ticker in tickers:
            yield self._fetch_one(ticker)

    def _fetch_one(self, ticker: str) -> FetchResult:
        symbol = normalize_ticker_for_vendor(ticker)
        request_params: dict[str, object] = {
            "ticker": ticker,
            "symbol": symbol,
            "period": self._period,
        }

        def _do() -> FetchResult:
            history = yf.Ticker(symbol).history(period=self._period, auto_adjust=False)
            if history.empty:
                return FetchResult(
                    endpoint="history",
                    request_params=request_params,
                    fetched_at=datetime.now(UTC),
                    http_status=404,
                    body=b"",
                )
            return FetchResult(
                endpoint="history",
                request_params=request_params,
                fetched_at=datetime.now(UTC),
                http_status=200,
                body=history.to_csv().encode("utf-8"),
            )

        # yfinance is unofficial and surfaces failures as a mix
        # of requests/JSON/library exceptions rather than one clean type, so
        # the retry is intentionally broad.
        return retry_with_backoff(_do, retry_on=(Exception,))
