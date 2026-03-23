#!/usr/bin/env python3
"""
EvoClaw Interactive Setup
Run this once to configure EvoClaw for the first time.
"""
import subprocess, sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)

def step(n, title):
    print(f"\n{'='*50}")
    print(f"Step {n}: {title}")
    print('='*50)

def main():
    print("\nEvoClaw Setup\n")

    # Step 1: Check Python
    step(1, "Check Python version")
    v = sys.version_info
    if v < (3, 11):
        print(f"  Python 3.11+ required (found {v.major}.{v.minor})")
        sys.exit(1)
    print(f"  Python {v.major}.{v.minor} OK")

    # Step 2: Check Docker
    step(2, "Check Docker")
    try:
        r = run(["docker", "info"], capture_output=True, timeout=10)
        if r.returncode != 0:
            print("  Docker is not running. Please start Docker Desktop or run:")
            print("    sudo systemctl start docker")
            sys.exit(1)
    except FileNotFoundError:
        print("  Docker is not installed.")
        print("  Install from: https://www.docker.com/products/docker-desktop")
        sys.exit(1)
    print("  Docker is running OK")

    # Step 3: Install dependencies
    step(3, "Install Python dependencies")
    r = run([sys.executable, "-m", "pip", "install", "-r", "host/requirements.txt"])
    if r.returncode != 0:
        print("  Failed to install dependencies from host/requirements.txt")
        sys.exit(1)
    print("  Dependencies installed OK")

    # Step 4: Setup .env
    step(4, "Configure .env")
    env_path = Path(".env")
    if not env_path.exists():
        print("\n  Which LLM provider do you want to use?")
        print("  1) Google Gemini   (free tier — recommended for new users)")
        print("  2) NVIDIA NIM      (supports Qwen, Llama, Mistral, and many others)")
        print("  3) OpenAI          (GPT-4 series)")
        print("  4) Anthropic Claude (most reliable, costs more)")
        choice = input("  Enter 1-4 [1]: ").strip() or "1"

        env_lines = []
        if choice == "1":
            key = input("  Enter your Google Gemini API key: ").strip()
            env_lines.append(f"GOOGLE_API_KEY={key}")
        elif choice == "2":
            key = input("  Enter your NVIDIA NIM API key: ").strip()
            env_lines.append(f"NIM_API_KEY={key}")
        elif choice == "3":
            key = input("  Enter your OpenAI API key: ").strip()
            env_lines.append(f"OPENAI_API_KEY={key}")
        elif choice == "4":
            key = input("  Enter your Anthropic Claude API key: ").strip()
            env_lines.append(f"CLAUDE_API_KEY={key}")
        else:
            print(f"  Unknown choice '{choice}', skipping LLM key setup.")

        tg_token = input("  Enter your Telegram bot token (or Enter to skip): ").strip()
        if tg_token:
            env_lines.append(f"TELEGRAM_BOT_TOKEN={tg_token}")

        name = input("  Assistant name [EvoClaw]: ").strip() or "EvoClaw"
        env_lines.append(f"ASSISTANT_NAME={name}")

        owner_ids = input(
            "  Your Telegram/Discord user ID for admin access (or Enter to skip): "
        ).strip()
        if owner_ids:
            env_lines.append(f"OWNER_IDS={owner_ids}")
        else:
            print("  WARNING: OWNER_IDS not set. RBAC will be in fail-open mode (all users allowed).")
            print("  Add OWNER_IDS=<your-user-id> to .env to enable access control.")

        dashboard_pw = input("  Dashboard password (or Enter to leave unprotected): ").strip()
        if dashboard_pw:
            env_lines.append(f"DASHBOARD_PASSWORD={dashboard_pw}")
        else:
            print("  WARNING: DASHBOARD_PASSWORD not set. The web dashboard has NO authentication.")

        env_path.write_text("\n".join(env_lines) + "\n")
        print("  .env created")
    else:
        print("  .env already exists — skipping")

    # Step 5: Init database
    step(5, "Initialize database")
    from host import db, config
    db.init_database(config.STORE_DIR / "messages.db")
    print("  Database initialized OK")

    # Step 6: Create directories
    step(6, "Create directories")
    for d in [config.GROUPS_DIR, config.GROUPS_DIR / "global",
              config.DATA_DIR, config.STORE_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    print("  Directories created OK")

    # Step 7: Build container
    step(7, "Build Docker container")
    print("  Building evoclaw-agent image (this may take 5-10 minutes on first run)...")
    r = run(["docker", "build", "-t", "evoclaw-agent", "."], cwd=Path("container"))
    if r.returncode != 0:
        print("  Container build failed. Common fixes:")
        print("    - docker builder prune -f   (clear stale cache)")
        print("    - check network connectivity (Docker needs internet to download packages)")
        sys.exit(1)
    print("  Container built: evoclaw-agent OK")

    # Step 8: Register main group (optional)
    step(8, "Register main group (optional)")
    print("  You can also do this later by sending /monitor from your Telegram group.")
    jid = input("  Enter your Telegram chat ID (e.g. tg:-1001234567890), or Enter to skip: ").strip()
    if jid:
        group_name = input("  Group name [Main]: ").strip() or "Main"
        folder = jid.replace("tg:", "telegram_").replace("-", "").replace(":", "_")
        db.set_registered_group(
            jid=jid, name=group_name, folder=folder,
            trigger_pattern=None, container_config=None,
            requires_trigger=False, is_main=True,
        )
        print(f"  Main group registered: {group_name} ({jid})")
    else:
        print("  Skipped. Send /monitor from your Telegram group after starting to register it.")

    print("\n" + "="*50)
    print("Setup complete! Start EvoClaw with:")
    print("  python run.py")
    print()
    print("First-run checklist:")
    print("  1. Start EvoClaw: python run.py")
    print("  2. Open your Telegram group and send /monitor to register it")
    print("  3. Send a message to the bot and verify it responds")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
