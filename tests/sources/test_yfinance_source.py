from pathlib import Path

import pandas as pd
import pytest

from pdw.sources.yfinance_source import YFinanceSource

FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "yfinance" / "aapl_history_sample.csv"
)


class _FakeTicker:
    def __init__(self, symbol: str, df: pd.DataFrame) -> None:
        self.symbol = symbol
        self._df = df

    def history(self, *, period: str, auto_adjust: bool) -> pd.DataFrame:
        assert auto_adjust is False, "auto_adjust=False is required to get Adj Close separately"
        return self._df


def test_fetch_universe_serializes_history_to_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    df = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    monkeypatch.setattr(
        "pdw.sources.yfinance_source.yf.Ticker", lambda symbol: _FakeTicker(symbol, df)
    )

    source = YFinanceSource(period="1mo")
    results = list(source.fetch_universe(["AAPL"]))

    assert len(results) == 1
    result = results[0]
    assert result.http_status == 200
    assert result.endpoint == "history"
    assert result.request_params["symbol"] == "AAPL"
    assert b"Close" in result.body
    assert b"Adj Close" in result.body


def test_dual_class_ticker_is_normalized_for_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_symbols: list[str] = []

    def fake_ticker(symbol: str) -> _FakeTicker:
        seen_symbols.append(symbol)
        return _FakeTicker(symbol, pd.DataFrame())

    monkeypatch.setattr("pdw.sources.yfinance_source.yf.Ticker", fake_ticker)

    source = YFinanceSource(period="1mo")
    list(source.fetch_universe(["BRK.B"]))

    assert seen_symbols == ["BRK-B"]


def test_empty_history_is_recorded_as_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pdw.sources.yfinance_source.yf.Ticker",
        lambda symbol: _FakeTicker(symbol, pd.DataFrame()),
    )

    source = YFinanceSource(period="1mo")
    results = list(source.fetch_universe(["NOSUCHTICKER"]))

    assert results[0].http_status == 404
    assert results[0].body == b""
