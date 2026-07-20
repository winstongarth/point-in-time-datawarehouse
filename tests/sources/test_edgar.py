import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pdw.config import get_settings
from pdw.sources.edgar import EdgarSource

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "edgar"


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
def _edgar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDW_EDGAR_CONTACT_EMAIL", "test@example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_fetch_universe_yields_ticker_map_then_known_companyfacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticker_map_body = (FIXTURES / "ticker_map_sample.json").read_bytes()
    companyfacts_body = (FIXTURES / "aapl_companyfacts_sample.json").read_bytes()

    def fake_urlopen(request: urllib.request.Request, timeout: float = 30) -> _FakeResponse:
        if "company_tickers.json" in request.full_url:
            return _FakeResponse(200, ticker_map_body)
        assert "companyfacts" in request.full_url
        return _FakeResponse(200, companyfacts_body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = EdgarSource()
    # AAPL and MSFT are in the trimmed fixture map; NOPE is deliberately not.
    results = list(source.fetch_universe(["AAPL", "MSFT", "NOPE"]))

    assert [r.endpoint for r in results] == ["ticker_map", "companyfacts", "companyfacts"]
    assert results[1].request_params["ticker"] == "AAPL"
    assert results[1].request_params["cik"] == "0000320193"
    assert results[1].content_sha256 == hashlib.sha256(companyfacts_body).hexdigest()


def test_missing_ticker_is_skipped_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticker_map_body = (FIXTURES / "ticker_map_sample.json").read_bytes()

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse(200, ticker_map_body)
    )

    source = EdgarSource()
    with caplog.at_level("WARNING"):
        results = list(source.fetch_universe(["DEFINITELY_NOT_A_REAL_TICKER"]))

    assert len(results) == 1  # only the ticker map fetch
    assert any("no CIK found" in message for message in caplog.messages)


def test_client_error_is_recorded_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    ticker_map_body = (FIXTURES / "ticker_map_sample.json").read_bytes()
    call_count = {"n": 0}

    def fake_urlopen(request: urllib.request.Request, timeout: float = 30) -> _FakeResponse:
        if "company_tickers.json" in request.full_url:
            return _FakeResponse(200, ticker_map_body)
        call_count["n"] += 1
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = EdgarSource()
    results = list(source.fetch_universe(["AAPL"]))

    assert results[1].http_status == 404
    assert call_count["n"] == 1  # a 4xx is recorded, not retried


def test_server_error_is_retried_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    ticker_map_body = (FIXTURES / "ticker_map_sample.json").read_bytes()
    monkeypatch.setattr("pdw.retry.time.sleep", lambda _seconds: None)

    def fake_urlopen(request: urllib.request.Request, timeout: float = 30) -> _FakeResponse:
        if "company_tickers.json" in request.full_url:
            return _FakeResponse(200, ticker_map_body)
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    source = EdgarSource()
    with pytest.raises(urllib.error.HTTPError):
        list(source.fetch_universe(["AAPL"]))
