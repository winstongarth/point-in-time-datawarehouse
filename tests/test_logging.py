import json
import logging

from pdw.logging import JSONFormatter


def _make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="pdw.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_format_emits_valid_json_with_core_fields() -> None:
    formatter = JSONFormatter()

    payload = json.loads(formatter.format(_make_record()))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "pdw.test"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload


def test_format_preserves_extra_fields() -> None:
    formatter = JSONFormatter()

    payload = json.loads(formatter.format(_make_record(run_id=42)))

    assert payload["run_id"] == 42
