#!/usr/bin/env python3
"""LocalRouter — GGUF endpoint manager — local, Vast.ai & managed.
Thin entry point. All logic lives in localrouter/ package."""
from localrouter.menus.main import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
