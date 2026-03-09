#!/usr/bin/env python3
"""Build the EvoClaw Docker container"""
import subprocess, sys
from pathlib import Path

def main():
    project_root = Path(__file__).parent.parent
    container_dir = project_root / "container"
    print("Building evoclaw-agent Docker container...")
    result = subprocess.run(
        ["docker", "build", "-t", "evoclaw-agent", "."],
        cwd=container_dir,
    )
    if result.returncode == 0:
        print("✓ Container built: evoclaw-agent")
    else:
        print("✗ Build failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
