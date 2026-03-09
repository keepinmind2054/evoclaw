# Skill: Add Telegram Channel

Adds Telegram bot support to EvoClaw.

## Steps

### 1. Create a Telegram bot
- Message @BotFather on Telegram
- Send `/newbot`, follow prompts
- Copy the token (e.g. `123456789:AAFxxx...`)

### 2. Add token to .env
```
TELEGRAM_BOT_TOKEN=your_token_here
```

### 3. Register your group in the database
Run this once to register your Telegram chat:
```python
python -c "
from host import db, config
db.init_database(config.STORE_DIR / 'messages.db')
db.set_registered_group(
    jid='tg:YOUR_CHAT_ID',       # e.g. tg:-1001234567890
    name='My Group',
    folder='telegram_my-group',  # channel_groupname format
    trigger_pattern=None,
    container_config=None,
    requires_trigger=True,
    is_main=False,
)
print('Group registered!')
"
```

To find your chat ID, add your bot to the group and send a message, then check:
`https://api.telegram.org/botTOKEN/getUpdates`

### 4. Restart EvoClaw
```bash
python run.py
```

### 5. Test
Send `@Andy hello` in your Telegram group — the bot should respond.
