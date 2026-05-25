"""
bot.py – Entry point for the SufferingFM bot.
"""
from __future__ import annotations

import logging

from app_bot import create_bot
from bot_commands import register_commands
from config import ADS_DIR, SONGS_DIR, TOKEN

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

SONGS_DIR.mkdir(parents=True, exist_ok=True)
ADS_DIR.mkdir(parents=True, exist_ok=True)

bot = create_bot()
register_commands(bot)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
