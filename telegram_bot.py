"""
Telegram Bot — Admin interface for managing the Kleinanzeigen autoresponder.

Features:
  - Cookie file upload (.json) → auto-assigns proxy, creates account profile
  - Deal notifications with inline keyboards (PayPal / Card / Manual reply)
  - /stats command for overview of all accounts
  - /start and /help commands
  - Dynamic PayPal/IBAN input from admin on each deal
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

if TYPE_CHECKING:
    from browser_core import AccountSession

logger = logging.getLogger(__name__)

router = Router()

DATA_DIR = Path(__file__).parent / "data"
COOKIES_DIR = DATA_DIR / "cookies"
CONFIG_PATH = Path(__file__).parent / "config.json"


# ---------------------------------------------------------------------------
# FSM states for dynamic input flows
# ---------------------------------------------------------------------------

class DealReplyStates(StatesGroup):
    """States for the deal reply flow."""

    waiting_paypal = State()
    waiting_card = State()
    waiting_manual = State()


# ---------------------------------------------------------------------------
# Shared state — populated by main.py at startup
# ---------------------------------------------------------------------------

class BotState:
    """Mutable shared state between the bot handlers and the monitoring loop."""

    accounts: dict[str, AccountSession] = {}
    pending_deals: dict[str, dict[str, Any]] = {}
    # pending_deals key format: "{account_id}:{conversation_id}"
    # value: {"account_id": ..., "conversation_id": ..., "buyer_name": ...,
    #         "ad_title": ..., "message": ..., "page": ...}
    config: dict[str, Any] = {}
    bot: Bot | None = None


bot_state = BotState()


def _load_config() -> dict[str, Any]:
    """Load config.json or return defaults."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "telegram_bot_token": "",
        "admin_chat_id": 0,
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b-instruct-q4_0",
        "vision_api_url": "http://localhost:3000",
        "check_interval_min": 30,
        "check_interval_max": 90,
        "typing_delay_min": 50,
        "typing_delay_max": 150,
        "accounts": {},
    }


def _save_config(config: dict[str, Any]) -> None:
    """Persist config to disk."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _get_free_proxy(config: dict[str, Any]) -> str | None:
    """Return the first proxy from proxies.txt not yet assigned to an account."""
    proxies_path = DATA_DIR / "proxies.txt"
    if not proxies_path.exists():
        return None

    used_proxies: set[str] = set()
    for acc in config.get("accounts", {}).values():
        used_proxies.add(acc.get("proxy", ""))

    for line in proxies_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in used_proxies:
            return line

    return None


# ---------------------------------------------------------------------------
# /start, /help
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    admin_id = bot_state.config.get("admin_chat_id", 0)
    if admin_id and message.chat.id != admin_id:
        await message.answer("⛔ Доступ запрещён.")
        return

    await message.answer(
        "🤖 <b>Kleinanzeigen Autoresponder</b>\n\n"
        "Команды:\n"
        "/stats — статистика аккаунтов\n"
        "/help — справка\n\n"
        "Для добавления аккаунта отправьте файл cookies (.json).",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    admin_id = bot_state.config.get("admin_chat_id", 0)
    if admin_id and message.chat.id != admin_id:
        return

    await message.answer(
        "📘 <b>Справка</b>\n\n"
        "<b>Добавление аккаунта:</b>\n"
        "Отправьте .json файл с куки Kleinanzeigen. "
        "Бот автоматически привяжет свободный прокси и запустит мониторинг.\n\n"
        "<b>Уведомления о сделках:</b>\n"
        "Когда покупатель готов купить, вы получите уведомление с кнопками:\n"
        "• [Отправить PayPal] — бот запросит email PayPal и отправит покупателю\n"
        "• [Отправить Карту] — бот запросит IBAN и отправит покупателю\n"
        "• [Ответить вручную] — введите текст, бот перешлёт покупателю\n\n"
        "<b>Статистика:</b>\n"
        "/stats — обзор всех аккаунтов и счётчиков",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    admin_id = bot_state.config.get("admin_chat_id", 0)
    if admin_id and message.chat.id != admin_id:
        return

    accounts = bot_state.accounts
    if not accounts:
        await message.answer("📊 Нет активных аккаунтов.")
        return

    total_replies = sum(a.total_replies for a in accounts.values())
    deals_pending = sum(a.deals_pending for a in accounts.values())
    active_count = sum(1 for a in accounts.values() if a.running)

    lines = [
        "📊 <b>Общая статистика:</b>",
        f"Активных аккаунтов: {active_count}",
        f"Отвечено сообщений: {total_replies}",
        f"Ожидают реквизитов: {deals_pending}",
        "---",
    ]

    for acc_id, acc in accounts.items():
        proxy_display = acc.proxy.split(":")[0] if acc.proxy else "N/A"
        status = "✅" if acc.running else "⏸"
        lines.append(
            f"{status} <b>{acc_id}</b>: {acc.total_replies} ответов "
            f"(Прокси: {proxy_display})"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Cookie file upload
# ---------------------------------------------------------------------------

@router.message(F.document)
async def handle_cookie_upload(message: Message) -> None:
    admin_id = bot_state.config.get("admin_chat_id", 0)
    if admin_id and message.chat.id != admin_id:
        return

    doc: Document | None = message.document
    if doc is None:
        return

    filename = doc.file_name or "unknown"
    if not filename.endswith(".json"):
        await message.answer("⚠️ Пожалуйста, отправьте файл в формате .json (куки).")
        return

    # Download file
    if bot_state.bot is None:
        await message.answer("❌ Бот не инициализирован.")
        return

    file = await bot_state.bot.get_file(doc.file_id)
    if file.file_path is None:
        await message.answer("❌ Не удалось получить файл.")
        return

    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = COOKIES_DIR / filename

    await bot_state.bot.download_file(file.file_path, dest_path)

    # Validate JSON
    try:
        with open(dest_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if not isinstance(cookies, list):
            raise ValueError("Cookie file must contain a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        dest_path.unlink(missing_ok=True)
        await message.answer(f"❌ Невалидный JSON: {exc}")
        return

    # Assign a free proxy
    config = _load_config()
    proxy = _get_free_proxy(config)
    if not proxy:
        await message.answer(
            "⚠️ Нет свободных прокси в data/proxies.txt. "
            "Добавьте прокси и попробуйте снова."
        )
        return

    # Create account entry
    account_id = filename.replace(".json", "")
    # Avoid duplicates
    counter = 1
    original_id = account_id
    while account_id in config.get("accounts", {}):
        account_id = f"{original_id}_{counter}"
        counter += 1

    config.setdefault("accounts", {})[account_id] = {
        "proxy": proxy,
        "cookie_file": filename,
        "total_replies": 0,
        "deals_pending": 0,
        "active": True,
    }
    _save_config(config)
    bot_state.config = config

    proxy_display = proxy.split(":")[0]
    await message.answer(
        f"✅ Аккаунт <b>{account_id}</b> добавлен!\n"
        f"Куки: {filename}\n"
        f"Прокси: {proxy_display}\n\n"
        "Мониторинг будет запущен автоматически.",
        parse_mode="HTML",
    )

    logger.info(
        "New account added: %s (proxy: %s, cookies: %s)",
        account_id,
        proxy,
        filename,
    )


# ---------------------------------------------------------------------------
# Deal notifications & inline keyboard handling
# ---------------------------------------------------------------------------

async def notify_deal(
    bot: Bot,
    admin_chat_id: int,
    account_id: str,
    conversation_id: str,
    buyer_name: str,
    ad_title: str,
    buyer_message: str,
    payment_guess: str,
) -> None:
    """Send a deal notification to the admin with inline action buttons."""
    deal_key = f"{account_id}:{conversation_id}"
    bot_state.pending_deals[deal_key] = {
        "account_id": account_id,
        "conversation_id": conversation_id,
        "buyer_name": buyer_name,
        "ad_title": ad_title,
        "message": buyer_message,
        "payment_guess": payment_guess,
    }

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Отправить PayPal",
                    callback_data=f"deal_paypal:{deal_key}",
                ),
                InlineKeyboardButton(
                    text="🏦 Отправить Карту",
                    callback_data=f"deal_card:{deal_key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Ответить вручную",
                    callback_data=f"deal_manual:{deal_key}",
                ),
            ],
        ]
    )

    text = (
        f"🔥 <b>Клиент готов купить!</b>\n\n"
        f"Аккаунт: <b>{account_id}</b>\n"
        f"Покупатель: <b>{buyer_name}</b>\n"
        f"Объявление: {ad_title}\n"
        f"Сообщение: <i>{buyer_message}</i>\n"
        f"Предполагаемый способ оплаты: {payment_guess}"
    )

    await bot.send_message(
        admin_chat_id, text, parse_mode="HTML", reply_markup=keyboard
    )
    logger.info("Deal notification sent for %s", deal_key)


@router.callback_query(F.data.startswith("deal_paypal:"))
async def handle_deal_paypal(callback: CallbackQuery, state: FSMContext) -> None:
    deal_key = callback.data.replace("deal_paypal:", "", 1)  # type: ignore[union-attr]
    if deal_key not in bot_state.pending_deals:
        await callback.answer("Сделка не найдена или уже обработана.", show_alert=True)
        return

    await state.update_data(deal_key=deal_key)
    await state.set_state(DealReplyStates.waiting_paypal)
    await callback.message.answer(  # type: ignore[union-attr]
        f"💳 Введите <b>PayPal email</b> для отправки покупателю "
        f"(сделка: {deal_key.split(':')[0]}):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("deal_card:"))
async def handle_deal_card(callback: CallbackQuery, state: FSMContext) -> None:
    deal_key = callback.data.replace("deal_card:", "", 1)  # type: ignore[union-attr]
    if deal_key not in bot_state.pending_deals:
        await callback.answer("Сделка не найдена или уже обработана.", show_alert=True)
        return

    await state.update_data(deal_key=deal_key)
    await state.set_state(DealReplyStates.waiting_card)
    await callback.message.answer(  # type: ignore[union-attr]
        f"🏦 Введите <b>IBAN</b> для отправки покупателю "
        f"(сделка: {deal_key.split(':')[0]}):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("deal_manual:"))
async def handle_deal_manual(callback: CallbackQuery, state: FSMContext) -> None:
    deal_key = callback.data.replace("deal_manual:", "", 1)  # type: ignore[union-attr]
    if deal_key not in bot_state.pending_deals:
        await callback.answer("Сделка не найдена или уже обработана.", show_alert=True)
        return

    await state.update_data(deal_key=deal_key)
    await state.set_state(DealReplyStates.waiting_manual)
    await callback.message.answer(  # type: ignore[union-attr]
        f"✍️ Введите текст для отправки покупателю "
        f"(сделка: {deal_key.split(':')[0]}):",
        parse_mode="HTML",
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM handlers — receive admin input and forward to buyer
# ---------------------------------------------------------------------------

@router.message(DealReplyStates.waiting_paypal)
async def receive_paypal(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    deal_key = data.get("deal_key", "")
    deal = bot_state.pending_deals.get(deal_key)

    if not deal:
        await message.answer("❌ Сделка не найдена.")
        await state.clear()
        return

    paypal_email = message.text or ""
    if not paypal_email.strip():
        await message.answer("⚠️ Пожалуйста, введите корректный PayPal email.")
        return

    reply_text = (
        f"Vielen Dank für Ihr Interesse! "
        f"Sie können die Zahlung bequem per PayPal senden an: {paypal_email.strip()}"
    )

    success = await _send_deal_reply(deal, reply_text)
    if success:
        await message.answer(
            f"PayPal ({paypal_email.strip()}) отправлен покупателю."
        )
        _close_deal(deal_key)
    else:
        await message.answer("❌ Не удалось отправить сообщение. Попробуйте ещё раз.")

    await state.clear()


@router.message(DealReplyStates.waiting_card)
async def receive_card(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    deal_key = data.get("deal_key", "")
    deal = bot_state.pending_deals.get(deal_key)

    if not deal:
        await message.answer("❌ Сделка не найдена.")
        await state.clear()
        return

    iban = message.text or ""
    if not iban.strip():
        await message.answer("⚠️ Пожалуйста, введите корректный IBAN.")
        return

    reply_text = (
        f"Vielen Dank! Hier sind meine Bankdaten für die Überweisung:\n"
        f"IBAN: {iban.strip()}"
    )

    success = await _send_deal_reply(deal, reply_text)
    if success:
        await message.answer("IBAN отправлен покупателю.")
        _close_deal(deal_key)
    else:
        await message.answer("❌ Не удалось отправить сообщение. Попробуйте ещё раз.")

    await state.clear()


@router.message(DealReplyStates.waiting_manual)
async def receive_manual(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    deal_key = data.get("deal_key", "")
    deal = bot_state.pending_deals.get(deal_key)

    if not deal:
        await message.answer("❌ Сделка не найдена.")
        await state.clear()
        return

    manual_text = message.text or ""
    if not manual_text.strip():
        await message.answer("⚠️ Пожалуйста, введите текст сообщения.")
        return

    success = await _send_deal_reply(deal, manual_text.strip())
    if success:
        await message.answer("✅ Сообщение отправлено покупателю.")
        _close_deal(deal_key)
    else:
        await message.answer("❌ Не удалось отправить сообщение. Попробуйте ещё раз.")

    await state.clear()


async def _send_deal_reply(deal: dict[str, Any], text: str) -> bool:
    """Send the reply text to the buyer via the browser."""
    from browser_core import send_message

    account_id = deal["account_id"]
    account = bot_state.accounts.get(account_id)

    if not account or not account.page:
        logger.error("Account %s has no active page for sending message", account_id)
        return False

    try:
        page = account.page
        conversation_id = deal["conversation_id"]
        conv_url = f"https://www.kleinanzeigen.de/m-nachricht-lesen.html#{conversation_id}"
        await page.goto(conv_url, wait_until="domcontentloaded")

        import asyncio
        import random
        await asyncio.sleep(random.uniform(2.0, 4.0))

        cfg = bot_state.config
        return await send_message(
            page,
            text,
            typing_delay_min=cfg.get("typing_delay_min", 50),
            typing_delay_max=cfg.get("typing_delay_max", 150),
        )
    except Exception as exc:
        logger.error("Error sending deal reply for %s: %s", account_id, exc)
        return False


def _close_deal(deal_key: str) -> None:
    """Remove a pending deal and update stats."""
    deal = bot_state.pending_deals.pop(deal_key, None)
    if deal:
        account_id = deal["account_id"]
        account = bot_state.accounts.get(account_id)
        if account:
            account.deals_pending = max(0, account.deals_pending - 1)

        # Unpause the conversation
        conv_id = deal["conversation_id"]
        if account and conv_id in account.conversations:
            account.conversations[conv_id].paused = False

        logger.info("Deal %s closed", deal_key)


# ---------------------------------------------------------------------------
# Dispatcher setup
# ---------------------------------------------------------------------------

def create_dispatcher() -> Dispatcher:
    """Create and configure the aiogram Dispatcher."""
    dp = Dispatcher()
    dp.include_router(router)
    return dp
