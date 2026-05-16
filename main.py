"""
Main — Entry point for the Kleinanzeigen autoresponder.

Starts the Telegram bot and launches per-account browser monitoring loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from browser_core import (
    AccountSession,
    ConversationState,
    connect_account,
    disconnect_account,
    get_unread_conversations,
    navigate_to_messages,
    open_conversation,
    random_pause,
    send_message,
)
from llm_handler import LLMReply, ask_llm
from telegram_bot import bot_state, create_dispatcher, notify_deal

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"
DATA_DIR = Path(__file__).parent / "data"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from libraries
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        logger.error(
            "config.json not found. "
            "Copy config.example.json and fill in your settings."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


async def monitor_account(
    account: AccountSession,
    config: dict[str, Any],
    http_session: aiohttp.ClientSession,
    bot: Bot,
) -> None:
    """
    Main monitoring loop for a single account.

    Periodically checks for new messages, processes them through the LLM,
    and either auto-replies or notifies the admin about deals.
    """
    admin_chat_id = config.get("admin_chat_id", 0)
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    ollama_model = config.get("ollama_model", "llama3.1:8b-instruct-q4_0")
    check_min = config.get("check_interval_min", 30)
    check_max = config.get("check_interval_max", 90)
    typing_min = config.get("typing_delay_min", 50)
    typing_max = config.get("typing_delay_max", 150)

    account.running = True
    logger.info("Monitoring started for account %s", account.account_id)

    try:
        while account.running:
            try:
                if account.page is None:
                    logger.warning(
                        "Account %s has no page, skipping cycle",
                        account.account_id,
                    )
                    await asyncio.sleep(30)
                    continue

                # Navigate to messages
                await navigate_to_messages(account.page)

                # Get unread conversations
                conversations = await get_unread_conversations(account.page)

                for conv in conversations:
                    conv_id = conv["conversation_id"]

                    # Skip paused conversations (waiting for admin input on a deal)
                    conv_is_paused = (
                        conv_id in account.conversations
                        and account.conversations[conv_id].paused
                    )
                    if conv_is_paused:
                        logger.debug("Skipping paused conversation %s", conv_id)
                        continue

                    # Open conversation and get details
                    conv_data = await open_conversation(account.page, conv["link"])
                    buyer_message = conv_data.get(
                        "buyer_message", conv.get("snippet", "")
                    )

                    if not buyer_message.strip():
                        continue

                    # Initialize conversation state if new
                    if conv_id not in account.conversations:
                        b_name = conv_data.get(
                            "buyer_name",
                            conv.get("buyer_name", "Unknown"),
                        )
                        a_title = conv_data.get(
                            "ad_title",
                            conv.get("ad_title", ""),
                        )
                        account.conversations[conv_id] = ConversationState(
                            conversation_id=conv_id,
                            buyer_name=b_name,
                            ad_title=a_title,
                            ad_price=conv_data.get("ad_price", ""),
                        )

                    conv_state = account.conversations[conv_id]

                    # Skip if we already processed this exact message
                    if buyer_message == conv_state.last_message:
                        continue

                    conv_state.last_message = buyer_message
                    conv_state.history.append(
                        {"role": "user", "content": buyer_message}
                    )

                    logger.info(
                        "Account %s | Conv %s | Buyer %s: %s",
                        account.account_id,
                        conv_id,
                        conv_state.buyer_name,
                        buyer_message[:100],
                    )

                    # Ask LLM for intent detection and response
                    try:
                        llm_reply: LLMReply = await ask_llm(
                            session=http_session,
                            ollama_url=ollama_url,
                            model=ollama_model,
                            ad_title=conv_state.ad_title,
                            ad_price=conv_state.ad_price,
                            buyer_message=buyer_message,
                            conversation_history=conv_state.history,
                        )
                    except Exception as exc:
                        logger.error("LLM error for conv %s: %s", conv_id, exc)
                        continue

                    if llm_reply.status == "reply" and llm_reply.text:
                        # Auto-reply
                        await random_pause(2.0, 5.0)
                        success = await send_message(
                            account.page,
                            llm_reply.text,
                            typing_delay_min=typing_min,
                            typing_delay_max=typing_max,
                        )
                        if success:
                            account.total_replies += 1
                            conv_state.messages_replied += 1
                            conv_state.history.append(
                                {"role": "assistant", "content": llm_reply.text}
                            )
                            logger.info(
                                "Auto-replied to %s in conv %s",
                                conv_state.buyer_name,
                                conv_id,
                            )

                            # Update persistent stats
                            accts = config.get("accounts", {})
                            acc_cfg = accts.get(account.account_id, {})
                            acc_cfg["total_replies"] = account.total_replies
                            config.setdefault("accounts", {})[
                                account.account_id
                            ] = acc_cfg
                            save_config(config)

                    elif llm_reply.status == "deal_reached":
                        # Pause conversation and notify admin
                        conv_state.paused = True
                        account.deals_pending += 1

                        # Update persistent stats
                        accts = config.get("accounts", {})
                        acc_cfg = accts.get(account.account_id, {})
                        acc_cfg["deals_pending"] = account.deals_pending
                        config.setdefault("accounts", {})[
                            account.account_id
                        ] = acc_cfg
                        save_config(config)

                        await notify_deal(
                            bot=bot,
                            admin_chat_id=admin_chat_id,
                            account_id=account.account_id,
                            conversation_id=conv_id,
                            buyer_name=conv_state.buyer_name,
                            ad_title=conv_state.ad_title,
                            buyer_message=buyer_message,
                            payment_guess=llm_reply.payment_method_guess or "unknown",
                        )
                        logger.info(
                            "Deal reached! Notified admin for conv %s (buyer: %s)",
                            conv_id,
                            conv_state.buyer_name,
                        )

                    # Random pause between processing conversations
                    await random_pause(3.0, 8.0)

            except Exception as exc:
                logger.error(
                    "Error in monitoring loop for %s: %s",
                    account.account_id,
                    exc,
                    exc_info=True,
                )
                await asyncio.sleep(30)

            # Random interval before next check cycle
            interval = random.randint(check_min, check_max)
            logger.debug(
                "Account %s sleeping for %d seconds",
                account.account_id,
                interval,
            )
            await asyncio.sleep(interval)

    except asyncio.CancelledError:
        logger.info("Monitoring cancelled for account %s", account.account_id)
    finally:
        account.running = False


async def start_account_monitoring(
    config: dict[str, Any],
    http_session: aiohttp.ClientSession,
    bot: Bot,
) -> list[asyncio.Task]:
    """Initialize and start monitoring for all configured accounts."""
    from playwright.async_api import async_playwright

    tasks: list[asyncio.Task] = []
    accounts_cfg = config.get("accounts", {})

    if not accounts_cfg:
        logger.info(
            "No accounts configured. "
            "Upload cookies via Telegram to add accounts."
        )
        return tasks

    vision_api_url = config.get("vision_api_url", "http://localhost:3000")
    pw = await async_playwright().start()

    for acc_id, acc_data in accounts_cfg.items():
        if not acc_data.get("active", True):
            logger.info("Skipping inactive account: %s", acc_id)
            continue

        account = AccountSession(
            account_id=acc_id,
            proxy=acc_data.get("proxy", ""),
            cookie_file=acc_data.get("cookie_file", ""),
            total_replies=acc_data.get("total_replies", 0),
            deals_pending=acc_data.get("deals_pending", 0),
        )

        try:
            await connect_account(http_session, vision_api_url, account, pw)
            bot_state.accounts[acc_id] = account
            task = asyncio.create_task(
                monitor_account(account, config, http_session, bot),
                name=f"monitor_{acc_id}",
            )
            tasks.append(task)
            logger.info("Account %s started successfully", acc_id)
        except Exception as exc:
            logger.error("Failed to start account %s: %s", acc_id, exc, exc_info=True)

    return tasks


async def watch_new_accounts(
    config: dict[str, Any],
    http_session: aiohttp.ClientSession,
    bot: Bot,
    tasks: list[asyncio.Task],
) -> None:
    """
    Periodically check for newly added accounts (via Telegram cookie upload)
    and start monitoring for them.
    """
    from playwright.async_api import async_playwright

    vision_api_url = config.get("vision_api_url", "http://localhost:3000")
    pw = await async_playwright().start()

    while True:
        await asyncio.sleep(10)

        # Re-read config to see if new accounts were added
        current_config = load_config()
        accounts_cfg = current_config.get("accounts", {})

        for acc_id, acc_data in accounts_cfg.items():
            if acc_id in bot_state.accounts:
                continue
            if not acc_data.get("active", True):
                continue

            logger.info("Detected new account: %s, starting monitoring...", acc_id)

            account = AccountSession(
                account_id=acc_id,
                proxy=acc_data.get("proxy", ""),
                cookie_file=acc_data.get("cookie_file", ""),
                total_replies=acc_data.get("total_replies", 0),
                deals_pending=acc_data.get("deals_pending", 0),
            )

            try:
                await connect_account(http_session, vision_api_url, account, pw)
                bot_state.accounts[acc_id] = account
                task = asyncio.create_task(
                    monitor_account(account, current_config, http_session, bot),
                    name=f"monitor_{acc_id}",
                )
                tasks.append(task)
                logger.info("New account %s started successfully", acc_id)
            except Exception as exc:
                logger.error("Failed to start new account %s: %s", acc_id, exc)

        # Sync config
        config.update(current_config)


async def main() -> None:
    setup_logging()

    config = load_config()
    bot_state.config = config

    token = config.get("telegram_bot_token", "")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.error("Set telegram_bot_token in config.json")
        sys.exit(1)

    admin_chat_id = config.get("admin_chat_id", 0)
    if not admin_chat_id:
        logger.error("Set admin_chat_id in config.json")
        sys.exit(1)

    # Create bot and dispatcher
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    bot_state.bot = bot

    dp = create_dispatcher()

    # HTTP session for Vision API and Ollama
    http_session = aiohttp.ClientSession()

    monitoring_tasks: list[asyncio.Task] = []

    try:
        # Start existing accounts
        monitoring_tasks = await start_account_monitoring(config, http_session, bot)

        # Background task: watch for newly added accounts
        asyncio.create_task(
            watch_new_accounts(config, http_session, bot, monitoring_tasks),
            name="account_watcher",
        )

        logger.info(
            "Bot started. %d account(s) monitoring. Waiting for Telegram commands...",
            len(monitoring_tasks),
        )

        # Start polling (this blocks until stopped)
        await dp.start_polling(bot)

    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        # Cancel all monitoring tasks
        for task in monitoring_tasks:
            task.cancel()
        if monitoring_tasks:
            await asyncio.gather(*monitoring_tasks, return_exceptions=True)

        # Disconnect all accounts
        vision_api_url = config.get("vision_api_url", "http://localhost:3000")
        for acc in bot_state.accounts.values():
            await disconnect_account(http_session, vision_api_url, acc)

        await http_session.close()
        await bot.session.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
