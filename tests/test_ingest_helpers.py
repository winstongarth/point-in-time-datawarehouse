from pathlib import Path

import pytest

from pdw.ingest import build_source, load_universe
from pdw.sources.edgar import EdgarSource
from pdw.sources.tiingo import TiingoSource
from pdw.sources.yfinance_source import YFinanceSource


def test_build_source_dispatches_by_name() -> None:
    assert isinstance(build_source("edgar"), EdgarSource)
    assert isinstance(build_source("yfinance"), YFinanceSource)
    assert isinstance(build_source("tiingo"), TiingoSource)


def test_build_source_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        build_source("not-a-real-source")


def test_load_universe_requires_tickers_key(tmp_path: Path) -> None:
    bad_file = tmp_path / "empty.yaml"
    bad_file.write_text("not_tickers: []\n")

    with pytest.raises(ValueError, match="no 'tickers'"):
        load_universe(bad_file)


def test_load_universe_reads_tickers(tmp_path: Path) -> None:
    good_file = tmp_path / "universe.yaml"
    good_file.write_text("tickers:\n  - AAPL\n  - MSFT\n")

    assert load_universe(good_file) == ["AAPL", "MSFT"]
