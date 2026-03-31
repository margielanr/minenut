import asyncio
import os
import random
import aiohttp
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from datetime import datetime, timezone
import re

WEBHOOK_ALERT     = "https://discord.com/api/webhooks/1481490669041090620/tAXROjdI4rjihfOXvkB5FWTcfORL8dEnVrjwNR1lgmTj1myDZQ88BXKCQgjaOAkm4-o1"
WEBHOOK_AVAILABLE = WEBHOOK_ALERT

CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "30"))
ALERT_TIMES     = [600, 300, 60]

USERNAMES_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usernames.txt")
NAMEMC_LIST_URL = "https://namemc.com/minecraft-names?sort=asc&length_op=eq&length=3&lang=&searches=0"
THREENAME_URL   = "https://3name.xyz/list"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

fired_alerts: dict[str, set] = {}
fired_available: set[str] = set()


async def send_webhook(session: aiohttp.ClientSession, webhook: str, content: str, allow_mentions: bool = False):
    if not webhook:
        return
    payload = {
        "content": content,
        "allowed_mentions": (
            {"roles": ["1481496559278358589"]} if allow_mentions else {"parse": []}
        )
    }
    for attempt in range(3):
        try:
            async with session.post(webhook, json=payload) as resp:
                if resp.status == 429:
                    retry = float(resp.headers.get("Retry-After", "5"))
                    print(f"[WEBHOOK] Rate limited, retrying in {retry}s")
                    await asyncio.sleep(retry)
                    continue
                elif resp.status >= 400:
                    print(f"[WEBHOOK ERROR {resp.status}] {await resp.text()}")
                return
        except Exception as e:
            print(f"[WEBHOOK EXCEPTION] {e}")
            await asyncio.sleep(2)


def parse_drop_times(text: str) -> list[datetime]:
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
        except ValueError:
            pass
    return results


async def get_body_text(page) -> str:
    try:
        return await asyncio.wait_for(page.inner_text("body"), timeout=10)
    except Exception:
        try:
            return await asyncio.wait_for(page.evaluate("document.body.innerText"), timeout=10)
        except Exception:
            return ""


async def new_page(browser):
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        locale="en-US",
        timezone_id="America/New_York",
        viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
    )
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    return context, page


async def scrape_namemc_list(browser) -> dict[str, list[datetime]]:
    context = None
    results = {}
    try:
        context, page = await new_page(browser)
        await page.goto(NAMEMC_LIST_URL, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        body_text = await get_body_text(page)
        if not body_text:
            print("[NAMEMC] Could not read page body")
            return {}

        body_lower = body_text.lower()
        if "security verification" in body_lower or "cloudflare" in body_lower:
            print("[NAMEMC] Cloudflare block — skipping this cycle")
            return {}

        time_suffixes = {'s', 'm', 'h', 'd'}
        rows = await page.query_selector_all("tr, .name-row, [class*='row']")
        if rows:
            for row in rows:
                try:
                    row_text = await asyncio.wait_for(row.inner_text(), timeout=5)
                    drop_times = parse_drop_times(row_text)
                    if not drop_times:
                        continue
                    first_token = row_text.strip().split()[0] if row_text.strip() else ""
                    if (len(first_token) == 3
                            and re.match(r'^[A-Za-z0-9_]{3}$', first_token)
                            and first_token[-1].lower() not in time_suffixes):
                        results[first_token] = drop_times
                except Exception:
                    pass

        if not results:
            for line in body_text.splitlines():
                line = line.strip()
                m = re.match(r'^([A-Za-z0-9_]{3})\s', line)
                if m:
                    username = m.group(1)
                    if username[-1].lower() not in time_suffixes:
                        drop_times = parse_drop_times(line)
                        if drop_times:
                            results[username] = drop_times

        print(f"[NAMEMC] Found {len(results)} 3-char name(s) with upcoming drops")
        return results

    except Exception as e:
        print(f"[NAMEMC LIST ERROR] {e}")
        return {}
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass


async def scrape_3name_list(browser, session: aiohttp.ClientSession) -> dict[str, list[datetime]]:
    context = None
    results = {}
    try:
        context, page = await new_page(browser)
        await page.goto(THREENAME_URL, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        body_text = await get_body_text(page)
        if not body_text:
            print("[3NAME] Could not read page body")
            return {}

        body_lower = body_text.lower()
        if "security verification" in body_lower or "cloudflare" in body_lower:
            print("[3NAME] Cloudflare block — skipping this cycle")
            return {}

        # Parse "Dropping" section — these are available RIGHT NOW
        dropping_now = []
        dropping_soon = []

        in_dropping = False
        in_dropping_soon = False

        for line in body_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "dropping soon" in line.lower():
                in_dropping = False
                in_dropping_soon = True
                continue
            if line.lower().startswith("dropping") and "soon" not in line.lower():
                in_dropping = True
                in_dropping_soon = False
                continue
            if line.lower() in ("stay updated", "donate", "home", "list", "search", "discord"):
                in_dropping = False
                in_dropping_soon = False
                continue

            name_match = re.match(r'^([A-Za-z0-9_]{3})$', line)
            if name_match:
                name = name_match.group(1)
                if in_dropping:
                    dropping_now.append(name)
                elif in_dropping_soon:
                    dropping_soon.append(name)

        print(f"[3NAME] Dropping now: {dropping_now}")
        print(f"[3NAME] Dropping soon ({len(dropping_soon)} names)")

        # Fire available alerts for names dropping right now
        for username in dropping_now:
            if username not in fired_available:
                fired_available.add(username)
                msg = f"🟢 **`{username}`** is **AVAILABLE NOW** on Minecraft! <@&1481496559278358589>\n🔗 https://namemc.com/profile/{username}\n⚡ Claim it: https://www.minecraft.net/en-us/msaprofile/mygames/editprofile"
                print(f"[AVAILABLE] {username}")
                await send_webhook(session, WEBHOOK_AVAILABLE, msg, allow_mentions=True)

        # For dropping soon — return them so caller can check NameMC for exact times
        for name in dropping_soon:
            results[name] = []

        return results

    except Exception as e:
        print(f"[3NAME LIST ERROR] {e}")
        return {}
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass


async def check_username(browser, username: str, session: aiohttp.ClientSession):
    context = None
    try:
        context, page = await new_page(browser)
        url = f"https://namemc.com/profile/{username}"
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        body_text = await get_body_text(page)
        if not body_text:
            print(f"[{username}] Could not read page body — skipping")
            return []

        body_lower = body_text.lower()

        if "security verification" in body_lower or "cloudflare" in body_lower:
            print(f"[{username}] Cloudflare block — skipping this cycle")
            return []

        # Tightly match NameMC's specific layout to avoid false positives
        # from profile history entries that may also contain "available"
        is_available = (
            "username is available" in body_lower
            or bool(re.search(r'status\s*\navailable\s*\nsearches', body_lower))
            or bool(re.search(r'minecraft name:.*\nstatus\navailable', body_lower))
        )
        if is_available:
            if username not in fired_available:
                fired_available.add(username)
                msg = f"🟢 **`{username}`** is **AVAILABLE NOW** on Minecraft! <@&1481496559278358589>\n🔗 https://namemc.com/profile/{username}\n⚡ Claim it: https://www.minecraft.net/en-us/msaprofile/mygames/editprofile"
                print(f"[AVAILABLE] {username}")
                await send_webhook(session, WEBHOOK_AVAILABLE or WEBHOOK_ALERT, msg, allow_mentions=True)
            return []

        drop_times = parse_drop_times(body_text)
        if drop_times:
            print(f"[{username}] Found {len(drop_times)} drop time(s): {[str(d) for d in drop_times]}")
        else:
            print(f"[{username}] No drop times found. Raw snippet: {body_text[:300]}")

        return drop_times

    except Exception as e:
        print(f"[ERROR] {username}: {e}")
        return []
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass


async def process_alerts(username: str, drop_times: list[datetime], session: aiohttp.ClientSession):
    if not drop_times:
        return

    now = datetime.now(timezone.utc)
    earliest = min(drop_times)
    seconds_left = (earliest - now).total_seconds()

    if username not in fired_alerts:
        fired_alerts[username] = set()

    for threshold in ALERT_TIMES:
        if threshold in fired_alerts[username]:
            continue
        if 0 < seconds_left <= threshold + CHECK_INTERVAL * 2:
            minutes = threshold // 60
            label = f"{minutes} minute{'s' if minutes != 1 else ''}"
            drop_str = earliest.strftime("%m/%d/%Y %I:%M:%S %p UTC")
            msg = (
                f"⏰ **`{username}`** drops in **{label}**! <@&1481496559278358589>\n"
                f"🕐 Drop time: `{drop_str}`\n"
                f"🔗 https://namemc.com/profile/{username}\n"
                f"⚡ Claim it: https://www.minecraft.net/en-us/msaprofile/mygames/editprofile"
            )
            print(f"[ALERT-{label}] {username} — {seconds_left:.0f}s left")
            await send_webhook(session, WEBHOOK_ALERT, msg, allow_mentions=True)
            fired_alerts[username].add(threshold)

    if seconds_left < -60:
        fired_alerts[username].clear()
        if username in fired_available:
            fired_available.discard(username)
        print(f"[{username}] Drop window passed, resetting alert state.")


async def main():
    manual_usernames = []
    if os.path.exists(USERNAMES_FILE):
        with open(USERNAMES_FILE, "r", encoding="utf-8") as f:
            manual_usernames = [line.strip() for line in f if line.strip()]
        print(f"Loaded {len(manual_usernames)} username(s) from usernames.txt")
    else:
        print("No usernames.txt found, only checking list pages")

    print(f"Checking every {CHECK_INTERVAL}s | Alerts at: 10min, 5min, 1min before drop")

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            try:
                while True:
                    # 1. Manual usernames.txt — highest priority
                    print(f"[MANUAL] Checking {len(manual_usernames)} username(s) from usernames.txt...")
                    for username in manual_usernames:
                        try:
                            drop_times = await check_username(browser, username, session)
                            await process_alerts(username, drop_times, session)
                        except Exception as e:
                            print(f"[ERROR] {username}: {e}")
                        await asyncio.sleep(random.uniform(4, 8))

                    # 2. NameMC list page
                    print("[NAMEMC] Scraping NameMC 3-char list page...")
                    try:
                        namemc_drops = await asyncio.wait_for(scrape_namemc_list(browser), timeout=60)
                    except asyncio.TimeoutError:
                        print("[NAMEMC] Timed out — skipping this cycle")
                        namemc_drops = {}

                    now = datetime.now(timezone.utc)
                    for username, drop_times in namemc_drops.items():
                        try:
                            earliest = min(drop_times)
                            seconds_left = (earliest - now).total_seconds()
                            hours_left = seconds_left / 3600
                            minutes_left = seconds_left / 60
                            time_str = f"{hours_left:.1f}h" if hours_left >= 1 else f"{minutes_left:.1f}m"
                            print(f"  [NAMEMC] [{username}] drops in {time_str}")
                            await process_alerts(username, drop_times, session)
                        except Exception as e:
                            print(f"[ERROR] {username}: {e}")
                        await asyncio.sleep(random.uniform(1, 2))

                    # 3. 3name.xyz list page
                    print("[3NAME] Scraping 3name.xyz list page...")
                    try:
                        threename_drops = await asyncio.wait_for(scrape_3name_list(browser, session), timeout=60)
                    except asyncio.TimeoutError:
                        print("[3NAME] Timed out — skipping this cycle")
                        threename_drops = {}

                    # For "dropping soon" names from 3name, look up exact times on NameMC
                    if threename_drops:
                        print(f"[3NAME] Looking up {len(threename_drops)} dropping-soon names on NameMC...")
                        for username in threename_drops:
                            if username in namemc_drops:
                                continue  # already handled above
                            try:
                                drop_times = await check_username(browser, username, session)
                                await process_alerts(username, drop_times, session)
                            except Exception as e:
                                print(f"[ERROR] {username}: {e}")
                            await asyncio.sleep(random.uniform(3, 6))

                    print(f"[OK] Cycle complete — next check in {CHECK_INTERVAL}s")
                    await asyncio.sleep(CHECK_INTERVAL)

            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[FATAL] {e}")
            finally:
                await browser.close()

    print("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
