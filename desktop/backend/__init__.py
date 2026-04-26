from __future__ import annotations

from typing import TYPE_CHECKING

from desktop.backend.server import SidecarServer

if TYPE_CHECKING:
    import argparse


def build_parser() -> "argparse.ArgumentParser":
    from desktop.backend.bootstrap import build_parser as _build_parser

    return _build_parser()


def main(argv: list[str] | None = None) -> int:
    from desktop.backend.bootstrap import main as _main

    return _main(argv)


__all__ = ["SidecarServer", "build_parser", "main"]
