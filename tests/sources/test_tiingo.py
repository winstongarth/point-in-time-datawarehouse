import json
import urllib.error
import urllib.request

import pytest

from pdw.config import get_settings
from pdw.sources.tiingo import TiingoSource

_SAMPLE_ROW = {
    "date": "2026-07-17T00:00:00.000Z",
    "close": 333.74,
    "high": 334.99,
    "low": 329.0,
    "open": 331.98,
    "volume": 63365300,
    "adjClose": 333.74,
    "adjHigh": 334.99,
    "adjLow": 329.0,
    "adjOpen": 331.98,
    "adjVolume": 63365300,
    "divCash": 0.0,
    "splitFactor": 1.0,
}


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _tiingo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDW_TIINGO_API_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_fetch_universe_records_valid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps([_SAMPLE_ROW]).encode("utf-8")

    seen_headers: dict[str, str] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float = 30) -> _FakeResponse:
        seen_headers.update(request.headers)
        return _FakeResponse(200, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = TiingoSource(history_years=1)
    results = list(source.fetch_universe(["AAPL"]))

    assert len(results) == 1
    assert results[0].http_status == 200
    assert results[0].body == body
    # token travels via header, never in request_params (which lands in raw.payload jsonb)
    assert "test-token" not in json.dumps(results[0].request_params)
    assert seen_headers.get("Authorization") == "Token test-token"


def test_unexpected_shape_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    malformed = json.dumps([{"date": "2026-07-17", "close": 1.0}]).encode("utf-8")

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse(200, malformed)
    )

    source = TiingoSource(history_years=1)
    with pytest.raises(ValueError, match="Tiingo response shape changed"):
        list(source.fetch_universe(["AAPL"]))


def test_dual_class_ticker_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps([_SAMPLE_ROW]).encode("utf-8")
    seen_urls: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 30) -> _FakeResponse:
        seen_urls.append(request.full_url)
        return _FakeResponse(200, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = TiingoSource(history_years=1)
    list(source.fetch_universe(["BRK.B"]))

    assert "BRK-B" in seen_urls[0]
