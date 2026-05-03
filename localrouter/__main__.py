"""Entry-point for ``python -m localrouter``."""
from __future__ import annotations

import sys


def main() -> None:
    """Launch the localrouter TUI."""
    from localrouter.menus.main import main as _main
    _main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
