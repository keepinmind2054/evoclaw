#!/bin/bash
# Install EvoClaw as a macOS launchd service.
# Usage: bash scripts/install_launchd.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_PATH="$(command -v python3)"
PLIST_SRC="$PROJECT_ROOT/launchd/com.nanoclaw.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.nanoclaw.plist"

if [ ! -f "$PLIST_SRC" ]; then
    echo "ERROR: $PLIST_SRC not found" >&2
    exit 1
fi

mkdir -p "$PROJECT_ROOT/logs"
mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" \
    -e "s|{{PYTHON_PATH}}|$PYTHON_PATH|g" \
    -e "s|{{HOME}}|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"

echo "Installed plist to: $PLIST_DST"
echo "  ProjectRoot : $PROJECT_ROOT"
echo "  Python      : $PYTHON_PATH"

# Unload existing service if running
launchctl unload "$PLIST_DST" 2>/dev/null || true

launchctl load "$PLIST_DST"
echo "EvoClaw launchd service loaded."
echo ""
echo "Useful commands:"
echo "  launchctl list | grep evoclaw    # check if running"
echo "  tail -f $PROJECT_ROOT/logs/evoclaw.log"
echo "  launchctl unload $PLIST_DST      # stop and disable"
