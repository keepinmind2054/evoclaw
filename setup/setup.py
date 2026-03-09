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
    print("\n🦀 EvoClaw Setup\n")

    # Step 1: Check Python
    step(1, "Check Python version")
    v = sys.version_info
    if v < (3, 11):
        print(f"✗ Python 3.11+ required (found {v.major}.{v.minor})")
        sys.exit(1)
    print(f"✓ Python {v.major}.{v.minor}")

    # Step 2: Check Docker
    step(2, "Check Docker")
    r = run(["docker", "info"], capture_output=True, timeout=10)
    if r.returncode != 0:
        print("✗ Docker not running. Please start Docker and retry.")
        sys.exit(1)
    print("✓ Docker is running")

    # Step 3: Install dependencies
    step(3, "Install Python dependencies")
    r = run([sys.executable, "-m", "pip", "install", "-r", "host/requirements.txt"])
    if r.returncode != 0:
        print("✗ Failed to install dependencies")
        sys.exit(1)
    print("✓ Dependencies installed")

    # Step 4: Setup .env
    step(4, "Configure .env")
    env_path = Path(".env")
    if not env_path.exists():
        api_key = input("Enter your Google Gemini API key: ").strip()
        tg_token = input("Enter your Telegram bot token (or press Enter to skip): ").strip()
        name = input("Assistant name (default: Andy): ").strip() or "Andy"
        env_content = f"GOOGLE_API_KEY={api_key}\n"
        if tg_token:
            env_content += f"TELEGRAM_BOT_TOKEN={tg_token}\n"
        env_content += f"ASSISTANT_NAME={name}\n"
        env_path.write_text(env_content)
        print("✓ .env created")
    else:
        print("✓ .env already exists")

    # Step 5: Init database
    step(5, "Initialize database")
    from host import db, config
    db.init_database(config.STORE_DIR / "messages.db")
    print("✓ Database initialized")

    # Step 6: Create group dirs
    step(6, "Create directories")
    for d in [config.GROUPS_DIR, config.GROUPS_DIR / "global",
              config.DATA_DIR, config.STORE_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    print("✓ Directories created")

    # Step 7: Build container
    step(7, "Build Docker container")
    print("Building evoclaw-agent image (this may take a few minutes)...")
    r = run(["docker", "build", "-t", "evoclaw-agent", "."], cwd=Path("container"))
    if r.returncode != 0:
        print("✗ Container build failed")
        sys.exit(1)
    print("✓ Container built: evoclaw-agent")

    # Step 8: Register main group
    step(8, "Register main group (optional)")
    jid = input("Enter your Telegram chat ID (e.g. tg:-1001234567890), or Enter to skip: ").strip()
    if jid:
        name = input("Group name: ").strip() or "Main"
        folder = jid.replace("tg:", "telegram_").replace("-", "").replace(":", "_")
        db.set_registered_group(
            jid=jid, name=name, folder=folder,
            trigger_pattern=None, container_config=None,
            requires_trigger=False, is_main=True,
        )
        print(f"✓ Main group registered: {name}")

    print("\n" + "="*50)
    print("✅ Setup complete! Start EvoClaw with:")
    print("   python run.py")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
