import pytest

from pdw.retry import retry_with_backoff


def test_returns_result_on_first_success() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    result = retry_with_backoff(fn, max_attempts=3, base_delay_seconds=0)

    assert result == "ok"
    assert len(calls) == 1


def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pdw.retry.time.sleep", lambda _seconds: None)
    attempts = {"count": 0}

    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    result = retry_with_backoff(flaky, max_attempts=5, retry_on=(ConnectionError,))

    assert result == "ok"
    assert attempts["count"] == 3


def test_raises_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pdw.retry.time.sleep", lambda _seconds: None)

    def always_fails() -> str:
        raise ConnectionError("permanent")

    with pytest.raises(ConnectionError):
        retry_with_backoff(always_fails, max_attempts=3, retry_on=(ConnectionError,))


def test_does_not_retry_unlisted_exceptions() -> None:
    def raises_value_error() -> str:
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        retry_with_backoff(raises_value_error, max_attempts=3, retry_on=(ConnectionError,))
