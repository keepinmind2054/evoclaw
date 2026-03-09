# EvoClaw Agent Container
# Runs Gemini API agent (Python) in isolated Linux VM with browser automation

FROM node:22-slim

# Install system dependencies: Python3, Chromium, fonts
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    chromium \
    fonts-liberation \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    libgbm1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libcups2 \
    libdrm2 \
    libxshmfence1 \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set Chromium path for agent-browser
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Install agent-browser globally (Node.js tool for web browsing)
RUN npm install -g agent-browser

# Set up Python app directory
WORKDIR /app

# Install Python dependencies
COPY agent-runner/requirements.txt ./
RUN pip3 install --break-system-packages -r requirements.txt

# Copy Python agent
COPY agent-runner/agent.py ./

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input

# Create entrypoint: read stdin JSON, run Python agent
RUN printf '#!/bin/bash\nset -e\ncat > /tmp/input.json\npython3 /app/agent.py < /tmp/input.json\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Set ownership for non-root user
RUN chown -R node:node /workspace && chmod 777 /home/node

# Switch to non-root user
USER node

# Set working directory
WORKDIR /workspace/group

# Entry point reads JSON from stdin, outputs JSON to stdout
ENTRYPOINT ["/app/entrypoint.sh"]
