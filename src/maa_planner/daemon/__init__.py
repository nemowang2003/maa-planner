"""Daemon entry point. All game automation and maintenance belong to this package."""


def main(argv: list[str] | None = None) -> None:
    from .cli import main as cli_main

    cli_main(argv)
