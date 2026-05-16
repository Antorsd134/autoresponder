"""
Browser Core — Vision anti-detect browser integration and Kleinanzeigen page parsing.

Manages browser profiles via the Vision local API, connects Playwright over CDP,
and implements the monitoring loop for each account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

KLEINANZEIGEN_MESSAGES_URL = "https://www.kleinanzeigen.de/m-nachrichten.html"
KLEINANZEIGEN_BASE_URL = "https://www.kleinanzeigen.de"

DATA_DIR = Path(__file__).parent / "data"
COOKIES_DIR = DATA_DIR / "cookies"


@dataclass
class ConversationState:
    """Tracks the state of a single conversation."""

    conversation_id: str
    buyer_name: str
    ad_title: str
    ad_price: str
    last_message: str = ""
    history: list[dict[str, str]] = field(default_factory=list)
    paused: bool = False
    messages_replied: int = 0


@dataclass
class AccountSession:
    """Holds runtime state for a single account."""

    account_id: str
    proxy: str
    cookie_file: str
    profile_id: str | None = None
    ws_endpoint: str | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    conversations: dict[str, ConversationState] = field(default_factory=dict)
    total_replies: int = 0
    deals_pending: int = 0
    running: bool = False


def parse_proxy(proxy_line: str) -> dict[str, str]:
    """Parse 'ip:port:login:pass' into a proxy dict."""
    parts = proxy_line.strip().split(":")
    if len(parts) != 4:
        raise ValueError(
            f"Invalid proxy format (expected ip:port:login:pass): "
            f"{proxy_line!r}"
        )
    return {
        "server": f"http://{parts[0]}:{parts[1]}",
        "username": parts[2],
        "password": parts[3],
    }


def load_proxies(filepath: str | Path | None = None) -> list[dict[str, str]]:
    """Load proxies from the proxies.txt file."""
    path = Path(filepath) if filepath else DATA_DIR / "proxies.txt"
    if not path.exists():
        logger.warning("Proxies file not found: %s", path)
        return []
    proxies = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                proxies.append(parse_proxy(line))
            except ValueError as exc:
                logger.warning("Skipping bad proxy line: %s", exc)
    return proxies


async def create_vision_profile(
    session: aiohttp.ClientSession,
    vision_api_url: str,
    proxy: dict[str, str],
    profile_name: str,
) -> str:
    """
    Create a new browser profile in Vision and return the profile ID.

    Vision API (typical endpoints):
      POST /api/v1/profile/start  — start a profile
      POST /api/v1/profile/create — create a profile
    """
    create_url = f"{vision_api_url.rstrip('/')}/api/v1/profile/create"
    payload = {
        "name": profile_name,
        "proxy": {
            "type": "http",
            "host": proxy["server"].replace("http://", "").split(":")[0],
            "port": int(proxy["server"].replace("http://", "").split(":")[1]),
            "username": proxy["username"],
            "password": proxy["password"],
        },
        "os": "win",
        "browser": "chrome",
    }

    async with session.post(create_url, json=payload) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise RuntimeError(
                f"Vision profile creation failed ({resp.status}): "
                f"{body[:300]}"
            )
        data = await resp.json()
        profile_id = data.get("id") or data.get("profile_id") or data.get("uuid", "")
        if not profile_id:
            raise RuntimeError(f"Vision returned no profile ID: {data}")
        logger.info("Created Vision profile %s for %s", profile_id, profile_name)
        return str(profile_id)


async def start_vision_profile(
    session: aiohttp.ClientSession,
    vision_api_url: str,
    profile_id: str,
) -> str:
    """Start a Vision profile and return the CDP websocket debugger URL."""
    start_url = f"{vision_api_url.rstrip('/')}/api/v1/profile/start/{profile_id}"

    async with session.get(start_url) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                f"Vision profile start failed ({resp.status}): "
                f"{body[:300]}"
            )
        data = await resp.json()
        ws_url = data.get("wsEndpoint") or data.get("ws", {}).get("puppeteer", "")
        if not ws_url:
            # Try alternative field names
            ws_url = data.get("websocketDebuggerUrl", "")
        if not ws_url:
            raise RuntimeError(f"No websocket endpoint in Vision response: {data}")
        logger.info("Vision profile %s started, CDP: %s", profile_id, ws_url)
        return str(ws_url)


async def stop_vision_profile(
    session: aiohttp.ClientSession,
    vision_api_url: str,
    profile_id: str,
) -> None:
    """Stop a running Vision browser profile."""
    stop_url = f"{vision_api_url.rstrip('/')}/api/v1/profile/stop/{profile_id}"
    try:
        async with session.get(stop_url) as resp:
            logger.info(
                "Stopped Vision profile %s (status %d)",
                profile_id,
                resp.status,
            )
    except Exception as exc:
        logger.warning("Error stopping Vision profile %s: %s", profile_id, exc)


async def inject_cookies(page: Page, cookie_file: str) -> None:
    """Load cookies from a JSON file and inject them into the browser context."""
    path = COOKIES_DIR / cookie_file
    if not path.exists():
        logger.warning("Cookie file not found: %s", path)
        return

    with open(path, "r", encoding="utf-8") as f:
        cookies_raw = json.load(f)

    # Normalize cookie format for Playwright
    cookies = []
    for c in cookies_raw:
        cookie: dict[str, Any] = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ".kleinanzeigen.de"),
            "path": c.get("path", "/"),
        }
        if "expirationDate" in c:
            cookie["expires"] = float(c["expirationDate"])
        if "sameSite" in c:
            ss = c["sameSite"].capitalize()
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
        cookie["httpOnly"] = c.get("httpOnly", False)
        cookie["secure"] = c.get("secure", False)
        cookies.append(cookie)

    context = page.context
    await context.add_cookies(cookies)
    logger.info("Injected %d cookies from %s", len(cookies), cookie_file)


async def human_type(
    page: Page,
    selector: str,
    text: str,
    delay_min: int = 50,
    delay_max: int = 150,
) -> None:
    """Type text character by character with random delays to mimic human input."""
    await page.click(selector)
    await asyncio.sleep(random.uniform(0.2, 0.5))
    await page.type(selector, text, delay=random.randint(delay_min, delay_max))


async def random_pause(min_sec: float = 1.0, max_sec: float = 4.0) -> None:
    """Sleep for a random duration to mimic human behavior."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def navigate_to_messages(page: Page) -> None:
    """Navigate to the Kleinanzeigen messages page."""
    await page.goto(KLEINANZEIGEN_MESSAGES_URL, wait_until="domcontentloaded")
    await random_pause(2.0, 5.0)

    # Accept cookie banner if present
    try:
        accept_btn = page.locator(
            "#gdpr-banner-accept, [data-testid='gdpr-banner-accept']"
        )
        if await accept_btn.is_visible(timeout=3000):
            await accept_btn.click()
            await random_pause(1.0, 2.0)
    except Exception:
        pass


async def get_unread_conversations(page: Page) -> list[dict[str, str]]:
    """
    Parse the messages page and extract unread conversations.

    Returns a list of dicts with keys:
        - conversation_id: unique ID of the conversation
        - buyer_name: name of the buyer
        - ad_title: title of the ad
        - snippet: preview text of the last message
        - link: URL to the conversation
    """
    conversations = []

    try:
        # Wait for message list to load
        await page.wait_for_selector(
            ".MessageList, [class*='MessageList'], "
            "#messages, [data-testid='message-list']",
            timeout=10000,
        )
    except Exception:
        logger.warning("Message list container not found, page may not have loaded")
        return conversations

    # Try to find unread message items
    # Kleinanzeigen uses various class patterns; we try multiple selectors
    selectors = [
        ".MessageListItem--unread",
        "[class*='MessageListItem'][class*='unread']",
        ".message-list-item.is-unread",
        "li[class*='unread']",
    ]

    items = []
    for sel in selectors:
        items = await page.query_selector_all(sel)
        if items:
            break

    if not items:
        # Fallback: get all message items and check for unread indicators
        all_items = await page.query_selector_all(
            ".MessageListItem, [class*='MessageListItem'], .message-list-item"
        )
        for item in all_items:
            # Check for unread badge/indicator
            badge = await item.query_selector(
                ".badge, [class*='unread'], [class*='Badge'], .notification-dot"
            )
            if badge:
                items.append(item)

    for item in items:
        try:
            # Extract conversation link
            link_el = await item.query_selector("a[href*='/m-nachricht']")
            link = ""
            conv_id = ""
            if link_el:
                href = await link_el.get_attribute("href") or ""
                if href.startswith("/"):
                    link = f"{KLEINANZEIGEN_BASE_URL}{href}"
                else:
                    link = href
                # Extract conversation ID from URL
                parts = href.rstrip("/").split("/")
                conv_id = parts[-1] if parts else ""

            # Extract buyer name
            name_sel = (
                "[class*='username'], [class*='UserName'], "
                ".message-username, [data-testid='user-name']"
            )
            name_el = await item.query_selector(name_sel)
            buyer_name = (
                (await name_el.inner_text()).strip()
                if name_el
                else "Unknown"
            )

            # Extract ad title
            title_sel = (
                "[class*='adTitle'], [class*='AdTitle'], "
                ".message-ad-title, [data-testid='ad-title']"
            )
            title_el = await item.query_selector(title_sel)
            ad_title = (
                (await title_el.inner_text()).strip()
                if title_el
                else "Unknown Ad"
            )

            # Extract message snippet
            snip_sel = (
                "[class*='snippet'], [class*='preview'], "
                ".message-snippet, [data-testid='message-snippet']"
            )
            snippet_el = await item.query_selector(snip_sel)
            snippet = (
                (await snippet_el.inner_text()).strip()
                if snippet_el
                else ""
            )

            if conv_id:
                conversations.append(
                    {
                        "conversation_id": conv_id,
                        "buyer_name": buyer_name,
                        "ad_title": ad_title,
                        "snippet": snippet,
                        "link": link,
                    }
                )
        except Exception as exc:
            logger.warning("Error parsing message item: %s", exc)
            continue

    logger.info("Found %d unread conversations", len(conversations))
    return conversations


async def open_conversation(page: Page, conversation_url: str) -> dict[str, str]:
    """
    Open a conversation and extract the latest buyer message and ad details.

    Returns dict with keys: buyer_message, ad_title, ad_price, buyer_name.
    """
    await page.goto(conversation_url, wait_until="domcontentloaded")
    await random_pause(2.0, 4.0)

    result: dict[str, str] = {
        "buyer_message": "",
        "ad_title": "",
        "ad_price": "",
        "buyer_name": "",
    }

    # Extract ad title from conversation header
    for sel in [
        "[class*='ConversationAdTitle'], [data-testid='ad-title'], .ad-title",
        "h1, h2",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                result["ad_title"] = (await el.inner_text()).strip()
                break
        except Exception:
            continue

    # Extract ad price
    for sel in [
        "[class*='price'], [data-testid='price'], .ad-price",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                result["ad_price"] = (await el.inner_text()).strip()
                break
        except Exception:
            continue

    # Extract the latest message from the buyer (the other party)
    # Messages are typically in a list; the buyer's messages have a different class
    try:
        # Get all message bubbles
        all_messages = await page.query_selector_all(
            "[class*='MessageBubble'], [class*='message-bubble'], "
            ".message-content, [data-testid*='message']"
        )
        if all_messages:
            # The last message should be the buyer's (since we're checking unread)
            last_msg = all_messages[-1]
            result["buyer_message"] = (await last_msg.inner_text()).strip()
    except Exception as exc:
        logger.warning("Error extracting buyer message: %s", exc)

    # Extract buyer name from the conversation
    for sel in [
        "[class*='PartnerName'], [data-testid='partner-name'], .partner-name",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                result["buyer_name"] = (await el.inner_text()).strip()
                break
        except Exception:
            continue

    return result


async def send_message(
    page: Page,
    text: str,
    typing_delay_min: int = 50,
    typing_delay_max: int = 150,
) -> bool:
    """
    Type and send a message in the currently open conversation.

    Returns True if the message was sent successfully.
    """
    # Find the message input field
    input_selectors = [
        "textarea[name='message'], textarea[id*='message']",
        "[class*='MessageInput'] textarea",
        "[data-testid='message-input'] textarea",
        "textarea",
    ]

    input_el = None
    for sel in input_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                input_el = sel
                break
        except Exception:
            continue

    if not input_el:
        logger.error("Could not find message input field")
        return False

    await random_pause(0.5, 1.5)

    # Type the message with human-like delays
    await human_type(page, input_el, text, typing_delay_min, typing_delay_max)
    await random_pause(0.5, 2.0)

    # Find and click the send button
    send_selectors = [
        "button[type='submit']",
        "[class*='SendButton'], [data-testid='send-button']",
        "button[class*='send'], button[class*='Send']",
        "input[type='submit']",
    ]

    for sel in send_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                logger.info("Message sent successfully")
                await random_pause(1.0, 3.0)
                return True
        except Exception:
            continue

    # Fallback: try pressing Enter
    try:
        await page.keyboard.press("Enter")
        logger.info("Message sent via Enter key")
        await random_pause(1.0, 3.0)
        return True
    except Exception as exc:
        logger.error("Failed to send message: %s", exc)
        return False


async def connect_account(
    http_session: aiohttp.ClientSession,
    vision_api_url: str,
    account: AccountSession,
    pw: Any,
) -> None:
    """
    Set up a browser profile in Vision and connect Playwright via CDP.

    Populates account.browser, account.context, and account.page.
    """
    proxy = parse_proxy(account.proxy) if isinstance(account.proxy, str) else {}

    # Create or reuse Vision profile
    if not account.profile_id:
        account.profile_id = await create_vision_profile(
            http_session,
            vision_api_url,
            proxy,
            f"kleinanzeigen_{account.account_id}",
        )

    # Start the profile and get CDP endpoint
    account.ws_endpoint = await start_vision_profile(
        http_session, vision_api_url, account.profile_id
    )

    # Connect Playwright over CDP
    account.browser = await pw.chromium.connect_over_cdp(account.ws_endpoint)
    contexts = account.browser.contexts
    if contexts:
        account.context = contexts[0]
    else:
        account.context = await account.browser.new_context()

    pages = account.context.pages
    if pages:
        account.page = pages[0]
    else:
        account.page = await account.context.new_page()

    # Inject cookies
    await inject_cookies(account.page, account.cookie_file)
    logger.info("Account %s connected via CDP", account.account_id)


async def disconnect_account(
    http_session: aiohttp.ClientSession,
    vision_api_url: str,
    account: AccountSession,
) -> None:
    """Gracefully disconnect a browser profile."""
    try:
        if account.browser:
            await account.browser.close()
            account.browser = None
            account.context = None
            account.page = None
    except Exception as exc:
        logger.warning("Error closing browser for %s: %s", account.account_id, exc)

    if account.profile_id:
        await stop_vision_profile(http_session, vision_api_url, account.profile_id)
