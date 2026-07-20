import time

import pytest

from pdw.ratelimit import TokenBucket


def test_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_first_calls_up_to_capacity_do_not_block() -> None:
    bucket = TokenBucket(rate_per_second=100)

    started = time.monotonic()
    for _ in range(50):
        bucket.acquire()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1


def test_exceeding_capacity_blocks_for_roughly_the_expected_time() -> None:
    bucket = TokenBucket(rate_per_second=50)  # capacity == 50

    for _ in range(50):
        bucket.acquire()  # drain the initial bucket

    started = time.monotonic()
    bucket.acquire()  # must now wait ~1/50s for a token to refill
    elapsed = time.monotonic() - started

    assert elapsed >= 1 / 50 * 0.5
