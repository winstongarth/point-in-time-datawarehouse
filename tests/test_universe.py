from pathlib import Path

from pdw.ingest import load_universe

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "config" / "universe.yaml"


def test_universe_has_exactly_fifty_unique_tickers() -> None:
    tickers = load_universe(UNIVERSE_PATH)

    assert len(tickers) == 50
    assert len(set(tickers)) == 50
