"""
Command-line interface for the Evoclaw skills engine.

Usage:
    python -m skills_engine init
    python -m skills_engine apply path/to/skill-dir/
    python -m skills_engine uninstall skill-name
    python -m skills_engine list
    python -m skills_engine rebase
    python -m skills_engine migrate
"""

import argparse
import sys
from pathlib import Path


def cmd_init(args):
    from . import init_skills_system
    init_skills_system()


def cmd_apply(args):
    from . import apply_skill
    result = apply_skill(args.skill_dir)
    if result.success:
        print(f"✓ Applied {result.skill} v{result.version}")
        if result.untracked_changes:
            print(f"  Note: drift detected in: {', '.join(result.untracked_changes)}")
    else:
        print(f"✗ Failed to apply: {result.error}", file=sys.stderr)
        if result.merge_conflicts:
            print(f"  Conflicts: {', '.join(result.merge_conflicts)}", file=sys.stderr)
        sys.exit(1)


def cmd_uninstall(args):
    from . import uninstall_skill
    result = uninstall_skill(args.skill_name)
    if result.success:
        print(f"✓ Uninstalled {result.skill}")
        if result.custom_patch_warning:
            print(f"  Warning: {result.custom_patch_warning}")
    else:
        print(f"✗ Failed to uninstall: {result.error}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    from . import get_applied_skills
    try:
        skills = get_applied_skills()
        if not skills:
            print("No skills applied.")
        else:
            print(f"Applied skills ({len(skills)}):")
            for s in skills:
                print(f"  • {s.name} v{s.version} — applied {s.applied_at[:10]}")
    except FileNotFoundError:
        print("Skills system not initialized. Run: python -m skills_engine init")
        sys.exit(1)


def cmd_rebase(args):
    from . import rebase
    result = rebase()
    if result.success:
        print(f"✓ Rebase complete. {result.files_in_patch} files in custom patch.")
        if result.patch_file:
            print(f"  Patch saved to: {result.patch_file}")
    else:
        print(f"✗ Rebase failed: {result.error}", file=sys.stderr)
        sys.exit(1)


def cmd_migrate(args):
    from . import migrate_existing
    migrate_existing()


def main():
    parser = argparse.ArgumentParser(
        prog="python -m skills_engine",
        description="Evoclaw Skills Engine — manage skills (plugins) for your project",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Initialize the skills system")
    p_init.set_defaults(func=cmd_init)

    # apply
    p_apply = subparsers.add_parser("apply", help="Apply a skill")
    p_apply.add_argument("skill_dir", help="Path to the skill directory")
    p_apply.set_defaults(func=cmd_apply)

    # uninstall
    p_uninstall = subparsers.add_parser("uninstall", help="Uninstall a skill")
    p_uninstall.add_argument("skill_name", help="Name of the skill to uninstall")
    p_uninstall.set_defaults(func=cmd_uninstall)

    # list
    p_list = subparsers.add_parser("list", help="List applied skills")
    p_list.set_defaults(func=cmd_list)

    # rebase
    p_rebase = subparsers.add_parser("rebase", help="Rebase: extract custom patch and replay skills")
    p_rebase.set_defaults(func=cmd_rebase)

    # migrate
    p_migrate = subparsers.add_parser("migrate", help="Migrate existing installation to skills system")
    p_migrate.set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
