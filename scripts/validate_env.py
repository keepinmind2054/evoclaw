#!/usr/bin/env python3
"""Validate EvoClaw environment and configuration"""
import sys, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from host.env import read_env_file

def check(label, ok, detail=""):
    icon = "✓" if ok else "✗"
    print(f"  {icon} {label}" + (f": {detail}" if detail else ""))
    return ok

def main():
    print("EvoClaw Environment Check\n")
    all_ok = True

    # Python version
    v = sys.version_info
    ok = v >= (3, 11)
    all_ok &= check("Python 3.11+", ok, f"found {v.major}.{v.minor}")

    # Docker
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        all_ok &= check("Docker running", r.returncode == 0)
    except Exception:
        all_ok &= check("Docker running", False, "not found")

    # .env keys
    env = read_env_file(["GOOGLE_API_KEY", "TELEGRAM_BOT_TOKEN"])
    all_ok &= check("GOOGLE_API_KEY set", bool(env.get("GOOGLE_API_KEY")))
    check("TELEGRAM_BOT_TOKEN set", bool(env.get("TELEGRAM_BOT_TOKEN")), "optional")

    # Container image
    try:
        r = subprocess.run(["docker", "image", "inspect", "evoclaw-agent"],
                          capture_output=True, timeout=5)
        all_ok &= check("evoclaw-agent image built", r.returncode == 0,
                        "run: python scripts/build_container.py" if r.returncode != 0 else "")
    except Exception:
        pass

    print()
    if all_ok:
        print("All checks passed! Run: python run.py")
    else:
        print("Some checks failed. Fix the issues above, then run: python run.py")
        sys.exit(1)

if __name__ == "__main__":
    main()
