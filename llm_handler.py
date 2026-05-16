"""
LLM Handler — Ollama integration for intent detection and response generation.

Communicates with a local Ollama instance running Llama-3.1-8B-Instruct.
Returns structured JSON with either a reply or a deal_reached status.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

import aiohttp

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Du bist ein Verkäufer auf Kleinanzeigen. "
    "Analysiere die Nachricht des Käufers und antworte "
    "AUSSCHLIEẞLICH mit validem JSON — kein zusätzlicher Text.\n\n"
    "Regeln:\n"
    "1. Wenn der Käufer eine Frage stellt, verhandelt oder allgemein interessiert ist, "
    'antworte: {"status": "reply", "text": "<deine Antwort auf Deutsch>"}\n'
    "2. Wenn der Käufer klar zustimmt zu kaufen, "
    "bereit ist zu zahlen oder nach Zahlungsdaten fragt, "
    'antworte: {"status": "deal_reached", "payment_method_guess": "paypal"} '
    'oder {"status": "deal_reached", "payment_method_guess": "card"}\n\n'
    "Antworte IMMER auf Deutsch. Antworte IMMER nur mit validem JSON."
)


@dataclass(frozen=True)
class LLMReply:
    status: Literal["reply", "deal_reached"]
    text: str | None = None
    payment_method_guess: str | None = None


def _extract_json(raw: str) -> dict:
    """Extract a JSON object from possibly noisy LLM output."""
    raw = raw.strip()

    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try to find JSON within markdown code blocks
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find any JSON object in the text
    brace_match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    # Try nested braces (for cases with escaped quotes, etc.)
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    start = -1

    raise ValueError(f"Could not extract valid JSON from LLM response: {raw[:200]}")


def _validate_llm_response(data: dict) -> LLMReply:
    """Validate parsed JSON and return a typed LLMReply."""
    status = data.get("status", "")
    if status not in ("reply", "deal_reached"):
        raise ValueError(f"Invalid status from LLM: {status!r}")

    if status == "reply":
        text = data.get("text", "")
        if not text:
            raise ValueError("LLM returned reply status but no text")
        return LLMReply(status="reply", text=str(text))

    # deal_reached
    method = data.get("payment_method_guess", "unknown")
    return LLMReply(
        status="deal_reached",
        payment_method_guess=str(method),
    )


async def ask_llm(
    session: aiohttp.ClientSession,
    ollama_url: str,
    model: str,
    ad_title: str,
    ad_price: str,
    buyer_message: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> LLMReply:
    """
    Send buyer message to Ollama and parse the structured response.

    Parameters
    ----------
    session : aiohttp.ClientSession
        Reusable HTTP session.
    ollama_url : str
        Base URL of the Ollama API (e.g. http://localhost:11434).
    model : str
        Ollama model name (e.g. llama3.1:8b-instruct-q4_0).
    ad_title : str
        Title of the listing being discussed.
    ad_price : str
        Price of the listing.
    buyer_message : str
        The latest message from the buyer.
    conversation_history : list[dict] | None
        Previous messages in the conversation for context.

    Returns
    -------
    LLMReply
        Parsed and validated response.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add conversation history for context
    if conversation_history:
        for entry in conversation_history[-6:]:  # last 6 messages max
            messages.append(entry)

    user_content = (
        f"Anzeigentitel: {ad_title}\n"
        f"Preis: {ad_price}\n"
        f"Nachricht des Käufers: {buyer_message}"
    )
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.4,
            "num_predict": 300,
        },
    }

    url = f"{ollama_url.rstrip('/')}/api/chat"
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Ollama returned %d: %s", resp.status, body[:300])
                    raise aiohttp.ClientError(f"Ollama HTTP {resp.status}")

                result = await resp.json()
                raw_content = result.get("message", {}).get("content", "")
                logger.debug(
                    "LLM raw response (attempt %d): %s",
                    attempt,
                    raw_content[:500],
                )

                parsed = _extract_json(raw_content)
                return _validate_llm_response(parsed)

        except (ValueError, KeyError) as exc:
            logger.warning(
                "LLM parse error (attempt %d/%d): %s",
                attempt,
                max_retries,
                exc,
            )
            if attempt == max_retries:
                # Fallback: return a safe generic reply
                logger.error("All LLM attempts failed, returning fallback reply")
                return LLMReply(
                    status="reply",
                    text="Danke für Ihre Nachricht! Ich melde mich gleich bei Ihnen.",
                )
        except aiohttp.ClientError as exc:
            logger.warning(
                "Ollama connection error (attempt %d/%d): %s",
                attempt,
                max_retries,
                exc,
            )
            if attempt == max_retries:
                raise

    # Should not reach here, but just in case
    return LLMReply(
        status="reply",
        text="Danke für Ihre Nachricht! Ich melde mich gleich bei Ihnen.",
    )
