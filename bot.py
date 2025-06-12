"""
Discord Web Crawler Bot – Global Slash + Prefix Commands
────────────────────────────────────────────────────────
• All interactions are available both as `/slash` commands and legacy `!` prefix.
• Global sync (may take up to 1 hour) – no guild IDs required.
• Features: title, links, images, meta-description, text snippet, common-wordlist
  directory discovery and directory-listing analysis.
"""

import os
import re
import asyncio
import time
import logging
import functools
import signal
from typing import Optional, Dict, Any, List, Set
from urllib.parse import urlparse, urljoin

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ──────────────────────────── Config ────────────────────────────────

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in the environment")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crawler_bot")

intents = discord.Intents.default()
intents.message_content = True  # keep for legacy prefix commands

# ────────────────────────── Constants ───────────────────────────────

RATE_LIMIT = 5  # seconds between requests per user
user_last_request: Dict[int, float] = {}

TIMEOUT = 10
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# Common wordlists (trim / extend as needed)
COMMON_DIRS = [
    "admin", "administrator", "login", "wp-admin", "wp-login", "api", "v1", "v2", "rest",
    "dashboard", "panel", "uploads", "images", "assets", "backup", "old", "dev", "test",
    "config", "docs", "includes", "scripts", "data", "logs", "private", "secure", "user",
    "shop", "blog", "search"
]

COMMON_FILES = [
    "robots.txt", "sitemap.xml", "favicon.ico", "humans.txt", "security.txt", "manifest.json",
    "index.html", "index.php", "login.php", "register.php", "admin.php", "config.php",
    "db.php", "readme.txt", "phpinfo.php", "backup.sql", "wp-config.php", ".env", "package.json"
]

DIRECTORY_INDICATORS = [
    "index of", "directory listing", "parent directory", "name", "size", "modified",
    "<title>index of", "apache", "nginx"
]

# ────────────────────────── Helper funcs ────────────────────────────

def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False

def check_rate_limit(user_id: int) -> Optional[str]:
    now = time.time()
    last = user_last_request.get(user_id, 0)
    if now - last < RATE_LIMIT:
        return f"Please wait {RATE_LIMIT - int(now-last)} s before making another request."
    user_last_request[user_id] = now
    return None

def clean_text(txt: str) -> str:
    return re.sub(r"\s+", " ", txt.strip()) if txt else ""

def truncate(txt: str, n: int = 1000) -> str:
    return txt if len(txt) <= n else txt[: n-3] + "…"

def get_base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def is_dir_listing(html: str) -> bool:
    low = html.lower()
    return any(k in low for k in DIRECTORY_INDICATORS)

# ────────────────────── Async HTTP Scraper ──────────────────────────

class WebScraper:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_request_time = 0
        self.min_request_interval = 1.0  # Minimum seconds between requests

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                headers=DEFAULT_HEADERS,
                connector=aiohttp.TCPConnector(limit=20),
            )
        return self.session

    async def _rate_limit(self):
        """Ensure minimum time between requests"""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()

    # HEAD helper
    async def head(self, url: str) -> Dict[str, Any]:
        try:
            await self._rate_limit()
            async with (await self._session()).head(url, allow_redirects=True) as r:
                return {
                    "ok": True,
                    "url": str(r.url),
                    "status": r.status,
                    "type": r.headers.get("content-type", ""),
                }
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error for {url}: {str(e)}")
            return {"ok": False, "error": f"HTTP error: {str(e)}", "url": url}
        except Exception as e:
            logger.error(f"Unexpected error for {url}: {str(e)}")
            return {"ok": False, "error": str(e), "url": url}

    # GET helper
    async def get_raw(self, url: str) -> Dict[str, Any]:
        try:
            await self._rate_limit()
            async with (await self._session()).get(url, allow_redirects=True) as r:
                body = await r.read()
                if len(body) > MAX_CONTENT_LENGTH:
                    logger.warning(f"Content too large for {url}: {len(body)} bytes")
                    return {"ok": False, "error": "Content too large", "status": r.status}
                return {
                    "ok": True,
                    "url": str(r.url),
                    "status": r.status,
                    "headers": dict(r.headers),
                    "body": body,
                }
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error for {url}: {str(e)}")
            return {"ok": False, "error": f"HTTP error: {str(e)}", "url": url}
        except Exception as e:
            logger.error(f"Unexpected error for {url}: {str(e)}")
            return {"ok": False, "error": str(e), "url": url}

    # High-level helpers
    async def fetch_page(self, url: str) -> Dict[str, Any]:
        res = await self.get_raw(url)
        if not res.get("ok"):
            return res
        html = res["body"].decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        res.update({"soup": soup, "text": html, "is_dir": is_dir_listing(html)})
        return res

    async def discover(self, base: str) -> Dict[str, Any]:
        wordlist = COMMON_DIRS + COMMON_FILES
        base = base.rstrip("/")
        tasks = [self.head(f"{base}/{w}") for w in wordlist]
        results = await asyncio.gather(*tasks)
        found = [r for r in results if r.get("ok") and r.get("status") in {200, 301, 302, 403}]
        return {"ok": True, "found": found, "checked": len(wordlist), "base": base}

    async def analyze_dir(self, url: str) -> Dict[str, Any]:
        res = await self.fetch_page(url)
        if not res.get("ok"):
            return res
        soup: BeautifulSoup = res["soup"]
        links = []
        files = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full = urljoin(url, href)
            if not is_valid_url(full):
                continue
            if re.search(r"\.[a-z0-9]{1,4}$", href, re.I):
                files.append(full)
            else:
                links.append(full)
        return {
            "ok": True,
            "url": res["url"],
            "status": res["status"],
            "is_listing": res["is_dir"],
            "server": res["headers"].get("server", ""),
            "links": links[:20],
            "files": files[:20],
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

scraper = WebScraper()

# ────────────────────── Embed / Error helpers ───────────────────────

def err_embed(msg: str, url: str) -> discord.Embed:
    e = discord.Embed(title="❌ Error", description=msg, color=discord.Color.red())
    e.add_field(name="URL", value=url, inline=False)
    return e

# Directory results builder

def build_dirs_embed(base: str, result: Dict[str, Any]):
    if not result["ok"]:
        return err_embed(result.get("error", "Unknown"), base), None

    found = result["found"]
    if not found:
        e = discord.Embed(
            title="📁 Directory Discovery Complete",
            description="No common paths found.",
            color=discord.Color.yellow(),
        )
        e.add_field(name="Base URL", value=base, inline=False)
        e.add_field(name="Paths Checked", value=str(result["checked"]), inline=True)
        return e, None

    e = discord.Embed(title="📁 Directory Discovery", color=discord.Color.green())
    e.add_field(name="Base URL", value=base, inline=False)
    e.add_field(name="Total Found", value=str(len(found)), inline=True)

    sample = []
    for r in found[:10]:
        emoji = "✅" if r["status"] == 200 else "🔒" if r["status"] == 403 else "↩️"
        path = r["url"].replace(base, "")
        sample.append(f"{emoji} `{path}` ({r['status']})")
    e.add_field(name="Examples", value="\n".join(sample), inline=False)

    view = DirectoryView(base, found)
    return e, view

# ────────────────────── Discord Bot / Cogs ──────────────────────────

class CrawlerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False

    async def setup_hook(self):
        await self.add_cog(CrawlCog(self))

    async def on_ready(self):
        if not self.synced:
            await self.tree.sync()
            self.synced = True
            logger.info("Global slash commands synced.")
        logger.info(f"Logged in as {self.user} ({self.user.id})")

bot = CrawlerBot()

# ──────────────────── Cog with prefix commands ─────────────────────

class CrawlCog(commands.Cog):
    """All **!prefix** commands live here. Separate thin wrappers expose them as global slash commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Rate-limit / URL sanity for Interactions
    async def _check(self, inter: discord.Interaction, url: str) -> bool:
        if (m := check_rate_limit(inter.user.id)):
            await inter.response.send_message(f"⏰ {m}", ephemeral=True)
            return False
        if not is_valid_url(url):
            await inter.response.send_message("❌ Invalid URL.", ephemeral=True)
            return False
        return True

    # ----------------------------------------------------------------------------
    # Prefix commands
    # ----------------------------------------------------------------------------

    @commands.command(name="crawl_dirs")
    async def crawl_dirs_prefix(self, ctx: commands.Context, url: str):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        msg = await ctx.send(f"Scanning `{get_base(url)}` …")
        async with ctx.typing():
            result = await scraper.discover(get_base(url))
        embed, view = build_dirs_embed(get_base(url), result)
        await msg.edit(content=None, embed=embed, view=view)

    # ---- crawl_dir_analyze
    @commands.command(name="crawl_dir_analyze")
    async def crawl_dir_analyze_prefix(self, ctx: commands.Context, url: str):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        async with ctx.typing():
            res = await scraper.analyze_dir(url)
        if not res.get("ok"):
            await ctx.send(embed=err_embed(res.get("error","fail"), url)); return
        e = discord.Embed(title="🔍 Directory Analysis", color=discord.Color.blue())
        e.add_field(name="URL", value=res["url"], inline=False)
        e.add_field(name="Status", value=str(res["status"]), inline=True)
        e.add_field(name="Listing", value="✅ Yes" if res["is_listing"] else "❌ No", inline=True)
        if res["server"]:
            e.add_field(name="Server", value=res["server"], inline=True)
        if res["links"]:
            e.add_field(name="Links", value="\n".join(res["links"]), inline=False)
        if res["files"]:
            e.add_field(name="Files", value="\n".join(res["files"]), inline=False)
        await ctx.send(embed=e)

    # ---- crawl_title
    @commands.command(name="crawl_title")
    async def crawl_title_prefix(self, ctx: commands.Context, url: str):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        async with ctx.typing():
            res = await scraper.fetch_page(url)
        if not res.get("ok"):
            await ctx.send(embed=err_embed(res.get("error","fail"), url)); return
        title_tag = res["soup"].find("title") if res.get("soup") else None
        title = clean_text(title_tag.get_text()) if title_tag else "No title found"
        e = discord.Embed(title="🌐 Page Title", description=truncate(title,400), color=discord.Color.green())
        e.add_field(name="URL", value=url, inline=False)
        await ctx.send(embed=e)

    # ---- crawl_links
    @commands.command(name="crawl_links")
    async def crawl_links_prefix(self, ctx: commands.Context, url: str):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        async with ctx.typing():
            res = await scraper.fetch_page(url)
        if not res.get("ok"):
            await ctx.send(embed=err_embed(res.get("error","fail"), url)); return
        base = res["url"]
        links = {
            urljoin(base, a["href"].strip())
            for a in res["soup"].find_all("a", href=True)
            if a["href"].strip() and not a["href"].startswith("#")
        }
        links = [l for l in links if is_valid_url(l)]
        e = discord.Embed(title="🔗 Page Links", description=truncate("\n".join(sorted(links)) or "No links found.", 4000), color=discord.Color.blue())
        await ctx.send(embed=e)

    # ---- crawl_images
    @commands.command(name="crawl_images")
    async def crawl_images_prefix(self, ctx: commands.Context, url: str):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        async with ctx.typing():
            res = await scraper.fetch_page(url)
        if not res.get("ok"):
            await ctx.send(embed=err_embed(res.get("error","fail"), url)); return
        base = res["url"]
        imgs = {
            urljoin(base, img["src"].strip())
            for img in res["soup"].find_all("img", src=True)
            if img["src"].strip()
        }
        e = discord.Embed(title="🖼️ Page Images", description=truncate("\n".join(sorted(imgs)) or "No images found.", 4000), color=discord.Color.purple())
        e.add_field(name="Total", value=str(len(imgs)))
        await ctx.send(embed=e)

    # ---- crawl_meta
    @commands.command(name="crawl_meta")
    async def crawl_meta_prefix(self, ctx: commands.Context, url: str):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        async with ctx.typing():
            res = await scraper.fetch_page(url)
        if not res.get("ok"):
            await ctx.send(embed=err_embed(res.get("error","fail"), url)); return
        soup = res["soup"]
        tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        desc = clean_text(tag.get("content", "")) if tag else "No meta description found"
        e = discord.Embed(title="📝 Meta Description", description=truncate(desc,1000), color=discord.Color.orange())
        await ctx.send(embed=e)

    # ---- crawl_text
    @commands.command(name="crawl_text")
    async def crawl_text_prefix(self, ctx: commands.Context, url: str, max_chars: int = 500):
        if (m := check_rate_limit(ctx.author.id)):
            await ctx.send(f"⏰ {m}"); return
        if not is_valid_url(url):
            await ctx.send("❌ Invalid URL."); return
        async with ctx.typing():
            res = await scraper.fetch_page(url)
        if not res.get("ok"):
            await ctx.send(embed=err_embed(res.get("error","fail"), url)); return
        soup = res["soup"]
        for t in soup(["script","style","nav","header","footer"]):
            t.decompose()
        txt = clean_text(soup.get_text())
        e = discord.Embed(title="📄 Page Text Snippet", description=truncate(txt,max_chars) or "No visible text", color=discord.Color.dark_green())
        e.set_footer(text=f"Showing up to {max_chars} characters")
        await ctx.send(embed=e)

# ────────────────── Slash wrappers calling Cog methods ──────────────

def get_cog() -> CrawlCog:
    return bot.get_cog("CrawlCog")

class InteractionContext:
    """A context-like wrapper for interactions"""
    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.author = interaction.user
        self.send = interaction.response.send_message
        self._deferred = False

    async def typing(self):
        """Async context manager for typing indicator"""
        if not self._deferred:
            await self.interaction.response.defer()
            self._deferred = True
        return self

    async def __aenter__(self):
        return await self.typing()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

@bot.tree.command(name="crawl_dirs", description="Discover common dirs & files")
async def crawl_dirs_slash(interaction: discord.Interaction, url: str):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_dirs_prefix(ctx, url)

@bot.tree.command(name="crawl_dir_analyze", description="Analyze a directory listing")
async def crawl_dir_ana_slash(interaction: discord.Interaction, url: str):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_dir_analyze_prefix(ctx, url)

@bot.tree.command(name="crawl_title", description="Fetch page title")
async def crawl_title_slash(interaction: discord.Interaction, url: str):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_title_prefix(ctx, url)

@bot.tree.command(name="crawl_links", description="List links on a page")
async def crawl_links_slash(interaction: discord.Interaction, url: str):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_links_prefix(ctx, url)

@bot.tree.command(name="crawl_images", description="List images on a page")
async def crawl_images_slash(interaction: discord.Interaction, url: str):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_images_prefix(ctx, url)

@bot.tree.command(name="crawl_meta", description="Fetch page meta description")
async def crawl_meta_slash(interaction: discord.Interaction, url: str):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_meta_prefix(ctx, url)

@bot.tree.command(name="crawl_text", description="Fetch page text snippet")
async def crawl_text_slash(interaction: discord.Interaction, url: str, max_chars: int = 500):
    cog = get_cog()
    ctx = InteractionContext(interaction)
    await cog.crawl_text_prefix(ctx, url, max_chars)

# ───────────────────────── Views (Directory + API) ───────────────────

class DirectoryView(discord.ui.View):
    def __init__(self, base_url: str, found: List[Dict[str, Any]]):
        super().__init__(timeout=300)
        self.base_url = base_url
        self.found = found
        self.page_size = 10
        self.current_page = 0
        self.max_pages = (len(found) + self.page_size - 1) // self.page_size

    def _get_page_content(self) -> str:
        start = self.current_page * self.page_size
        end = start + self.page_size
        page_items = self.found[start:end]
        lines = [f"`{d['url'].replace(self.base_url,'')}` → {d['status']}" for d in page_items]
        return "\n".join(lines) or "(none)"

    @discord.ui.button(label="List paths", style=discord.ButtonStyle.blurple)
    async def list_paths(self, interaction: discord.Interaction, btn: discord.ui.Button):
        content = self._get_page_content()
        await interaction.response.send_message(
            f"Page {self.current_page + 1}/{self.max_pages}\n{content}"[:2000],
            ephemeral=True
        )

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def prev_page(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            content = self._get_page_content()
            await interaction.response.edit_message(
                content=f"Page {self.current_page + 1}/{self.max_pages}\n{content}"[:2000]
            )
        else:
            await interaction.response.defer()

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            content = self._get_page_content()
            await interaction.response.edit_message(
                content=f"Page {self.current_page + 1}/{self.max_pages}\n{content}"[:2000]
            )
        else:
            await interaction.response.defer()

    async def on_timeout(self):
        # Disable all buttons when the view times out
        for item in self.children:
            item.disabled = True

# ───────────────────────── graceful shutdown ─────────────────────────

async def close_all():
    """Gracefully shutdown all resources"""
    logger.info("Shutting down...")
    try:
        # Close the web scraper session
        await scraper.close()
        logger.info("Web scraper closed")
        
        # Close the bot
        await bot.close()
        logger.info("Bot closed")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
    finally:
        # Ensure the event loop is closed
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            loop.close()
        logger.info("Event loop closed")

def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    asyncio.create_task(close_all())

# Set up signal handlers
loop = asyncio.get_event_loop()
for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, handle_shutdown)

if __name__ == "__main__":
    try:
        logger.info("Starting bot...")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        asyncio.run(close_all())
