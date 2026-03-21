#!/usr/bin/env python3
"""Build the EvoClaw Docker container.

p13d fixes:
- Tag the image with an explicit version tag (``evoclaw-agent:VERSION``) in
  addition to ``latest`` so operators can pin ``CONTAINER_IMAGE`` to a
  specific build and roll back without rebuilding.
- Pass ``--no-cache`` when the ``--no-cache`` CLI flag is supplied.
"""
import argparse
import subprocess
import sys
from pathlib import Path

# Increment this version string when the agent image changes.
IMAGE_VERSION = "1.10.21"
IMAGE_NAME = "evoclaw-agent"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the EvoClaw agent container image.")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Pass --no-cache to docker build (forces a full rebuild).",
    )
    parser.add_argument(
        "--version",
        default=IMAGE_VERSION,
        help=f"Image version tag (default: {IMAGE_VERSION}).",
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    container_dir = project_root / "container"

    versioned_tag = f"{IMAGE_NAME}:{args.version}"
    latest_tag = f"{IMAGE_NAME}:latest"

    print(f"Building {versioned_tag} (also tagged as {latest_tag})...")

    cmd = ["docker", "build"]
    if args.no_cache:
        cmd.append("--no-cache")
    cmd += [
        "-t", versioned_tag,
        "-t", latest_tag,
        ".",
    ]

    result = subprocess.run(cmd, cwd=container_dir)
    if result.returncode == 0:
        print(f"Container built successfully.")
        print(f"  Versioned tag : {versioned_tag}")
        print(f"  Latest tag    : {latest_tag}")
        print(
            f"\nTip: set CONTAINER_IMAGE={versioned_tag} in .env to pin to this build."
        )
    else:
        print("Build failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
