#!/usr/bin/env python3
"""Register a new group in the EvoClaw database"""
import sys, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from host import db, config

def main():
    parser = argparse.ArgumentParser(description="Register a group in EvoClaw")
    parser.add_argument("--jid", required=True, help="Chat JID (e.g. tg:-1001234567890)")
    parser.add_argument("--name", required=True, help="Group name")
    parser.add_argument("--folder", required=True, help="Folder name (e.g. telegram_my-group)")
    parser.add_argument("--main", action="store_true", help="Mark as main group (no trigger required)")
    parser.add_argument("--no-trigger", action="store_true", help="Don't require trigger word")
    args = parser.parse_args()

    db.init_database(config.STORE_DIR / "messages.db")
    db.set_registered_group(
        jid=args.jid,
        name=args.name,
        folder=args.folder,
        trigger_pattern=None,
        container_config=None,
        requires_trigger=not (args.main or args.no_trigger),
        is_main=args.main,
    )
    print(f"✓ Registered group: {args.name} ({args.jid})")
    print(f"  Folder: {args.folder}")
    print(f"  Main: {args.main}")
    print(f"  Requires trigger: {not (args.main or args.no_trigger)}")

if __name__ == "__main__":
    main()
