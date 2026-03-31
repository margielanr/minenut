import asyncio
import os
import random
import aiohttp
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from datetime import datetime, timezone
import re

# 🔐 FROM GITHUB SECRETS
WEBHOOK_ALERT = os.getenv("WEBHOOK_ALERT")
WEBHOOK_AVAILABLE = WEBHOOK_ALERT

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))
ALERT_TIMES = [600, 300, 60]

# 📄 GitHub Actions runs from repo root
USERNAMES_FILE = "usernames.txt"

LIST_URL = "https://namemc.com/minecraft-names?sort=asc&length_op=eq&length=3&lang=&searches=0"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

fired_alerts = {}
fired_available = set()


async def send_webhook(session, webhook, content, allow_mentions=False):
    if not webhook:
        print("[WARN] No webhook set")
        return

    payload = {
        "content": content,
        "allowed_mentions": (
            {"roles": ["1481496559278358589"]} if allow_mentions else {"parse": []}
        )
    }

    for _ in range(3):
        try:
            async with session.post(webhook, json=payload) as resp:
                if resp.status == 429:
                    retry = float(resp.headers.get("Retry-After", "5"))
                    await asyncio.sleep(retry)
                    continue
                elif resp.status >= 400:
                    print(f"[WEBHOOK ERROR {resp.status}] {await resp.text()}")
                return
        except Exception as e:
            print(f"[WEBHOOK ERROR] {e}")
            await asyncio.sleep(2)


def parse_drop_times(text):
    pattern = r'(\d{1,2}/\d{1,2}/\d{4})\s*[•·]\s*(\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM))'
    matches = re.findall(pattern, text, re.IGNORECASE)

    now = datetime.now(timezone.utc)
    results = []

    for date_str, time_str in matches:
        try:
            dt = datetime.strptime(f"{date_str} {time_str.strip()}", "%m/%d/%Y %I:%M:%S %p")
            dt = dt.replace(tzinfo=timezone.utc)
            if dt > now:
                results.append(dt)
        except:
            pass

    return results


async def get_body_text(page):
    try:
        return await asyncio.wait_for(page.inner_text("body"), timeout=10)
    except:
        try:
            return await asyncio.wait_for(page.evaluate("document.body.innerText"), timeout=10)
        except:
            return ""


async def new_page(browser):
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
    )
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    return context, page


async def scrape_list_page(browser):
    results = {}
    context = None

    try:
        context, page = await new_page(browser)
        await page.goto(LIST_URL, timeout=30000)

        await page.wait_for_timeout(3000)
        body_text = await get_body_text(page)

        if not body_text:
            return {}

        if "cloudflare" in body_text.lower():
            print("[LIST] Blocked by Cloudflare")
            return {}

        for line in body_text.splitlines():
            line = line.strip()
            m = re.match(r'^([A-Za-z0-9_]{3})\s', line)
            if m:
                username = m.group(1)
                drops = parse_drop_times(line)
                if drops:
                    results[username] = drops

        print(f"[LIST] Found {len(results)} names")
        return results

    except Exception as e:
        print(f"[LIST ERROR] {e}")
        return {}

    finally:
        if context:
            await context.close()


async def check_username(browser, username, session):
    context = None

    try:
        context, page = await new_page(browser)
        await page.goto(f"https://namemc.com/profile/{username}", timeout=30000)

        await page.wait_for_timeout(3000)
        body = await get_body_text(page)

        if not body:
            return []

        lower = body.lower()

        if "cloudflare" in lower:
            print(f"[{username}] Blocked")
            return []

        if "available" in lower:
            if username not in fired_available:
                fired_available.add(username)
                msg = f"🟢 `{username}` AVAILABLE NOW\nhttps://namemc.com/profile/{username}"
                print(f"[AVAILABLE] {username}")
                await send_webhook(session, WEBHOOK_AVAILABLE, msg, True)
            return []

        return parse_drop_times(body)

    except Exception as e:
        print(f"[ERROR] {username}: {e}")
        return []

    finally:
        if context:
            await context.close()


async def process_alerts(username, drop_times, session):
    if not drop_times:
        return

    now = datetime.now(timezone.utc)
    earliest = min(drop_times)
    seconds_left = (earliest - now).total_seconds()

    if username not in fired_alerts:
        fired_alerts[username] = set()

    for t in ALERT_TIMES:
        if t in fired_alerts[username]:
            continue

        if 0 < seconds_left <= t + CHECK_INTERVAL:
            msg = f"⏰ `{username}` drops soon ({t//60}m)\nhttps://namemc.com/profile/{username}"
            print(f"[ALERT] {username}")
            await send_webhook(session, WEBHOOK_ALERT, msg, True)
            fired_alerts[username].add(t)


async def main():
    manual_usernames = []

    if os.path.exists(USERNAMES_FILE):
        with open(USERNAMES_FILE) as f:
            manual_usernames = [x.strip() for x in f if x.strip()]

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            try:
                # 🔁 ONE CYCLE ONLY (GitHub Actions friendly)

                print(f"[CHECK] Manual usernames: {len(manual_usernames)}")

                for username in manual_usernames:
                    drops = await check_username(browser, username, session)
                    await process_alerts(username, drops, session)
                    await asyncio.sleep(random.uniform(2, 5))

                print("[CHECK] Scraping list...")

                list_data = await scrape_list_page(browser)

                for username, drops in list_data.items():
                    await process_alerts(username, drops, session)
                    await asyncio.sleep(random.uniform(1, 2))

                print("[DONE] Cycle complete")

            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
