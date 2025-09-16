"""Angel One minimal alert scaffold (test-mode)
- Fetches LTP, volume, and attempts option-chain summary for NIFTY50, SENSEX, RELIANCE, HDFCBANK.
- Sends a Telegram message with the data snapshot.

USAGE:
    python main.py --one-shot
    python main.py --loop
"""
import os, time, json, sqlite3, argparse
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
ANGEL_API_KEY = os.getenv('ANGEL_API_KEY')
ANGEL_CLIENT_CODE = os.getenv('ANGEL_CLIENT_CODE')
ANGEL_PASSWORD = os.getenv('ANGEL_PASSWORD')
ANGEL_TOTP_SECRET = os.getenv('ANGEL_TOTP_SECRET')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SYMBOLS = ['NIFTY', 'SENSEX', 'RELIANCE', 'HDFCBANK']
DB_FILE = 'alerts.db'

# Try import SmartAPI
try:
    # smartapi-python recommended import path
    from SmartApi import SmartConnect
except Exception as e:
    SmartConnect = None
    print('Warning: SmartApi library not available. Install smartapi-python.', e)

# Optional TOTP support
try:
    import pyotp
except Exception:
    pyotp = None

import requests

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

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print('Telegram not configured, skipping send.')
        return
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    resp = requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text})
    try:
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print('Telegram send failed', e, resp.text)
        return None

def login_smartapi():
    if SmartConnect is None:
        raise RuntimeError('SmartConnect library not installed. pip install smartapi-python')
    api = SmartConnect(api_key=ANGEL_API_KEY)
    # If user has TOTP secret, generate totp
    totp = None
    if ANGEL_TOTP_SECRET and pyotp:
        try:
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            print('Using generated TOTP')
        except Exception:
            totp = None
    # generateSession expects client code and password and totp (if required)
    try:
        data = api.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)
        # data contains access token and refresh token etc.
        print('Login OK:', data.get('status'))
        return api
    except Exception as e:
        print('Login failed:', e)
        raise

def fetch_ltp_and_volume(api, symbol):
    """Try to fetch LTP & volume. For indices like NIFTY/SENSEX the instrument token handling may differ.
    This function attempts a few common API calls; adapt per your SmartAPI app permissions.
    """
    out = {'symbol': symbol, 'ltp': None, 'volume': None, 'raw': None}
    try:
        # SmartAPI provides getLTP - try common wrapper name
        # Note: depending on SmartAPI version you may need to call api.ltp or api.get_ltp or api.getLTP
        if hasattr(api, 'ltp'):
            l = api.ltp(symbol)
            out['raw'] = l
        elif hasattr(api, 'get_ltp'):
            l = api.get_ltp(symbol)
            out['raw'] = l
        else:
            # fallback: try publisher/getquote endpoint via api.fetch_from_feed (not standardized)
            out['raw'] = {'error': 'no ltp method found on SmartConnect object; check version'}
        # Attempt to parse common fields
        if isinstance(out['raw'], dict):
            # try common keys
            for k in ('ltp','lastPrice','last_traded_price'):
                if k in out['raw']:
                    out['ltp'] = out['raw'][k]
                    break
        return out
    except Exception as e:
        out['raw'] = {'error': str(e)}
        return out

def fetch_option_chain(api, symbol):
    """Attempt to fetch option chain. Angel SmartAPI option-chain methods / availability vary.
    The community reports multiple approaches: using Market Feeds (publisher endpoints) or specialized option-chain endpoints.
    This helper attempts several calls; if none work, it returns a placeholder instructing user to adapt.
    """
    try:
        # Many community examples build option chain by calling:
        # api.get_option_chain or api.option_chain or via publisher endpoints.
        if hasattr(api, 'option_chain'):
            oc = api.option_chain(symbol)
            return {'ok': True, 'data': oc}
        if hasattr(api, 'get_option_chain'):
            oc = api.get_option_chain(symbol)
            return {'ok': True, 'data': oc}
        # If no direct method, attempt to use instrument master -> filter NFO tokens (NOT implemented here)
        return {'ok': False, 'error': 'No option chain method on SmartConnect instance. Please adapt fetch_option_chain() per your SmartAPI version.'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def single_cycle(api=None):
    init_db()
    if api is None:
        try:
            api = login_smartapi()
        except Exception as e:
            print('Could not login; aborting cycle.', e)
            return
    for sym in SYMBOLS:
        try:
            ltp = fetch_ltp_and_volume(api, sym)
            oc = fetch_option_chain(api, sym)
            payload = {'ltp': ltp, 'option_chain': oc}
            save_snapshot(sym, payload)
            text = f"Snapshot: {sym}\nLTP: {ltp.get('ltp')}\nOptionChain_ok: {oc.get('ok')}\nNote: Check logs for details."
            print(text)
            send_telegram(text)
        except Exception as e:
            print('Error for', sym, e)

def main(loop=False):
    if loop:
        while True:
            print('Running cycle at', datetime.now(timezone.utc).isoformat())
            try:
                single_cycle()
            except Exception as e:
                print('Cycle error', e)
            # sleep until next 30-min mark
            now = datetime.now()
            next_min = (now.minute // 30) * 30 + 30
            if next_min >= 60:
                next_run = now.replace(hour=(now.hour+1)%24, minute=0, second=5, microsecond=0)
            else:
                next_run = now.replace(minute=next_min, second=5, microsecond=0)
            sleep_seconds = (next_run - now).total_seconds()
            print(f"Sleeping {sleep_seconds}s until next run at {next_run.isoformat()}")
            time.sleep(max(10, sleep_seconds))
    else:
        single_cycle()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop', action='store_true', help='Run continuously')
    parser.add_argument('--one-shot', action='store_true', help='Run single cycle and exit')
    args = parser.parse_args()
    if args.loop:
        main(loop=True)
    else:
        main(loop=False)
