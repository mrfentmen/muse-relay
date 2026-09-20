#!/usr/bin/env python3
"""MOS command line shim — see mos/cli.py for the real implementation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mos.cli import main

if __name__ == "__main__":
    sys.exit(main())
