#!/usr/bin/env python3
"""
Updated main.py - robust imports and clearer errors for Railway/Docker deployment.

Behavior:
- Tries to import SmartConnect from smartapi-python.
- If SmartConnect missing -> prints clear error and exits (fail-fast).
- Provides logzero-based logger if available, otherwise fallback to simple print wrapper.
- Keeps previous minimal logic: login, fetch LTP/option attempts, save snapshot, send Telegram.
- Use .env for secrets via python-dotenv.

Usage:
  python main.py --one-shot
  python main.py --loop
"""
import os
import sys
import time
import json
import sqlite3
import argparse
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

# Environment variables
ANGEL_API_KEY = os.getenv('ANGEL_API_KEY')
ANGEL_CLIENT_CODE = os.getenv('ANGEL_CLIENT_CODE')
ANGEL_PASSWORD = os.getenv('ANGEL_PASSWORD')
ANGEL_TOTP_SECRET = os.getenv('ANGEL_TOTP_SECRET')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SYMBOLS = ['NIFTY', 'SENSEX', 'RELIANCE', 'HDFCBANK']
DB_FILE = os.getenv('DB_FILE', 'alerts.db')

# --------- Robust imports and logger ----------
SMARTAPI_AVAILABLE = False
try:
    from SmartApi import SmartConnect
    SMARTAPI_AVAILABLE = True
except Exception as e:
    SmartConnect = None
    SMARTAPI_AVAILABLE = False
    # we'll log this below

# logzero preferred for nice logging; fallback to simple logger
try:
    from logzero import logger
except Exception:
    class SimpleLogger:
        @staticmethod
        def info(*args, **kwargs):
            print("[INFO]", *args)

        @staticmethod
        def warning(*args, **kwargs):
            print("[WARN]", *args)

        @staticmethod
        def error(*args, **kwargs):
            print("[ERROR]", *args)

    logger = SimpleLogger()

import requests

# --------- DB helpers ----------
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        ts TEXT,
        payload TEXT
    )''')
    con.commit()
    con.close()

def save_snapshot(symbol, payload):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute('INSERT INTO snapshots(symbol,ts,payload) VALUES(?,?,?)',
                (symbol, datetime.now(timezone.utc).isoformat(), json.dumps(payload)))
    con.commit()
    con.close()

# --------- Telegram helper ----------
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing). Skipping send.")
        return None
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    try:
        resp = requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=15)
        resp.raise_for_status()
        logger.info("Telegram message sent")
        return resp.json()
    except Exception as e:
        logger.error("Telegram send failed:", e, getattr(e, 'response', None))
        return None

# --------- SmartAPI login & fetch helpers ----------
def login_smartapi() -> 'SmartConnect':
    """
    Try to login to Angel One SmartAPI and return SmartConnect instance.
    If SmartAPI package missing or login fails, raise RuntimeError with helpful message.
    """
    if not SMARTAPI_AVAILABLE:
        raise RuntimeError(
            "SmartConnect (smartapi-python) not installed in the container. "
            "Add `smartapi-python` to requirements.txt and rebuild the container. "
            "Example: pip install smartapi-python OR pip install git+https://github.com/AngelBroking/SmartAPI-Python.git"
        )

    if not ANGEL_API_KEY or not ANGEL_CLIENT_CODE or not ANGEL_PASSWORD:
        raise RuntimeError("ANGEL_API_KEY, ANGEL_CLIENT_CODE or ANGEL_PASSWORD missing in environment.")

    api = SmartConnect(api_key=ANGEL_API_KEY)
    # generate TOTP if available
    totp = None
    if ANGEL_TOTP_SECRET:
        try:
            import pyotp
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            logger.info("Generated TOTP from ANGEL_TOTP_SECRET")
        except Exception as e:
            logger.warning("pyotp not available or TOTP generation failed:", e)
            totp = None

    try:
        # generateSession is common in SmartAPI libs, but method names may vary across versions.
        # We attempt common patterns and bubble up any exception details.
        if hasattr(api, "generateSession"):
            resp = api.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)
        elif hasattr(api, "login"):
            resp = api.login(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)
        else:
            raise RuntimeError("SmartConnect instance does not have generateSession/login methods. Check smartapi-python version.")
        logger.info("SmartAPI login response status (partial): %s", str(resp)[:200])
        return api
    except Exception as e:
        raise RuntimeError(f"SmartAPI login failed: {e}")

def fetch_ltp_and_volume(api, symbol: str) -> dict:
    """
    Attempt to fetch LTP and volume using common SmartAPI methods.
    Return a dictionary with keys ltp, volume, raw (for debugging).
    """
    out = {'symbol': symbol, 'ltp': None, 'volume': None, 'raw': None}
    try:
        # Try a few common method names - real method may vary
        if hasattr(api, 'ltp'):
            raw = api.ltp(symbol)
            out['raw'] = raw
        elif hasattr(api, 'get_ltp'):
            raw = api.get_ltp(symbol)
            out['raw'] = raw
        elif hasattr(api, 'getLTP'):
            raw = api.getLTP(symbol)
            out['raw'] = raw
        else:
            out['raw'] = {'error': 'No known ltp method on SmartConnect. Inspect SmartAPI client.'}
            logger.warning("No known ltp method on SmartConnect object.")
            return out

        # Try to extract common fields from returned structure
        if isinstance(out['raw'], dict):
            for k in ('ltp', 'lastPrice', 'last_traded_price', 'LTP'):
                if k in out['raw']:
                    out['ltp'] = out['raw'][k]
                    break
            # volume field heuristics
            for k in ('volume', 'totalTradedVolume', 'tradedQuantity'):
                if k in out['raw']:
                    out['volume'] = out['raw'][k]
                    break
        return out
    except Exception as e:
        out['raw'] = {'error': str(e)}
        logger.error("fetch_ltp_and_volume error for %s: %s", symbol, e)
        return out

def fetch_option_chain(api, symbol: str) -> dict:
    """
    Best-effort attempt to pull option chain / option summary.
    SmartAPI option endpoints differ; this function tries common names and otherwise returns guidance.
    """
    try:
        if hasattr(api, 'option_chain'):
            oc = api.option_chain(symbol)
            return {'ok': True, 'data': oc}
        if hasattr(api, 'get_option_chain'):
            oc = api.get_option_chain(symbol)
            return {'ok': True, 'data': oc}
        # If not available, return informative error to user
        return {'ok': False, 'error': 'No option chain method found on SmartConnect. Adapt fetch_option_chain for your SmartAPI version.'}
    except Exception as e:
        logger.error("fetch_option_chain error for %s: %s", symbol, e)
        return {'ok': False, 'error': str(e)}

# --------- Orchestration ----------
def single_cycle(api: Optional['SmartConnect'] = None):
    init_db()
    if api is None:
        # Try login; surface clear errors
        try:
            api = login_smartapi()
        except Exception as e:
            logger.error("Could not login to SmartAPI: %s", e)
            return

    for sym in SYMBOLS:
        try:
            ltp = fetch_ltp_and_volume(api, sym)
            oc = fetch_option_chain(api, sym)
            payload = {'ltp': ltp, 'option_chain': oc}
            save_snapshot(sym, payload)
            text = (
                f"Snapshot: {sym}\\n"
                f"LTP: {ltp.get('ltp')}\\n"
                f"Volume: {ltp.get('volume')}\\n"
                f"OptionChain_ok: {oc.get('ok')}\\n"
                f"Note: Check logs for details."
            )
            logger.info("Prepared snapshot for %s", sym)
            send_telegram(text)
            # polite short sleep between symbols to avoid rate-limits
            time.sleep(0.4)
        except Exception as e:
            logger.error("Error processing symbol %s: %s", sym, e)

def run_loop():
    while True:
        logger.info("Starting scan cycle at %s", datetime.now(timezone.utc).isoformat())
        try:
            single_cycle()
        except Exception as e:
            logger.error("Cycle error: %s", e)
        # compute time until next 30-min boundary
        now = datetime.now()
        next_min = (now.minute // 30) * 30 + 30
        if next_min >= 60:
            next_run = now.replace(hour=(now.hour+1) % 24, minute=0, second=5, microsecond=0)
        else:
            next_run = now.replace(minute=next_min, second=5, microsecond=0)
        sleep_seconds = (next_run - now).total_seconds()
        logger.info("Sleeping %.0f seconds until %s", sleep_seconds, next_run.isoformat())
        time.sleep(max(10, sleep_seconds))

# --------- Entrypoint ----------
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop', action='store_true', help='Run continuously every 30 minutes')
    parser.add_argument('--one-shot', action='store_true', help='Run single cycle and exit')
    args = parser.parse_args()

    # Pre-check some required env vars for early feedback
    missing = []
    if not ANGEL_API_KEY:
        missing.append('ANGEL_API_KEY')
    if not ANGEL_CLIENT_CODE:
        missing.append('ANGEL_CLIENT_CODE')
    if not ANGEL_PASSWORD:
        missing.append('ANGEL_PASSWORD')
    if missing:
        logger.error("Missing environment variables: %s. Fill them before running.", ", ".join(missing))
        logger.error("If you don't want SmartAPI functionality, stop here and provide mock or skip.")
        sys.exit(2)

    # Inform about SmartAPI availability
    if not SMARTAPI_AVAILABLE:
        logger.error("SmartAPI Python client missing. Please ensure 'smartapi-python' is in requirements.txt and redeploy.")
        logger.error("Example install: pip install smartapi-python OR pip install git+https://github.com/AngelBroking/SmartAPI-Python.git")
        sys.exit(3)

    if args.loop:
        run_loop()
    else:
        single_cycle()
