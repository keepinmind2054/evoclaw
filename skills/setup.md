# Skill: Setup EvoClaw

Initial setup guide for EvoClaw.

## Prerequisites
- Python 3.11+
- Docker installed and running
- A Google account (for Gemini API)

## Step-by-step

### 1. Clone the repo
```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
```

### 2. Get a Gemini API key (free)
- Go to https://aistudio.google.com
- Sign in → Get API key → Create API key
- Copy the key

### 3. Create .env file
```bash
cp .env.example .env   # if exists, or create manually
```
Edit `.env`:
```
GOOGLE_API_KEY=your_gemini_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token  # get from @BotFather
ASSISTANT_NAME=Andy   # change to your preferred name
```

### 4. Install Python dependencies
```bash
pip install -r host/requirements.txt
```

### 5. Initialize the database
```bash
python -c "from host import db, config; db.init_database(config.STORE_DIR / 'messages.db'); print('DB ready')"
```

### 6. Build the Docker container
```bash
cd container
docker build -t evoclaw-agent .
cd ..
```

### 7. Register a Telegram group as main
```bash
python -c "
from host import db, config
db.init_database(config.STORE_DIR / 'messages.db')
db.set_registered_group(
    jid='tg:YOUR_CHAT_ID',
    name='Main',
    folder='telegram_main',
    trigger_pattern=None,
    container_config=None,
    requires_trigger=False,
    is_main=True,
)
print('Main group registered!')
"
```

### 8. Start EvoClaw
```bash
python run.py
```

### 9. Test
Send a message in your Telegram group — EvoClaw should respond!
