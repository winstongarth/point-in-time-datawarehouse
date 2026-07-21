from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class FetchResult:
    """One vendor response, ready to be written verbatim to raw.payload."""

    endpoint: str
    request_params: dict[str, object]
    fetched_at: datetime
    http_status: int
    body: bytes

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class Source(Protocol):
    """Shared interface for every vendor adapter.

    Not a general ingestion framework - three concrete adapters, one shared
    interface. The ingestion orchestrator (pdw.ingest) only ever talks to
    `name` and `fetch_universe`, never to EDGAR/yfinance/Tiingo specifics
    directly.
    """

    name: str

    def fetch_universe(self, tickers: list[str]) -> Iterator[FetchResult]:
        """Yield one FetchResult per vendor request needed to cover `tickers`.

        Not necessarily one-per-ticker: EdgarSource also yields a FetchResult
        for the shared ticker->CIK map it fetches once per run.
        """
        ...


def normalize_ticker_for_vendor(ticker: str) -> str:
    """AAPL stays AAPL; dual-class tickers like "BRK.B" become "BRK-B".

    Confirmed against SEC's live company_tickers.json (2026-07-20): its keys
    use a hyphen, not the "BRK.B" dot notation common on stock-quote pages.
    yfinance and Tiingo both follow the same hyphenated convention.
    """
    return ticker.upper().replace(".", "-")
