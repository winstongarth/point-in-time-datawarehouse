from importlib.metadata import version as package_version

from typer.testing import CliRunner

from pdw.cli import app

runner = CliRunner()


def test_help_exits_cleanly() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "pdw" in result.output.lower()


def test_version_matches_installed_package() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == package_version("pdw")
