#!/usr/bin/env python3
"""Interactive group registration"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def main():
    from scripts.register_group import main as reg
    reg()

if __name__ == "__main__":
    main()
