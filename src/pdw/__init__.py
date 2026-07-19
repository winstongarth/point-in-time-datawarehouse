from importlib.metadata import version

__version__ = version("pdw")


def main() -> None:
    from pdw.cli import app

    app()
