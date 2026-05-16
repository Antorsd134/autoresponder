# Kleinanzeigen Autoresponder

Multi-account autoresponder for Kleinanzeigen.de with Telegram bot control, local LLM intent detection (Ollama), and Vision anti-detect browser integration.

## Features

- **Multi-account support** — each account runs in its own isolated browser profile with a dedicated proxy
- **LLM-powered replies** — Llama 3.1 (via Ollama) analyzes buyer messages: auto-replies to questions/negotiations, notifies admin when a deal is reached
- **Telegram bot interface** — upload cookies, receive deal notifications with action buttons, view stats
- **Dynamic payment details** — PayPal/IBAN entered by admin on each deal (not stored in config)
- **Anti-detect browser** — integrates with Vision browser via CDP for fingerprint protection
- **Human-like behavior** — randomized typing delays, pauses between actions, cookie banner handling

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai/) with `llama3.1:8b-instruct-q4_0` model
- [Vision](https://visionbrowser.io/) anti-detect browser (running locally)
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

Copy the example config and fill in your values:

```bash
cp config.example.json config.json
```

Edit `config.json`:
- `telegram_bot_token` — your Telegram bot token
- `admin_chat_id` — your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))
- `ollama_url` — Ollama API URL (default: `http://localhost:11434`)
- `ollama_model` — model name (default: `llama3.1:8b-instruct-q4_0`)
- `vision_api_url` — Vision browser API URL (default: `http://localhost:3000`)

### 3. Add proxies

Create `data/proxies.txt` with one proxy per line:

```
ip:port:login:password
```

### 4. Pull the LLM model

```bash
ollama pull llama3.1:8b-instruct-q4_0
```

### 5. Run

```bash
python main.py
```

## Usage

### Adding accounts

Send a `.json` cookie file to the Telegram bot. The bot will:
1. Save cookies to `data/cookies/`
2. Assign the next free proxy from `data/proxies.txt`
3. Create a browser profile and start monitoring

### Deal notifications

When a buyer agrees to purchase, you'll receive a Telegram notification with buttons:
- **Отправить PayPal** — bot asks you for a PayPal email, then sends it to the buyer
- **Отправить Карту** — bot asks you for an IBAN, then sends it to the buyer
- **Ответить вручную** — enter custom text to forward to the buyer

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help`  | Usage guide |
| `/stats` | Account statistics |

## Project Structure

```
├── main.py              # Entry point, orchestration
├── telegram_bot.py      # Telegram bot handlers (aiogram 3.x)
├── browser_core.py      # Vision API + Playwright CDP + page parsing
├── llm_handler.py       # Ollama LLM integration + JSON parsing
├── config.example.json  # Config template
├── requirements.txt     # Python dependencies
└── data/
    ├── proxies.txt      # Proxy list (ip:port:login:pass)
    └── cookies/         # Cookie files (.json)
```

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Telegram    │────▶│   main.py    │────▶│  Vision API  │
│  Admin       │◀────│ (asyncio)    │◀────│  (profiles)  │
└─────────────┘     └──────┬───────┘     └──────┬───────┘
                           │                     │ CDP
                    ┌──────▼───────┐     ┌──────▼───────┐
                    │  Ollama LLM  │     │  Playwright   │
                    │ (local, 8B)  │     │  (per-acct)   │
                    └──────────────┘     └──────────────┘
```
