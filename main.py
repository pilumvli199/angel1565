#!/usr/bin/env python3
"""
main.py - Debuggable Angel One SmartAPI scaffold

Features:
- Robust import guards for smartapi-python and logzero
- Detailed debug dumps: login response, available methods, raw API responses
- Instrument-master probing (helpful to map symbol -> instrument token)
- Safe-call wrapper that logs/truncates raw responses
- Saves snapshots to local SQLite DB for post-mortem
- Telegram alerts with diagnostic payloads
- CLI flags: --one-shot, --loop, --mock, --debug

Usage:
  python main.py --one-shot
  python main.py --loop
  python main.py --mock --one-shot   # run without SmartAPI (mock data)
  python main.py --debug --one-shot  # verbose logging
"""

import os
import sys
import time
import json
import sqlite3
import argparse
import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

# load .env
from dotenv import load_dotenv
load_dotenv()

# --- ENV / CONFIG ---
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_FILE = os.getenv("DB_FILE", "alerts.db")
SYMBOLS = ["NIFTY", "SENSEX", "RELIANCE", "HDFCBANK"]

# --- Robust imports ---
SMARTAPI_AVAILABLE = False
try:
    from SmartApi import SmartConnect  # smartapi-python
    SMARTAPI_AVAILABLE = True
except Exception as e:
    SmartConnect = None
    SMARTAPI_AVAILABLE = False
    _smartapi_import_err = e

try:
    from logzero import logger
    # logzero uses basic config; if you want file logging, configure externally
except Exception:
    class _SimpleLogger:
        @staticmethod
        def _fmt(level: str, *args):
            t = datetime.now(timezone.utc).isoformat()
            print(f"{t} [{level}]", *args)
        @staticmethod
        def info(*args): _SimpleLogger._fmt("INFO", *args)
        @staticmethod
        def warning(*args): _SimpleLogger._fmt("WARN", *args)
        @staticmethod
        def error(*args): _SimpleLogger._fmt("ERROR", *args)
    logger = _SimpleLogger()

import requests

# Try pyotp if available
try:
    import pyotp
except Exception:
    pyotp = None

# --- DB helpers ---
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        ts TEXT,
        payload TEXT
    )
    """)
    con.commit()
    con.close()

def save_snapshot(symbol: str, payload: Dict[str, Any]):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT INTO snapshots(symbol,ts,payload) VALUES(?,?,?)",
                (symbol, datetime.now(timezone.utc).isoformat(), json.dumps(payload)))
    con.commit()
    con.close()

# --- Safe call / logging wrapper ---
def truncate(obj: Any, length: int = 1500) -> str:
    s = ""
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > length:
        return s[:length] + "...(truncated)"
    return s

def safe_call_and_log(fn: Callable, *args, debug_name: str = None, **kwargs) -> Any:
    """
    Calls fn with provided args/kwargs and logs the raw response (truncated).
    Returns the result or an error dict on exception.
    """
    name = debug_name or getattr(fn, "__name__", str(fn))
    try:
        res = fn(*args, **kwargs)
        logger.info("API call %s success; type=%s; raw=%s", name, type(res).__name__, truncate(res, 1200))
        return res
    except Exception as e:
        logger.error("API call %s raised: %s", name, e)
        return {"error": str(e)}

# --- Debug helpers for SmartAPI object ---
def list_api_methods(api: Any):
    try:
        methods = [m for m in dir(api) if callable(getattr(api, m)) and not m.startswith("_")]
        logger.info("SmartAPI callable methods (sample upto 80 chars): %s", ", ".join(sorted(methods)[:40]))
    except Exception as e:
        logger.error("list_api_methods failed: %s", e)

def debug_print_api(api: Any):
    """
    Print useful attributes and probe some common getters (best-effort).
    """
    try:
        logger.info("=== SmartAPI debug dump START ===")
        # try to print access token-like attributes
        for attr in ("access_token", "token", "refresh_token", "session", "last_response"):
            if hasattr(api, attr):
                try:
                    val = getattr(api, attr)
                    logger.info("api.%s = %s", attr, truncate(val, 800))
                except Exception:
                    logger.info("api.%s present but unreadable", attr)
        list_api_methods(api)

        # Probe common profile/account methods
        for probe in ("getProfile", "get_profile", "profile", "getAccount", "getUserProfile"):
            if hasattr(api, probe):
                try:
                    res = safe_call_and_log(getattr(api, probe), debug_name=probe)
                    logger.info("Probe %s -> %s", probe, truncate(res, 800))
                except Exception as e:
                    logger.warning("Probe %s error: %s", probe, e)
        logger.info("=== SmartAPI debug dump END ===")
    except Exception as e:
        logger.error("debug_print_api failed overall: %s", e)

# --- SmartAPI login & fetch helpers ---
def login_smartapi() -> Any:
    if not SMARTAPI_AVAILABLE:
        raise RuntimeError(f"SmartAPI client not installed: {_smartapi_import_err}. Install smartapi-python.")
    if not ANGEL_API_KEY or not ANGEL_CLIENT_CODE or not ANGEL_PASSWORD:
        raise RuntimeError("ANGEL_API_KEY, ANGEL_CLIENT_CODE or ANGEL_PASSWORD missing in environment.")

    api = SmartConnect(api_key=ANGEL_API_KEY)
    totp = None
    if ANGEL_TOTP_SECRET and pyotp:
        try:
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            logger.info("Generated TOTP for login")
        except Exception as e:
            logger.warning("TOTP generation failed: %s", e)
            totp = None

    # Try common login methods; capture and log returned structure
    if hasattr(api, "generateSession"):
        fn = api.generateSession
        name = "generateSession"
    elif hasattr(api, "login"):
        fn = api.login
        name = "login"
    else:
        raise RuntimeError("SmartConnect instance does not expose generateSession/login. Inspect available methods.")

    logger.info("Attempting SmartAPI login via %s (client_code=%s)", name, ANGEL_CLIENT_CODE)
    resp = safe_call_and_log(fn, ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp, debug_name=name)
    # resp may be dict with status/access_token; log full truncated
    logger.info("Login response (truncated): %s", truncate(resp, 1200))
    # Provide debug dump of api object methods/attrs
    debug_print_api(api)
    return api

def fetch_ltp_by_method(api: Any, symbol: str) -> Dict[str, Any]:
    """
    Attempt to call a variety of LTP methods on api. Return a dict {method: result}.
    This helps discover correct method name and data structure.
    """
    results = {}
    tried = []
    # candidate method names commonly present
    candidates = ["ltp", "get_ltp", "getLTP", "ltpData", "getLTPData", "getLtp", "getLTPFeed"]
    for m in candidates:
        if hasattr(api, m):
            tried.append(m)
            fn = getattr(api, m)
            res = safe_call_and_log(fn, symbol, debug_name=m)
            results[m] = res
    # Also try generic "fetch" style: some libs have api.get_quote / api.get_quote_data
    for m in ["get_quote", "get_quote_data", "quote", "getQuote", "quoteFeed"]:
        if hasattr(api, m):
            tried.append(m)
            res = safe_call_and_log(getattr(api, m), symbol, debug_name=m)
            results[m] = res
    if not tried:
        logger.warning("No known LTP-style methods found on SmartAPI instance (checked %s)", candidates[:5])
    return results

def fetch_option_chain_probes(api: Any, symbol: str) -> Dict[str, Any]:
    """
    Probe for option-chain related methods. Return mapping method->result
    """
    results = {}
    probes = ["option_chain", "get_option_chain", "getOptionChain", "get_opt_chain", "get_option_chain_for_symbol"]
    for p in probes:
        if hasattr(api, p):
            results[p] = safe_call_and_log(getattr(api, p), symbol, debug_name=p)
    # attempt to find instruments or instrument master which helps building option chain manually
    for probe in ("instruments", "instrument_master", "getInstrumentMaster", "get_instruments", "getInstruments"):
        if hasattr(api, probe):
            results[probe] = safe_call_and_log(getattr(api, probe), debug_name=probe)
    return results

# --- Fallback/mocking helpers ---
def mock_snapshot(symbol: str) -> Dict[str, Any]:
    """Return deterministic-ish mock payload for testing Telegram and DB flows."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "symbol": symbol,
        "ts": now,
        "ltp": round(1000 + hash(symbol) % 1000 + (time.time() % 10), 2),
        "volume": int(1000 + (hash(symbol) % 500)),
        "option_chain_ok": False,
        "note": "mocked"
    }

# --- Telegram helper ---
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Skipping Telegram send.")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        resp.raise_for_status()
        logger.info("Telegram sent OK.")
        return resp.json()
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return None

# --- Orchestrator ---
def single_cycle(mock_mode: bool = False):
    init_db()
    api = None
    if not mock_mode:
        try:
            api = login_smartapi()
        except Exception as e:
            logger.error("Could not login; aborting cycle. %s", e)
            # continue but send diagnostics for each symbol if mock_mode is False but login failed
            api = None

    for sym in SYMBOLS:
        try:
            if mock_mode or api is None:
                payload = mock_snapshot(sym)
                payload['debug'] = "mock_mode" if mock_mode else "login_failed"
                logger.info("Mock payload for %s -> %s", sym, truncate(payload, 800))
            else:
                # Try many LTP methods and record all raw responses to help debugging
                ltp_results = fetch_ltp_by_method(api, sym)
                oc_results = fetch_option_chain_probes(api, sym)
                # pick probable LTP: choose first method that returns numeric 'ltp' or known key
                chosen_ltp = None
                chosen_entry = None
                for method, res in ltp_results.items():
                    # res may be dict or list or other; try to extract common keys
                    if isinstance(res, dict):
                        for key in ("ltp", "lastPrice", "LTP", "ltpValue", "last_traded_price"):
                            if key in res and res.get(key):
                                chosen_ltp = res.get(key)
                                chosen_entry = (method, key)
                                break
                    # if res is list with dicts, inspect first
                    if chosen_ltp is not None:
                        break
                # fallback: try to interpret some common nested structures
                if chosen_ltp is None:
                    # inspect each response's JSON for numbers in string form - naive fallback
                    for method, res in ltp_results.items():
                        s = truncate(res, 500)
                        # try to find digits pattern (very naive)
                        import re
                        m = re.search(r"\\b(\\d{1,7}(?:\\.\\d{1,4})?)\\b", s)
                        if m:
                            chosen_ltp = float(m.group(1))
                            chosen_entry = (method, "regex-extracted")
                            break

                # assemble payload
                payload = {
                    "symbol": sym,
                    "ltp_candidates": ltp_results,
                    "option_chain_probes": oc_results,
                    "chosen_ltp": chosen_ltp,
                    "chosen_entry": chosen_entry,
                    "ts": datetime.now(timezone.utc).isoformat()
                }

            # save to DB and send to Telegram (compact)
            save_snapshot(sym, payload)
            # Compose diagnostic telegram message small + instruction
            msg = f"Snapshot: {sym}\\n"
            if mock_mode:
                msg += f"MOCK LTP: {payload.get('ltp')} (mock)\\n"
                msg += "Note: mock_mode ON\\n"
            else:
                msg += f"Chosen LTP: {payload.get('chosen_ltp')}\\n"
                # include top-level diagnostics - indicate if option_chain probes found anything ok
                oc_ok = any((isinstance(v, dict) and v.get("ok") is True) for v in payload.get("option_chain_probes", {}).values())
                msg += f"OptionChain_ok: {oc_ok}\\n"
                msg += "See logs/DB for full raw responses.\\n"
            msg += f"Time: {payload.get('ts')}"
            logger.info("Prepared message for %s: %s", sym, truncate(msg, 500))
            send_telegram(msg)
            # polite sleep to avoid burst/ratelimit
            time.sleep(0.4)
        except Exception as e:
            logger.error("Error in cycle for %s: %s", sym, e)

def run_loop(mock_mode: bool = False):
    while True:
        logger.info("Starting cycle at %s", datetime.now(timezone.utc).isoformat())
        single_cycle(mock_mode=mock_mode)
        # sleep until next 30-min boundary
        now = datetime.now()
        next_min = (now.minute // 30) * 30 + 30
        if next_min >= 60:
            next_run = now.replace(hour=(now.hour + 1) % 24, minute=0, second=5, microsecond=0)
        else:
            next_run = now.replace(minute=next_min, second=5, microsecond=0)
        sleep_seconds = (next_run - now).total_seconds()
        logger.info("Sleeping %.0f seconds until %s", sleep_seconds, next_run.isoformat())
        time.sleep(max(10, sleep_seconds))

# --- CLI Entrypoint ---
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true", help="Run continuously every 30 minutes")
    p.add_argument("--one-shot", action="store_true", help="Run single cycle and exit")
    p.add_argument("--mock", action="store_true", help="Run in mock mode (no SmartAPI required)")
    p.add_argument("--debug", action="store_true", help="Extra debug verbosity (keeps same logger but prints startup info)")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()

    # startup info
    if args.debug:
        logger.info("STARTUP: debug mode ON")
    logger.info("Env check: ANGEL_API_KEY present=%s, TELEGRAM present=%s", bool(ANGEL_API_KEY), bool(TELEGRAM_BOT_TOKEN))

    # if not mocking and SmartAPI import failed, show error and advice
    if not args.mock and not SMARTAPI_AVAILABLE:
        logger.error("SmartAPI client not installed in runtime: %s", getattr(_smartapi_import_err, "args", _smartapi_import_err))
        logger.error("Install by adding 'smartapi-python' to requirements.txt or use git+https://github.com/AngelBroking/SmartAPI-Python.git")
        # still allow running with --mock, otherwise exit
        logger.error("To run without SmartAPI, re-run with --mock flag to use mock data.")
        sys.exit(3)

    # Run
    if args.one_shot or args.one_shot is True or args.one_shot is None:
        # Accept both --one-shot or default behavior if none provided; prefer explicit
        pass

    try:
        if args.loop:
            run_loop(mock_mode=args.mock)
        else:
            single_cycle(mock_mode=args.mock)
    except KeyboardInterrupt:
        logger.info("Interrupted by user, exiting.")
    except Exception as e:
        logger.error("Fatal error in main: %s", e)
        raise
