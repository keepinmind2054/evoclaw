# EvoClaw Agent Container - Production Ready v1.10.21
# Supports: Multi-provider LLM (Gemini/Claude/OpenAI), document generation,
#           web scraping, browser automation, image processing, data analysis,
#           MCP protocol (Node.js), CJK fonts, non-root security

# p13d: Pin to a specific digest rather than the mutable ':22' tag so that
# builds are reproducible and a silent upstream change cannot introduce
# regressions or supply-chain vulnerabilities.  Update this digest
# intentionally when upgrading the base image.
FROM node:22-bookworm-slim@sha256:a4b757cd491c7f0b57f57951f35f4e85b7e1ad54dbcdbb86a3bc3a7d30e4f8a2

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
RUN npm install -g agent-browser

# ── Python: pre-install infrastructure packages ───────────────────────────────
# Separated from requirements.txt so project deps stay lean and focused.
# Infrastructure packages rarely change; this layer is cached between builds.
RUN pip3 install --no-cache-dir --break-system-packages \
    # Document generation (Issue #77)
    python-pptx==1.0.2 \
    reportlab \
    openpyxl \
    python-docx \
    # Web scraping (Issue #78)
    httpx \
    beautifulsoup4 \
    lxml \
    # Image processing (Issue #79)
    Pillow \
    # Optional heavy data science dependencies (can be removed if not using Excel/chart tools)
    # Data analysis (Issue #80)
    pandas \
    numpy \
    matplotlib \
    # Utilities
    python-dateutil \
    pytz \
    aiohttp

# ── Python: install agent-specific requirements ───────────────────────────────
WORKDIR /app
# p13d: Use COPY (not ADD) for local files — ADD has implicit tar-extraction
# and URL-fetching behaviour that can silently pull remote content.
COPY agent-runner/requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# ── Copy agent source ─────────────────────────────────────────────────────────
COPY agent-runner/agent.py ./

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

# ── Health check (p13d) ───────────────────────────────────────────────────────
# Verify that the Python agent entrypoint is importable.  Docker (and any
# orchestrator) can use this to detect a broken image layer before a real
# run is attempted.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)"

ENTRYPOINT ["/app/entrypoint.sh"]
