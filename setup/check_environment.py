#!/usr/bin/env python3
"""Check EvoClaw environment requirements"""
import sys, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def main():
    from scripts.validate_env import main as validate
    validate()

if __name__ == "__main__":
    main()
