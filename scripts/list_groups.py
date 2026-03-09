#!/usr/bin/env python3
"""List all registered groups"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from host import db, config

def main():
    db.init_database(config.STORE_DIR / "messages.db")
    groups = db.get_all_registered_groups()
    if not groups:
        print("No groups registered yet.")
        return
    print(f"{'JID':<35} {'Name':<20} {'Folder':<30} {'Main':<6} {'Trigger'}")
    print("-" * 100)
    for g in groups:
        print(f"{g['jid']:<35} {g['name']:<20} {g['folder']:<30} {bool(g['is_main'])!s:<6} {bool(g['requires_trigger'])}")

if __name__ == "__main__":
    main()
