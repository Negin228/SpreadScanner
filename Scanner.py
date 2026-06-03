import requests
import datetime
import json
import time
import os
import math
from datetime import timedelta, timezone, datetime as dt

# ─────────────────────────────────────────────
# INSTITUTIONAL CONFIGURATION
# ─────────────────────────────────────────────

BASE_URL = "https://api.tradier.com/v1"
TRADIER_API_KEY = os.getenv("TRADIER_API_KEY", "").strip()

SYMBOLS = ["NVDA", "AMZN", "MSFT", "META", "GOOG", "NFLX", "TSLA", "SPY", "AMD", "AAPL", "ORCL", "TQQQ"]

BETA_MAPPING = {
    "TQQQ": 3.0, "SQQQ": -3.0, "SOXL": 3.0, "SOXS": -3.0,
    "UPRO": 3.0, "SPXU": -3.0, "SPY":  1.0, "NVDA": 1.7,
    "AAPL": 1.1, "AMZN": 1.1, "MSFT": 0.9, "META": 1.2,
    "GOOG": 1.1, "AMD":  1.6, "TSLA": 1.4, "NFLX": 1.2,
    "PLTR": 1.5, "ORCL": 1.0, "MSTR": 3.1
}

BENCHMARK = "SPY"
ACCOUNT_SIZE       = 100000
MAX_RISK_PER_TRADE = 0.02
SPREAD_WIDTH       = 5.0
MIN_DTE            = 7
MAX_DTE            = 45
MIN_PROB_PROFIT    = 85.0
SLIPPAGE_ADJUST    = 0.02
MIN_BID_PRICE      = 0.10
MAX_BID_ASK_RATIO  = 2.5
MIN_OI             = 200

OUTPUT_FILE = "signals.json"

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept": "application/json"
}

# ─────────────────────────────────────────────
# RISK & ANALYTICS ENGINE
# ─────────────────────────────────────────────

def calculate_institutional_metrics(short_delta, net_credit, max_loss, price, spy_price, ticker_beta=1.2):
    p_loss = abs(short_delta)
    p_win = 1.0 - p_loss
    ev = (p_win * net_credit) - (p_loss * max_loss)
    pop = p_win * 100

    dollar_risk_cap = ACCOUNT_SIZE * MAX_RISK_PER_TRADE
    risk_per_spread = max_loss * 100
    recommended_qty = math.floor(dollar_risk_cap / risk_per_spread) if risk_per_spread > 0 else 0

    pos_delta = short_delta * recommended_qty * 100
    weighted_delta = pos_delta * (price / spy_price) * ticker_beta

    return {
        "ev": round(ev, 2),
        "pop": round(pop, 1),
        "qty": recommended_qty,
        "spy_weighted_delta": round(weighted_delta, 2),
        "edge_ratio": round(ev / max_loss, 4) if max_loss > 0 else 0
    }

def get_correlation(hist1, hist2):
    if len(hist1) < 10 or len(hist2) < 10: return 0.0
    n = min(len(hist1), len(hist2))
    h1, h2 = hist1[-n:], hist2[-n:]
    mu1, mu2 = sum(h1)/n, sum(h2)/n
    num = sum((h1[i]-mu1)*(h2[i]-mu2) for i in range(n))
    den = math.sqrt(sum((x-mu1)**2 for x in h1) * sum((y-mu2)**2 for y in h2))
    return round(num/den, 3) if den != 0 else 0

# ─────────────────────────────────────────────
# API DATA WRAPPERS
# ─────────────────────────────────────────────

def fetch_data(endpoint, params=None):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"API Error on {endpoint}: {e}")
        return None

def get_historical_closes(symbol):
    data = fetch_data("markets/history", {
        "symbol": symbol, "interval": "daily",
        "start": (datetime.date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
    })
    history = data.get("history", {}).get("day", []) if data else []
    return [float(d["close"]) for d in history if "close" in d]

# ─────────────────────────────────────────────
# ARCHIVE HELPERS
# ─────────────────────────────────────────────

def make_spread_key(symbol, spread, expiration):
    """Unique identifier for a spread position."""
    return f"{symbol}|{spread}|{expiration}"

def load_existing_archive():
    """Load the spread_archive from the existing signals.json, if present."""
    if not os.path.exists(OUTPUT_FILE):
        return {}
    try:
        with open(OUTPUT_FILE, "r") as f:
            existing = json.load(f)
        archive = existing.get("spread_archive", {})
        return archive
    except Exception:
        return {}

def prune_expired_archive(archive):
    """Remove archive entries whose expiration date has already passed."""
    today = datetime.date.today()
    pruned = {}
    for key, entry in archive.items():
        try:
            exp_date = datetime.datetime.strptime(entry["expiration"], "%Y-%m-%d").date()
            if exp_date >= today:
                pruned[key] = entry
        except Exception:
            pruned[key] = entry  # Keep if we can't parse the date
    removed = len(archive) - len(pruned)
    if removed:
        print(f"  [ARCHIVE] Pruned {removed} expired spread(s).")
    return pruned

# ─────────────────────────────────────────────
# MAIN SCANNER EXECUTION
# ─────────────────────────────────────────────

def run_workstation_scan():
    print(f"\n[SYSTEM] Initializing $100k Institutional Scan...")

    # ── Load existing archive before scanning ──
    archive = load_existing_archive()
    archive = prune_expired_archive(archive)
    print(f"  [ARCHIVE] Loaded {len(archive)} tracked spread(s) from previous run(s).")

    spy_data = fetch_data("markets/quotes", {"symbols": BENCHMARK, "greeks": "true"})
    if not spy_data or "quotes" not in spy_data:
        print("Fatal: Could not fetch SPY data.")
        return

    spy_price = float(spy_data["quotes"]["quote"]["last"])
    spy_hist = get_historical_closes(BENCHMARK)

    all_signals = []
    # Convert to Pacific Time (PT): PDT=UTC-7 (Mar-Nov), PST=UTC-8
    _utc_now = datetime.datetime.now(timezone.utc)
    _year = _utc_now.year
    _dst_start = datetime.datetime(_year, 3, 8, 10, 0, tzinfo=timezone.utc)
    _dst_end   = datetime.datetime(_year, 11, 1, 9, 0, tzinfo=timezone.utc)
    while _dst_start.weekday() != 6: _dst_start += timedelta(days=1)
    while _dst_end.weekday()   != 6: _dst_end   += timedelta(days=1)
    _pt_offset = timedelta(hours=-7) if _dst_start <= _utc_now < _dst_end else timedelta(hours=-8)
    _tz_label  = "PDT" if _pt_offset.total_seconds() == -25200 else "PST"
    _pt_now    = _utc_now + _pt_offset
    scan_time  = _pt_now.strftime(f"%Y-%m-%d %H:%M:%S {_tz_label}")

    for symbol in SYMBOLS:
        print(f"  > Scanning {symbol}...", end="\r")

        quote_data = fetch_data("markets/quotes", {"symbols": symbol, "greeks": "true"})
        if not quote_data or not quote_data.get("quotes"): continue
        quote = quote_data["quotes"]["quote"]
        price = float(quote["last"])
        hist = get_historical_closes(symbol)
        correlation = get_correlation(hist, spy_hist)

        exp_data = fetch_data("markets/options/expirations", {"symbol": symbol})
        if not exp_data: continue
        dates = exp_data.get("expirations", {}).get("date", [])
        if isinstance(dates, str): dates = [dates]

        valid_dates = [d for d in dates if MIN_DTE <= (datetime.datetime.strptime(d, "%Y-%m-%d").date() - datetime.date.today()).days <= MAX_DTE]

        for exp in valid_dates:
            chain = fetch_data("markets/options/chains", {"symbol": symbol, "expiration": exp, "greeks": "true"})
            options = chain.get("options", {}).get("option", []) if chain else []
            if isinstance(options, dict): options = [options]

            puts = [o for o in options if o["option_type"] == "put"]
            by_strike = {float(o["strike"]): o for o in puts}

            for strike, short_opt in by_strike.items():
                long_opt = by_strike.get(strike - SPREAD_WIDTH)
                if not long_opt: continue

                s_bid = float(short_opt.get("bid", 0) or 0)
                s_ask = float(short_opt.get("ask", 0) or 0)
                if s_bid < MIN_BID_PRICE: continue
                if s_bid > 0 and (s_ask / s_bid) > MAX_BID_ASK_RATIO: continue
                if int(short_opt.get("open_interest", 0) or 0) < MIN_OI: continue

                l_bid = float(long_opt.get("bid", 0) or 0)
                l_ask = float(long_opt.get("ask", 0) or 0)
                mid_credit = ((s_bid + s_ask)/2) - ((l_bid + l_ask)/2)
                net_credit = mid_credit * (1 - SLIPPAGE_ADJUST)
                max_loss = SPREAD_WIDTH - net_credit

                if net_credit <= 0.05: continue

                greeks_dict = short_opt.get("greeks", {})
                if not greeks_dict or greeks_dict.get("delta") is None:
                    continue

                delta = float(greeks_dict.get("delta"))
                ticker_beta = BETA_MAPPING.get(symbol, 1.2)
                metrics = calculate_institutional_metrics(delta, net_credit, max_loss, price, spy_price, ticker_beta=ticker_beta)

                if metrics["pop"] < MIN_PROB_PROFIT or metrics["ev"] <= 0:
                    continue

                spread_label = f"{strike}/{strike-SPREAD_WIDTH}P"
                spread_key = make_spread_key(symbol, spread_label, exp)

                signal = {
                    "symbol": symbol,
                    "expiration": exp,
                    "spread": spread_label,
                    "spread_key": spread_key,
                    "price": price,
                    "net_credit": round(net_credit, 2),
                    "max_loss": round(max_loss, 2),
                    "ev": metrics["ev"],
                    "pop_pct": metrics["pop"],
                    "rec_qty": metrics["qty"],
                    "spy_delta_eq": metrics["spy_weighted_delta"],
                    "correlation_spy": correlation,
                    "edge_ratio": metrics["edge_ratio"],
                    "total_risk": round(metrics["qty"] * max_loss * 100, 2)
                }

                # ── Archive logic ──
                credit_snapshot = {"time": scan_time, "net_credit": round(net_credit, 2)}

                if spread_key in archive:
                    # Seen before: append credit snapshot, mark as returning
                    archive[spread_key]["credit_history"].append(credit_snapshot)
                    archive[spread_key]["last_seen"] = scan_time
                    archive[spread_key]["latest_signal"] = signal
                    signal["is_new"] = False
                else:
                    # First time seen: create archive entry
                    archive[spread_key] = {
                        "spread_key": spread_key,
                        "symbol": symbol,
                        "expiration": exp,
                        "spread": spread_label,
                        "first_seen": scan_time,
                        "last_seen": scan_time,
                        "credit_history": [credit_snapshot],
                        "latest_signal": signal
                    }
                    signal["is_new"] = True

                all_signals.append(signal)

        time.sleep(0.5)

    # Mark archive entries NOT in this scan (they stay in archive only)
    current_keys = {s["spread_key"] for s in all_signals}
    for key in archive:
        archive[key]["in_current_scan"] = key in current_keys

    all_signals.sort(key=lambda x: x["edge_ratio"], reverse=True)

    report = {
        "scan_time": scan_time,
        "account_basis": ACCOUNT_SIZE,
        "benchmark_spy": spy_price,
        "top_signals": all_signals[:20],
        "spread_archive": archive
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=4)

    new_count = sum(1 for s in all_signals if s.get("is_new"))
    returning_count = sum(1 for s in all_signals if not s.get("is_new"))
    archive_only = len(archive) - len(current_keys)
    print(f"\n[SUCCESS] Scan complete.")
    print(f"  ★ {new_count} new spread(s)  |  ↺ {returning_count} returning  |  📦 {archive_only} archived-only (not in current scan)")
    print(f"  Total archive size: {len(archive)} spread(s)  →  Written to {OUTPUT_FILE}")

if __name__ == "__main__":
    run_workstation_scan()
