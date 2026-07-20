from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

from pdw.config import get_settings
from pdw.retry import retry_with_backoff
from pdw.sources import FetchResult, normalize_ticker_for_vendor

logger = logging.getLogger(__name__)

PRICES_URL_TEMPLATE = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"

# CLAUDE.md 2: "History: 10 years or vendor availability, whichever is shorter."
DEFAULT_HISTORY_YEARS = 10

# Per https://api.tiingo.com/documentation/end-of-day (documented shape).
# NOT yet verified against a live response — no Tiingo API token was
# available in the session that wrote this adapter (CLAUDE.md 4.3 was
# amended to add Tiingo after Stooq turned out to be bot-gated). Verify on
# the first real run; if the shape differs, fix this set and fail loudly
# rather than silently accept a different response (CLAUDE.md 11).
_EXPECTED_KEYS = {
    "date",
    "close",
    "high",
    "low",
    "open",
    "volume",
    "adjClose",
    "adjHigh",
    "adjLow",
    "adjOpen",
    "adjVolume",
    "divCash",
    "splitFactor",
}


class TiingoSource:
    """Tiingo EOD prices adapter (CLAUDE.md 4.3). Prices, secondary/reconciliation source.

    Replaces the originally-specified Stooq CSV endpoint, which turned out to
    sit behind a JavaScript proof-of-work bot challenge incompatible with
    automated ingestion — see docs/limitations.md.
    """

    name = "tiingo"

    def __init__(self, *, history_years: int = DEFAULT_HISTORY_YEARS) -> None:
        self._token = get_settings().tiingo_api_token
        self._history_years = history_years

    def fetch_universe(self, tickers: list[str]) -> Iterator[FetchResult]:
        for ticker in tickers:
            yield self._fetch_one(ticker)

    def _fetch_one(self, ticker: str) -> FetchResult:
        symbol = normalize_ticker_for_vendor(ticker)
        end = date.today()
        start = end - timedelta(days=365 * self._history_years)

        # The token is sent as a header, not a query param, so it never ends
        # up persisted in raw.payload.request_params (jsonb) or in fixtures.
        url = (
            f"{PRICES_URL_TEMPLATE.format(symbol=symbol)}"
            f"?startDate={start.isoformat()}&endDate={end.isoformat()}&format=json"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Token {self._token}",
        }
        request_params: dict[str, object] = {
            "ticker": ticker,
            "symbol": symbol,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }

        def _do() -> FetchResult:
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    _validate_shape(body)
                    return FetchResult(
                        endpoint="prices",
                        request_params=request_params,
                        fetched_at=datetime.now(UTC),
                        http_status=response.status,
                        body=body,
                    )
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if 500 <= exc.code < 600:
                    raise
                return FetchResult(
                    endpoint="prices",
                    request_params=request_params,
                    fetched_at=datetime.now(UTC),
                    http_status=exc.code,
                    body=body,
                )

        return retry_with_backoff(_do, retry_on=(urllib.error.URLError,))


def _validate_shape(body: bytes) -> None:
    data = json.loads(body)
    if not data:
        return
    missing = _EXPECTED_KEYS - set(data[0].keys())
    if missing:
        raise ValueError(f"Tiingo response shape changed, missing expected keys: {missing}")
