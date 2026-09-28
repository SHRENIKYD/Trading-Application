#!/usr/bin/env python3
"""Entry point kept for existing commands: `python agent.py ...` runs the trading_agent package CLI."""

import sys

from trading_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
