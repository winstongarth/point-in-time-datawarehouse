import os

import pytest

from pdw.config import Settings


@pytest.fixture(autouse=True)
def _clear_pdw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate tests from any real PDW_* vars or .env the developer has locally."""
    for key in list(os.environ):
        if key.startswith("PDW_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_construct_without_any_env_file() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql://pdw:pdw@localhost:5432/pdw"
    assert settings.edgar_contact_email == "set-me@example.com"
    assert settings.log_level == "INFO"


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDW_LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)

    assert settings.log_level == "DEBUG"
