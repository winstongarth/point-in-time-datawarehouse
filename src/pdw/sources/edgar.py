from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime

from pdw.config import get_settings
from pdw.ratelimit import TokenBucket
from pdw.retry import retry_with_backoff
from pdw.sources import FetchResult, normalize_ticker_for_vendor

logger = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Verified against a live fetch on 2026-07-20: each entry in company_tickers.json
# has exactly these three keys. Fail loudly (CLAUDE.md 11) rather than silently
# skip a shape that no longer matches.
_TICKER_MAP_ENTRY_KEYS = {"cik_str", "ticker", "title"}


class EdgarSource:
    """SEC EDGAR company facts adapter (CLAUDE.md 4.1). Fundamentals, primary source."""

    name = "edgar"

    def __init__(self) -> None:
        settings = get_settings()
        self._headers = {
            "User-Agent": f"pdw point-in-time data warehouse ({settings.edgar_contact_email})"
        }
        self._bucket = TokenBucket(settings.edgar_requests_per_second)

    def fetch_universe(self, tickers: list[str]) -> Iterator[FetchResult]:
        ticker_map_result = self._get(TICKER_MAP_URL, endpoint="ticker_map", request_params={})
        yield ticker_map_result

        if ticker_map_result.http_status != 200:
            logger.error(
                "ticker map fetch failed, cannot resolve any CIKs this run",
                extra={"http_status": ticker_map_result.http_status},
            )
            return

        cik_by_ticker = _parse_ticker_map(ticker_map_result.body)

        for ticker in tickers:
            normalized = normalize_ticker_for_vendor(ticker)
            cik = cik_by_ticker.get(normalized)
            if cik is None:
                logger.warning(
                    "no CIK found for ticker in EDGAR ticker map",
                    extra={"ticker": ticker, "normalized": normalized},
                )
                continue

            url = COMPANYFACTS_URL_TEMPLATE.format(cik=cik)
            yield self._get(
                url,
                endpoint="companyfacts",
                request_params={"ticker": ticker, "cik": cik},
            )

    def _get(
        self, url: str, *, endpoint: str, request_params: dict[str, object]
    ) -> FetchResult:
        self._bucket.acquire()

        def _do() -> FetchResult:
            request = urllib.request.Request(url, headers=self._headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return FetchResult(
                        endpoint=endpoint,
                        request_params=request_params,
                        fetched_at=datetime.now(UTC),
                        http_status=response.status,
                        body=response.read(),
                    )
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if 500 <= exc.code < 600:
                    # Transient server-side failure: let the retry loop handle it.
                    raise
                # Client error (e.g. 404 for a bad CIK): a real, informative
                # answer in its own right, not something to retry away.
                return FetchResult(
                    endpoint=endpoint,
                    request_params=request_params,
                    fetched_at=datetime.now(UTC),
                    http_status=exc.code,
                    body=body,
                )

        return retry_with_backoff(_do, retry_on=(urllib.error.URLError,))


def _parse_ticker_map(body: bytes) -> dict[str, str]:
    data = json.loads(body)
    result: dict[str, str] = {}
    for entry in data.values():
        if not _TICKER_MAP_ENTRY_KEYS.issubset(entry.keys()):
            raise ValueError(
                f"SEC ticker map entry shape changed, expected keys "
                f"{_TICKER_MAP_ENTRY_KEYS}, got {set(entry.keys())}"
            )
        ticker = str(entry["ticker"]).upper()
        cik = str(entry["cik_str"]).zfill(10)
        result[ticker] = cik
    return result
