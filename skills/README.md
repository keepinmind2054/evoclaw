# EvoClaw Skills

Each skill is a guide for adding new capabilities to EvoClaw.
All skills are written for the Python-based EvoClaw stack.

## Available Skills

| Skill | Description |
|-------|-------------|
| [setup.md](setup.md) | Initial setup guide |
| [add-telegram.md](add-telegram.md) | Add Telegram bot support |
| [add-whatsapp.md](add-whatsapp.md) | Add WhatsApp support |
| [add-discord.md](add-discord.md) | Add Discord bot support |
| [add-slack.md](add-slack.md) | Add Slack support |
| [add-gmail.md](add-gmail.md) | Add Gmail support |
| [add-image-vision.md](add-image-vision.md) | Enable image/vision analysis |

## How to use a skill

Each skill file contains:
1. What Python files to create or modify
2. What dependencies to install (`pip install ...`)
3. How to register the new channel in the database
4. How to test it

## Adding a new channel (general pattern)

1. Create `host/channels/your_channel.py`
2. Implement the `Channel` protocol (connect, send_message, owns_jid, is_connected, disconnect)
3. Call `register_channel_class("name", YourChannel)` at the bottom
4. Import it in `host/main.py`
5. Instantiate and `await channel.connect()` in `main()`
