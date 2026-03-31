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

USERNAMES_FILE = "usernames.txt"

LIST_URL = "https://namemc.com/minecraft-names?sort=asc&length_op=eq&length=3"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0",
]

fired_alerts = {}
fired_available = set()


# 🧠 Pretty logger
def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


async def send_webhook(session, webhook, content, allow_mentions=False):
    if not webhook:
        log("[WEBHOOK] ❌ No webhook set")
        return

    payload = {
        "content": content,
        "allowed_mentions": (
            {"roles": ["1481496559278358589"]} if allow_mentions else {"parse": []}
        )
    }

    for attempt in range(3):
        try:
            log(f"[WEBHOOK] Sending (attempt {attempt+1})")
            async with session.post(webhook, json=payload) as resp:
                log(f"[WEBHOOK] Status: {resp.status}")

                if resp.status == 429:
                    retry = float(resp.headers.get("Retry-After", "5"))
                    log(f"[WEBHOOK] Rate limited, sleeping {retry}s")
                    await asyncio.sleep(retry)
                    continue

                if resp.status >= 400:
                    log(f"[WEBHOOK ERROR] {await resp.text()}")

                return

        except Exception as e:
            log(f"[WEBHOOK EXCEPTION] {e}")
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
                log(f"[PARSE] Found future drop: {dt}")

        except Exception as e:
            log(f"[PARSE ERROR] {e}")

    return results


async def get_body_text(page):
    try:
        text = await asyncio.wait_for(page.inner_text("body"), timeout=10)
        log(f"[PAGE] Body length: {len(text)} chars")
        return text
    except:
        try:
            text = await asyncio.wait_for(page.evaluate("document.body.innerText"), timeout=10)
            log(f"[PAGE] Body fallback length: {len(text)} chars")
            return text
        except:
            log("[PAGE] ❌ Failed to read body")
            return ""


async def new_page(browser):
    ua = random.choice(USER_AGENTS)
    log(f"[BROWSER] New page with UA: {ua}")

    context = await browser.new_context(
        user_agent=ua,
        viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
    )

    page = await context.new_page()
    await Stealth().apply_stealth_async(page)

    return context, page


async def scrape_list_page(browser):
    log("[LIST] Starting scrape")
    results = {}
    context = None

    try:
        context, page = await new_page(browser)

        log(f"[LIST] Navigating → {LIST_URL}")
        await page.goto(LIST_URL, timeout=30000)

        await page.wait_for_timeout(3000)
        body_text = await get_body_text(page)

        if not body_text:
            log("[LIST] ❌ Empty body")
            return {}

        if "cloudflare" in body_text.lower():
            log("[LIST] ❌ Blocked by Cloudflare")
            return {}

        for line in body_text.splitlines():
            m = re.match(r'^([A-Za-z0-9_]{3})\s', line.strip())
            if m:
                username = m.group(1)
                drops = parse_drop_times(line)

                if drops:
                    log(f"[LIST] Found: {username}")
                    results[username] = drops

        log(f"[LIST] ✅ Total found: {len(results)}")
        return results

    except Exception as e:
        log(f"[LIST ERROR] {e}")
        return {}

    finally:
        if context:
            await context.close()
            log("[LIST] Closed context")


async def check_username(browser, username, session):
    log(f"[CHECK] Checking: {username}")
    context = None

    try:
        context, page = await new_page(browser)

        url = f"https://namemc.com/profile/{username}"
        log(f"[CHECK] Visiting → {url}")

        await page.goto(url, timeout=30000)
        await page.wait_for_timeout(3000)

        body = await get_body_text(page)

        if not body:
            log(f"[{username}] ❌ No body")
            return []

        lower = body.lower()

        if "cloudflare" in lower:
            log(f"[{username}] ❌ Cloudflare block")
            return []

        if "available" in lower:
            if username not in fired_available:
                fired_available.add(username)
                log(f"[{username}] 🟢 AVAILABLE")

                msg = f"🟢 `{username}` AVAILABLE NOW\nhttps://namemc.com/profile/{username}"
                await send_webhook(session, WEBHOOK_AVAILABLE, msg, True)

            return []

        drops = parse_drop_times(body)

        if drops:
            log(f"[{username}] ⏰ Drops found: {len(drops)}")
        else:
            log(f"[{username}] No drops")

        return drops

    except Exception as e:
        log(f"[ERROR] {username}: {e}")
        return []

    finally:
        if context:
            await context.close()
            log(f"[{username}] Closed context")


async def process_alerts(username, drop_times, session):
    if not drop_times:
        return

    now = datetime.now(timezone.utc)
    earliest = min(drop_times)
    seconds_left = (earliest - now).total_seconds()

    log(f"[ALERT CHECK] {username} → {seconds_left:.1f}s left")

    if username not in fired_alerts:
        fired_alerts[username] = set()

    for t in ALERT_TIMES:
        if t in fired_alerts[username]:
            continue

        if 0 < seconds_left <= t + CHECK_INTERVAL:
            log(f"[ALERT] 🚨 Triggering {username} ({t}s)")

            msg = f"⏰ `{username}` drops soon ({t//60}m)\nhttps://namemc.com/profile/{username}"
            await send_webhook(session, WEBHOOK_ALERT, msg, True)

            fired_alerts[username].add(t)


async def main():
    log("🚀 Script start")

    manual_usernames = []

    if os.path.exists(USERNAMES_FILE):
        with open(USERNAMES_FILE) as f:
            manual_usernames = [x.strip() for x in f if x.strip()]
        log(f"[INIT] Loaded {len(manual_usernames)} usernames")
    else:
        log("[INIT] No usernames.txt found")

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            log("[BROWSER] Launching Chromium")

            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            try:
                log("[PHASE] Manual checks")

                for username in manual_usernames:
                    drops = await check_username(browser, username, session)
                    await process_alerts(username, drops, session)

                    sleep_time = random.uniform(2, 5)
                    log(f"[SLEEP] {sleep_time:.2f}s")
                    await asyncio.sleep(sleep_time)

                log("[PHASE] List scrape")

                list_data = await scrape_list_page(browser)

                for username, drops in list_data.items():
                    await process_alerts(username, drops, session)

                    sleep_time = random.uniform(1, 2)
                    log(f"[SLEEP] {sleep_time:.2f}s")
                    await asyncio.sleep(sleep_time)

                log("✅ Cycle complete")

            finally:
                log("[BROWSER] Closing")
                await browser.close()

    log("🏁 Script finished")


if __name__ == "__main__":
    asyncio.run(main())
