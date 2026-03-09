#!/usr/bin/env python3
"""Run database migrations for EvoClaw"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from host import db, config

def main():
    print("Running EvoClaw database migrations...")
    db.init_database(config.STORE_DIR / "messages.db")
    print("✓ Database schema up to date")

if __name__ == "__main__":
    main()
