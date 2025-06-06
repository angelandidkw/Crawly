import os
import asyncio
import random
import discord
from discord.ext import commands
from dotenv import load_dotenv
import scrapy
from scrapy.crawler import CrawlerProcess

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

class TitleSpider(scrapy.Spider):
    name = "title_spider"
    custom_settings = {"LOG_ENABLED": False}

    def __init__(self, url: str, **kwargs):
        super().__init__(**kwargs)
        self.start_urls = [url]
        self.found_title = None

    def parse(self, response):
        self.found_title = response.css("title::text").get()


def fetch_title(url: str) -> str:
    process = CrawlerProcess()
    spider = TitleSpider(url)
    process.crawl(spider)
    process.start()  # blocks until finished
    return spider.found_title or "No title found"


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.command()
async def ping(ctx: commands.Context):
    embed = discord.Embed(title="Pong!", color=discord.Color.green())
    await ctx.send(embed=embed)


@bot.command()
async def echo(ctx: commands.Context, *, message: str):
    embed = discord.Embed(title="Echo", description=message, color=discord.Color.blue())
    await ctx.send(embed=embed)


@bot.command()
async def randomnumber(ctx: commands.Context):
    number = random.randint(1, 100)
    embed = discord.Embed(title="Random Number", description=str(number), color=discord.Color.purple())
    await ctx.send(embed=embed)


@bot.command()
async def info(ctx: commands.Context):
    embed = discord.Embed(title="Crawly", description="A simple web crawling bot.", color=discord.Color.orange())
    embed.add_field(name="Library", value="discord.py", inline=False)
    embed.add_field(name="Commands", value="ping, echo, randomnumber, info, crawl", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def crawl(ctx: commands.Context, url: str):
    await ctx.trigger_typing()
    loop = asyncio.get_running_loop()
    title = await loop.run_in_executor(None, fetch_title, url)
    embed = discord.Embed(title="Crawl Result", description=f"Title: {title}", color=discord.Color.red())
    await ctx.send(embed=embed)


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in the environment")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
