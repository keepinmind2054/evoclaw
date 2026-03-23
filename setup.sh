#!/bin/bash
set -euo pipefail

# setup.sh — Bootstrap script for EvoClaw
# Checks system prerequisites (Python, Docker) and installs Python dependencies.
# Hands off to setup/setup.py for interactive configuration.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$PROJECT_ROOT/logs/setup.log"

mkdir -p "$PROJECT_ROOT/logs"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [bootstrap] $*" >> "$LOG_FILE"; }
info() { echo "  $*"; log "$*"; }
err()  { echo "ERROR: $*" >&2; log "ERROR: $*"; }

# --- Platform detection ---

detect_platform() {
  local uname_s
  uname_s=$(uname -s)
  case "$uname_s" in
    Darwin*) PLATFORM="macos" ;;
    Linux*)  PLATFORM="linux" ;;
    *)       PLATFORM="unknown" ;;
  esac

  IS_WSL="false"
  if [ "$PLATFORM" = "linux" ] && [ -f /proc/version ]; then
    if grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null; then
      IS_WSL="true"
    fi
  fi

  IS_ROOT="false"
  if [ "$(id -u)" -eq 0 ]; then
    IS_ROOT="true"
  fi

  log "Platform: $PLATFORM, WSL: $IS_WSL, Root: $IS_ROOT"
}

# --- Python check ---

check_python() {
  PYTHON_OK="false"
  PYTHON_VERSION="not_found"
  PYTHON_PATH_FOUND=""

  for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
      PYTHON_VERSION=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
      PYTHON_PATH_FOUND=$(command -v "$cmd")
      local major minor
      major=$(echo "$PYTHON_VERSION" | cut -d. -f1)
      minor=$(echo "$PYTHON_VERSION" | cut -d. -f2)
      if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ] 2>/dev/null; then
        PYTHON_OK="true"
        PYTHON_CMD="$cmd"
        break
      fi
    fi
  done

  if [ "$PYTHON_OK" = "false" ]; then
    if [ -n "$PYTHON_VERSION" ] && [ "$PYTHON_VERSION" != "not_found" ]; then
      err "Python 3.11+ required but found $PYTHON_VERSION. Install Python 3.11 or later."
    else
      err "Python 3.11+ not found. Install from https://www.python.org/downloads/"
    fi
  fi

  log "Python: $PYTHON_VERSION at ${PYTHON_PATH_FOUND:-not_found} (ok=$PYTHON_OK)"
}

# --- Docker check ---

check_docker() {
  DOCKER_OK="false"

  if ! command -v docker >/dev/null 2>&1; then
    err "Docker not found."
    if [ "$PLATFORM" = "macos" ]; then
      echo "  Install Docker Desktop from: https://www.docker.com/products/docker-desktop"
    elif [ "$PLATFORM" = "linux" ]; then
      echo "  Ubuntu/Debian: sudo apt-get install docker.io && sudo systemctl start docker"
      echo "  RHEL/Fedora:   sudo dnf install docker && sudo systemctl start docker"
      echo "  Then add yourself to the docker group: sudo usermod -aG docker \$USER"
    fi
    log "Docker not found"
    return
  fi

  if docker info >/dev/null 2>&1; then
    DOCKER_OK="true"
    log "Docker is running"
  else
    err "Docker is installed but not running."
    if [ "$PLATFORM" = "macos" ]; then
      echo "  Start Docker Desktop."
    else
      echo "  Run: sudo systemctl start docker"
    fi
    log "Docker not running (exit code non-zero)"
  fi
}

# --- Install Python dependencies ---

install_python_deps() {
  DEPS_OK="false"

  if [ "$PYTHON_OK" = "false" ]; then
    log "Skipping pip install — Python not available"
    return
  fi

  cd "$PROJECT_ROOT"
  log "Running pip install -r host/requirements.txt"
  if "$PYTHON_CMD" -m pip install -r host/requirements.txt >> "$LOG_FILE" 2>&1; then
    DEPS_OK="true"
    log "pip install succeeded"
  else
    err "pip install failed — see $LOG_FILE for details"
    log "pip install failed"
  fi
}

# --- Build Docker image (optional, runs only when Docker is available) ---

build_docker_image() {
  IMAGE_OK="false"

  if [ "$DOCKER_OK" = "false" ]; then
    log "Skipping Docker image build — Docker not available"
    return
  fi

  local build_script="$PROJECT_ROOT/container/build.sh"
  if [ ! -f "$build_script" ]; then
    err "container/build.sh not found — cannot build Docker image"
    log "build.sh missing"
    return
  fi

  info "Building EvoClaw agent container image (evoclaw-agent:latest)..."
  log "Running container/build.sh"
  if bash "$build_script" >> "$LOG_FILE" 2>&1; then
    # Verify the image actually exists after the build
    if docker image inspect evoclaw-agent:latest >/dev/null 2>&1; then
      IMAGE_OK="true"
      log "Docker image build succeeded: evoclaw-agent:latest"
    else
      err "container/build.sh exited 0 but evoclaw-agent:latest not found in local registry"
      log "Docker image missing after build"
    fi
  else
    err "Docker image build failed — see $LOG_FILE for details"
    log "container/build.sh failed"
  fi
}

# --- Build tools check (optional, for native extensions) ---

check_build_tools() {
  HAS_BUILD_TOOLS="false"

  if [ "$PLATFORM" = "macos" ]; then
    if xcode-select -p >/dev/null 2>&1; then
      HAS_BUILD_TOOLS="true"
    fi
  elif [ "$PLATFORM" = "linux" ]; then
    if command -v gcc >/dev/null 2>&1 && command -v make >/dev/null 2>&1; then
      HAS_BUILD_TOOLS="true"
    fi
  fi

  log "Build tools: $HAS_BUILD_TOOLS"
}

# --- Main ---

log "=== Bootstrap started ==="

detect_platform
check_python
check_docker
install_python_deps
build_docker_image
check_build_tools

# Determine overall status
STATUS="success"
if [ "$PYTHON_OK" = "false" ]; then
  STATUS="python_missing"
elif [ "$DOCKER_OK" = "false" ]; then
  STATUS="docker_missing"
elif [ "$DEPS_OK" = "false" ]; then
  STATUS="deps_failed"
elif [ "$IMAGE_OK" = "false" ]; then
  STATUS="image_build_failed"
fi

cat <<EOF

=== EVOCLAW SETUP: BOOTSTRAP ===
PLATFORM:       $PLATFORM
IS_WSL:         $IS_WSL
IS_ROOT:        $IS_ROOT
PYTHON_VERSION: $PYTHON_VERSION
PYTHON_OK:      $PYTHON_OK
PYTHON_PATH:    ${PYTHON_PATH_FOUND:-not_found}
DOCKER_OK:      $DOCKER_OK
DEPS_OK:        $DEPS_OK
IMAGE_OK:       ${IMAGE_OK:-false}
HAS_BUILD_TOOLS:$HAS_BUILD_TOOLS
STATUS:         $STATUS
LOG:            logs/setup.log
=== END ===

EOF

log "=== Bootstrap completed: $STATUS ==="

if [ "$PYTHON_OK" = "false" ]; then
  exit 2
fi
if [ "$DOCKER_OK" = "false" ]; then
  exit 2
fi
if [ "$DEPS_OK" = "false" ]; then
  exit 1
fi
if [ "${IMAGE_OK:-false}" = "false" ] && [ "$DOCKER_OK" = "true" ]; then
  exit 1
fi

# Continue to interactive Python setup
info "Running interactive setup..."
"$PYTHON_CMD" "$PROJECT_ROOT/setup/setup.py"
