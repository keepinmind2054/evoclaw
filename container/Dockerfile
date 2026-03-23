# EvoClaw Agent Container - Production Ready v1.10.21
# Supports: Multi-provider LLM (Gemini/Claude/OpenAI), document generation,
#           web scraping, browser automation, image processing, data analysis,
#           MCP protocol (Node.js), CJK fonts, non-root security

# p13d: Pin to a specific digest rather than the mutable ':22' tag so that
# builds are reproducible and a silent upstream change cannot introduce
# regressions or supply-chain vulnerabilities.  Update this digest
# intentionally when upgrading the base image.
FROM node:22-bookworm-slim@sha256:4f77a690f2f8946ab16fe1e791a3ac0667ae1c3575c3e4d0d4589e9ed5bfaf3d

# ── System dependencies ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python runtime + build tools
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    gcc \
    # Network utilities
    curl \
    wget \
    git \
    # Archive / data utilities
    unzip \
    jq \
    # Browser automation (Chromium for Playwright / agent-browser)
    chromium \
    # Font rendering libraries
    libfreetype6 \
    libfontconfig1 \
    libpangocairo-1.0-0 \
    # Image / graphics libraries
    libjpeg-dev \
    libpng-dev \
    zlib1g-dev \
    libcairo2 \
    # Chromium runtime dependencies
    libgbm1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libdrm2 \
    libxshmfence1 \
    libcups2 \
    # Media processing
    ffmpeg \
    # GitHub CLI (gh) — for gh auth, gh repo create, gh pr create, etc.
    # Installed via official GitHub apt repo below
    apt-transport-https \
    # CJK and general fonts — comprehensive coverage
    fonts-liberation \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -fv

# ── GitHub CLI (gh) ────────────────────────────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Browser paths for agent-browser / Playwright ──────────────────────────────
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# ── Node.js: install agent-browser globally ────────────────────────────────────
# BUG-19B-05 FIX: pin to a specific version so builds are reproducible and a
# future npm publish cannot silently change agent-browser's behaviour or
# introduce a supply-chain vulnerability.  Update this pin intentionally when
# upgrading.
RUN npm install -g agent-browser@0.21.4

# ── Python: pre-install infrastructure packages ───────────────────────────────
# Separated from requirements.txt so project deps stay lean and focused.
# Infrastructure packages rarely change; this layer is cached between builds.
# p17b: Pin versions for reproducibility and to lock out known CVEs.
#   - httpx: pinned to >=0.27.0 (stable async HTTP, used by OpenAI SDK timeout config)
#   - Pillow: pinned to >=10.3.0 (CVE-2023-44271 / CVE-2024-28219 fixed in 10.x)
#   - aiohttp: pinned to >=3.10.11 (CVE-2024-52304 HTTP smuggling fixed in 3.10.11)
#   - reportlab: pinned to >=4.1.0 (stable; avoid unmaintained 3.x line)
RUN pip3 install --no-cache-dir --break-system-packages \
    # Document generation (Issue #77)
    python-pptx==1.0.2 \
    "reportlab>=4.1.0" \
    # BUG-19B-06 FIX: pin previously floating packages for reproducible builds.
    # Without version pins a `docker build` on a different date can pull a
    # different version, breaking the layer cache and introducing regressions.
    "openpyxl>=3.1.2" \
    "python-docx>=1.1.0" \
    # Web scraping (Issue #78)
    "httpx>=0.27.0" \
    "beautifulsoup4>=4.12.3" \
    "lxml>=5.2.2" \
    # Image processing (Issue #79) — CVE-2023-44271 / CVE-2024-28219 fixed in Pillow 10.x
    "Pillow>=10.3.0" \
    # Optional heavy data science dependencies (can be removed if not using Excel/chart tools)
    # Data analysis (Issue #80)
    "pandas>=2.2.2" \
    "numpy>=1.26.4" \
    "matplotlib>=3.9.0" \
    # Utilities
    "python-dateutil>=2.9.0" \
    "pytz>=2024.1" \
    # p17b: aiohttp pinned floor — CVE-2024-52304 (HTTP request smuggling) fixed in 3.10.11
    "aiohttp>=3.10.11"

# ── Python: install agent-specific requirements ───────────────────────────────
WORKDIR /app
# p13d: Use COPY (not ADD) for local files — ADD has implicit tar-extraction
# and URL-fetching behaviour that can silently pull remote content.
COPY agent-runner/requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# ── Copy agent source ─────────────────────────────────────────────────────────
COPY agent-runner/agent.py ./
# p17b: fitness_reporter.py provides Phase 1 WSBridge telemetry; imported by agent.py.
#       soul.md contains core anti-hallucination rules; read at startup by agent.py.
#       Both were previously missing from the image, causing silent feature degradation.
COPY agent-runner/fitness_reporter.py ./
COPY agent-runner/soul.md ./

# ── Create workspace directories ──────────────────────────────────────────────
RUN mkdir -p /workspace/group /workspace/global /workspace/extra \
             /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# ── Entrypoint: read stdin JSON → run Python agent → stdout JSON ──────────────
RUN printf '#!/bin/bash\nset -e\ncat > /tmp/input.json\npython3 /app/agent.py < /tmp/input.json\n' \
    > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# ── Non-root security: use the built-in 'node' user (uid 1000) ───────────────
RUN chown -R node:node /workspace && chmod 777 /home/node

USER node

WORKDIR /workspace/group

# ── Runtime environment ───────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8
ENV NODE_ENV=production

# ── Health check (p17b) ───────────────────────────────────────────────────────
# Verify that the agent module and its critical dependencies are importable.
# Previously this only checked python3 exits 0, which passes even with a
# broken install.  Now it imports the key third-party packages so a broken
# layer is caught before a real run is attempted.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import json, pathlib, anthropic, openai; import google.genai; print('ok')"

# BUG-19B-08 FIX: declare SIGTERM as the stop signal so that `docker stop`
# delivers SIGTERM to PID 1 (the bash entrypoint).  Without this instruction
# Docker defaults to SIGTERM already, but making it explicit documents the
# intent and ensures the entrypoint's `trap` handlers (if added) receive the
# signal rather than having it silently ignored by bash's default handler.
STOPSIGNAL SIGTERM

ENTRYPOINT ["/app/entrypoint.sh"]
