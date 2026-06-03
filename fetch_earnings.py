"""
fetch_earnings.py
-----------------
Fetches the NEXT (upcoming) earnings date for each scanned symbol and writes
them to earnings.json. Meant to run on a biweekly schedule, since confirmed
earnings dates rarely change. The main scanner reads this cache every scan and
flags any spread whose earnings land on/before its expiration.

Source: yfinance (free, no API key). ETFs/indexes (SPY, TQQQ, ...) have no
earnings and are simply recorded as null — they carry no earnings risk.

Resilience: if a symbol fails to fetch, its PREVIOUS value in earnings.json is
kept rather than wiped, so a flaky Yahoo response never erases good data.
"""
import json
import os
from datetime import datetime as dt, date

import yfinance as yf

# Keep this list in sync with SYMBOLS in scanner.py. We try to import it so
# there's a single source of truth; fall back to a hardcoded copy if import
# fails for any reason (e.g. scanner.py not importable in this environment).
try:
    from scanner import SYMBOLS
except Exception:
    SYMBOLS = ["NVDA", "AMZN", "MSFT", "META", "GOOG", "NFLX",
               "TSLA", "SPY", "AMD", "AAPL", "ORCL", "TQQQ"]

OUTPUT_FILE = "earnings.json"


def load_existing():
    if not os.path.exists(OUTPUT_FILE):
        return {}
    try:
        with open(OUTPUT_FILE) as f:
            return json.load(f).get("earnings", {})
    except Exception:
        return {}


def to_date(x):
    """Normalize a pandas Timestamp / datetime / date / 'YYYY-MM-DD' to a date."""
    if x is None:
        return None
    # pandas Timestamp subclasses datetime, so this covers it too
    if isinstance(x, dt):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return dt.strptime(str(x)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def next_earnings_date(symbol):
    """Return the soonest upcoming earnings date (>= today) as 'YYYY-MM-DD', or None."""
    today = date.today()
    tk = yf.Ticker(symbol)
    candidates = []

    # Primary: full earnings-date table (past + future rows)
    try:
        df = tk.get_earnings_dates(limit=16)
        if df is not None and not df.empty:
            for idx in df.index:
                d = to_date(idx)
                if d is not None:
                    candidates.append(d)
    except Exception as e:
        print(f"  [{symbol}] get_earnings_dates failed: {e}")

    # Fallback: the calendar dict, which usually carries the next date
    try:
        cal = tk.calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed is not None:
            if isinstance(ed, (list, tuple)):
                for e in ed:
                    d = to_date(e)
                    if d is not None:
                        candidates.append(d)
            else:
                d = to_date(ed)
                if d is not None:
                    candidates.append(d)
    except Exception as e:
        print(f"  [{symbol}] calendar fallback failed: {e}")

    future = sorted(d for d in candidates if d >= today)
    if future:
        return future[0].strftime("%Y-%m-%d")
    return None


def main():
    previous = load_existing()
    result = {}

    print(f"[EARNINGS] Fetching next earnings dates for {len(SYMBOLS)} symbols...")
    for sym in SYMBOLS:
        try:
            ed = next_earnings_date(sym)
        except Exception as e:
            ed = None
            print(f"  [{sym}] hard failure: {e}")

        if ed is None and sym in previous and previous[sym]:
            # Keep last known good value rather than wiping it on a flaky fetch.
            ed = previous[sym]
            print(f"  [{sym}] no fresh date -> keeping previous {ed}")
        else:
            print(f"  [{sym}] next earnings: {ed}")

        result[sym] = ed

    payload = {
        "updated": dt.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "earnings": result,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[EARNINGS] Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
