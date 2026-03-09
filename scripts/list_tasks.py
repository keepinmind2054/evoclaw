#!/usr/bin/env python3
"""List all scheduled tasks"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from host import db, config

def main():
    db.init_database(config.STORE_DIR / "messages.db")
    tasks = db.get_all_tasks()
    if not tasks:
        print("No scheduled tasks.")
        return
    print(f"{'ID':<38} {'Group':<25} {'Type':<10} {'Value':<20} {'Status'}")
    print("-" * 100)
    for t in tasks:
        print(f"{t['id']:<38} {t['group_folder']:<25} {t['schedule_type']:<10} {t['schedule_value']:<20} {t['status']}")

if __name__ == "__main__":
    main()
