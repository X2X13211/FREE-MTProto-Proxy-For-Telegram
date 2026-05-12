import asyncio
import logging
import httpx
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramRetryAfter
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PROXY_LIST_URL = os.getenv("PROXY_LIST_URL")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
router = Router()

async def get_proxies():
    """Fetches the proxy list from the URL."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(PROXY_LIST_URL)
            response.raise_for_status()
            lines = response.text.splitlines()
            proxies = [line.strip() for line in lines if line.strip().startswith(('tg://', 'https://t.me/proxy'))]
            return proxies
    except Exception as e:
        logger.error(f"Error fetching proxies: {e}")
        return []

SENT_PROXIES_FILE = "sent_proxies.txt"

def load_sent_proxies():
    if not os.path.exists(SENT_PROXIES_FILE):
        return set()
    with open(SENT_PROXIES_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_proxy(proxy):
    with open(SENT_PROXIES_FILE, "a") as f:
        f.write(proxy + "\n")

async def send_proxies_to_channel():
    """Fetches and sends all NEW proxies to the channel one by one."""
    logger.info("Starting proxy update task...")
    proxies = await get_proxies()
    sent_proxies = load_sent_proxies()
    
    if not proxies:
        logger.warning("No proxies found to send.")
        return

    count = 0
    for proxy in proxies:
        if proxy in sent_proxies:
            continue
            
        try:
            parsed_url = urlparse(proxy)
            params = parse_qs(parsed_url.query)
            
            server = params.get('server', [''])[0]
            port = params.get('port', [''])[0]
            secret = params.get('secret', [''])[0]

            if not all([server, port, secret]):
                logger.warning(f"Skipping malformed proxy: {proxy}")
                continue

            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="connect", url=proxy))
            
            message_text = (
                f"Server: {server}\n"
                f"Port: {port}\n"
                f"Secret: {secret}"
            )

            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message_text,
                reply_markup=builder.as_markup()
            )
            save_sent_proxy(proxy)
            count += 1
            await asyncio.sleep(3.5) 
        except TelegramRetryAfter as e:
            logger.warning(f"Flood limit reached. Waiting for {e.retry_after} seconds...")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.error(f"Error sending proxy {proxy}: {e}")
            await asyncio.sleep(5) # Longer delay on error

    logger.info(f"Finished sending {count} new proxies.")

# Admin check filter
async def is_admin(message: types.Message):
    """Checks if the user is an administrator of the target channel."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=message.from_user.id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if await is_admin(message):
        await message.answer("Привет, админ! Бот работает в автоматическом режиме (раз в час).\n\n"
                             "Используй /check чтобы запустить проверку сейчас.")
    else:
        pass

@router.message(Command("check"))
async def cmd_check(message: types.Message):
    if await is_admin(message):
        await message.answer("Запускаю проверку прокси вручную...")
        await send_proxies_to_channel()
        await message.answer("Проверка завершена.")
    else:
        pass # Ignore non-admins

async def on_startup(bot: Bot):
    base_url = os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        logger.warning("RENDER_EXTERNAL_URL not found, webhooks might not work correctly.")
        return

    webhook_url = f"{base_url}/webhook"
    logger.info(f"Setting webhook to: {webhook_url}")
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)

async def main():
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_proxies_to_channel, 'interval', hours=1, next_run_time=datetime.now())
    scheduler.start()

    app = web.Application()
    
    async def health_check(request):
        return web.Response(text="OK")
    
    app.router.add_get("/", health_check)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")

    setup_application(app, dp, bot=bot)
    port = int(os.getenv("PORT", 10000))
    
    # Start server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    logger.info(f"Starting web server on port {port}...")
    await site.start()

    logger.info("Bot is running with webhooks...")
    
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
