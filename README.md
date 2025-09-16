# Angel One - Indian Market Alert Bot (Test mode)

**What this package contains**
- A minimal, ready-to-edit scaffold to fetch live data (option-chain / LTP / volume) for NIFTY50, SENSEX, RELIANCE, HDFCBANK using Angel One SmartAPI (SmartConnect).
- Sends compact alerts to Telegram (test mode).
- NOTE: This is a test scaffold. You must provide your Angel One credentials/API key and Telegram bot token in `.env`. See instructions below.

**Important**
- Angel One SmartAPI requires you to generate an API key and use TOTP (2FA) for login in many cases. See Angel One SmartAPI docs: https://smartapi.angelbroking.com
- Option chain availability can vary by account/app permissions. The code tries common SmartAPI calls; if your account/app has different endpoints you may need to adapt the small helper `fetch_option_chain()` in `main.py`.

## Setup (quick)
1. Create a Python 3.10+ venv and activate it:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` -> `.env` and fill values:
   - ANGEL_API_KEY: SmartAPI app key
   - ANGEL_CLIENT_CODE: your Angel client code (e.g., R12345)
   - ANGEL_PASSWORD: your trading PIN / password (used only for login)
   - ANGEL_TOTP_SECRET: (optional) TOTP secret from Angel One enable-totp page (if set up). If left blank, the script will try to prompt/exit.
   - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

3. Run a single scan (dry-run):
   ```bash
   python main.py --one-shot
   ```

4. To run continuously (every 30 minutes):
   ```bash
   python main.py --loop
   ```

## What the script does
- Logs into Angel One SmartAPI (using smartapi-python)
- Attempts to fetch option-chain or option-related summaries (depends on API permissions)
- Fetches LTP and volume for symbols
- Logs outputs to local SQLite DB `alerts.db`
- Sends a compact message to Telegram chat (if configured)

## Caveats & next steps
- You must enable Market Feeds or appropriate app permissions in Angel One developer portal to access option chain.
- If you see JSON parse errors or permission errors, consult Angel One docs and adapt endpoints in `main.py`.
- This repo is intentionally minimal and safe (alerts only). No order placement is implemented.
