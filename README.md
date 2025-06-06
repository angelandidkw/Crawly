# Crawly

Crawly is a simple Discord bot that demonstrates using the **Scrapy** framework within a bot. The project uses [`discord.py`](https://discordpy.readthedocs.io/) and loads its configuration from a `.env` file.

## Setup

1. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and add your bot token:
   ```bash
   cp .env.example .env
   # Edit .env and put your token
   ```
3. Run the bot:
   ```bash
   python bot.py
   ```

## Commands

- `!ping` – replies with Pong! using an embed.
- `!echo <message>` – echoes back your message.
- `!random` – shows a random number between 1 and 100.
- `!info` – shows information about the bot.
- `!crawl <url>` – crawls the page and returns its `<title>` using Scrapy.

## Notes

The crawl command is a simplified example. For large websites consider running Scrapy as a separate spider.
