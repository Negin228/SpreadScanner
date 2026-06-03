import requests
import datetime
import json
import time
import os
import math
from datetime import timedelta, timezone

# ─────────────────────────────────────────────
# CONFIGURATION & HYBRID VELOCITY ENGINE
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
MIN_DTE            = 0
MAX_DTE            = 45
MIN_PROB_PROFIT    = 85.0
SLIPPAGE_ADJUST    = 0.02
MIN_BID_PRICE      = 0.05
MAX_BID_ASK_RATIO  = 2.2     # Stricter filter for cleaner fills
MIN_OI             = 50
MIN_VOLUME         = 50
MIN_DISCOUNT_PCT   = 0.20    # Rigid 20% Out-Of-The-Money cushion

OUTPUT_FILE = "signals.json"
EARNINGS_FILE = "earnings.json"   # written biweekly by fetch_earnings.py

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept": "application/json"
}

# ─────────────────────────────────────────────
# VELOCITY RANKING ENGINE
# ─────────────────────────────────────────────

def calculate_velocity_metrics(short_delta, net_credit, max_loss, price, spy_price, dte, short_ba_width, ticker_beta=1.2):
    # Baseline Institutional Expectancy
    p_loss = abs(short_delta)
    p_win = 1.0 - p_loss
    ev = (p_win * net_credit) - (p_loss * max_loss)
    pop = p_win * 100
    edge_ratio = ev / max_loss if max_loss > 0 else 0

    # Clean Execution Layer: Penalize wider bid/ask spreads
    # Spreads wider than $0.10 get exponentially penalized to guarantee clean fills
    liquidity_factor = 1.0
    if short_ba_width > 0.10:
        liquidity_factor = max(0.1, 1.0 - ((short_ba_width - 0.10) * 4))

    # High-Velocity Win Layer: Time Decay Optimization
    # We compress DTE using a square root function so shorter duration boosts the score.
    time_decay_multiplier = 1.0 / math.sqrt(dte + 1)

    # Combined Velocity Edge Score
    velocity_edge_score = edge_ratio * time_decay_multiplier * liquidity_factor

    # Institutional Position Sizing
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
        "edge_ratio": round(edge_ratio, 4),
        "velocity_edge_score": round(velocity_edge_score, 6)
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
# DATA & ARCHIVE EXECUTION
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

def load_earnings_map():
    """Load {symbol: 'YYYY-MM-DD' or None} from the biweekly earnings cache."""
    if not os.path.exists(EARNINGS_FILE):
        return {}
    try:
        with open(EARNINGS_FILE, "r") as f:
            return json.load(f).get("earnings", {})
    except Exception:
        return {}


def earnings_check(symbol, expiration, earnings_map):
    """
    Return (earnings_date_str_or_None, earnings_before_exp_bool).
    earnings_before_exp is True when the next earnings report lands on or before
    the spread's expiration (and hasn't already passed) — i.e. you'd be holding
    the short premium through the earnings event.
    """
    ed = earnings_map.get(symbol)
    if not ed:
        return None, False
    try:
        ed_date = datetime.datetime.strptime(ed[:10], "%Y-%m-%d").date()
        exp_date = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
        today = datetime.date.today()
    except Exception:
        return ed, False
    return ed, (today <= ed_date <= exp_date)


def load_existing_archive():
    if not os.path.exists(OUTPUT_FILE): return {}
    try:
        with open(OUTPUT_FILE, "r") as f:
            return json.load(f).get("spread_archive", {})
    except Exception: return {}

def run_workstation_scan():
    print(f"\n[SYSTEM] Initializing Custom Velocity Edge Scan...")
    
    archive = load_existing_archive()
    earnings_map = load_earnings_map()
    spy_data = fetch_data("markets/quotes", {"symbols": BENCHMARK, "greeks": "true"})
    if not spy_data or "quotes" not in spy_data: return

    spy_price = float(spy_data["quotes"]["quote"]["last"])
    spy_hist = get_historical_closes(BENCHMARK)

    all_signals = []
    scan_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for symbol in SYMBOLS:
        print(f"  > Processing Velocity Fills: {symbol}...", end="\r")

        quote_data = fetch_data("markets/quotes", {"symbols": symbol, "greeks": "true"})
        if not quote_data or not quote_data.get("quotes"): continue
        price = float(quote_data["quotes"]["quote"]["last"])
        hist = get_historical_closes(symbol)
        correlation = get_correlation(hist, spy_hist)

        exp_data = fetch_data("markets/options/expirations", {"symbol": symbol})
        if not exp_data: continue
        dates = exp_data.get("expirations", {}).get("date", [])
        if isinstance(dates, str): dates = [dates]

        for exp in dates:
            dte = (datetime.datetime.strptime(exp, "%Y-%m-%d").date() - datetime.date.today()).days
            if not (MIN_DTE <= dte <= MAX_DTE): continue

            chain = fetch_data("markets/options/chains", {"symbol": symbol, "expiration": exp, "greeks": "true"})
            options = chain.get("options", {}).get("option", []) if chain else []
            if isinstance(options, dict): options = [options]

            puts = [o for o in options if o["option_type"] == "put"]
            by_strike = {float(o["strike"]): o for o in puts}
            max_short_strike = price * (1 - MIN_DISCOUNT_PCT)

            for strike, short_opt in by_strike.items():
                if strike > max_short_strike: continue
                
                long_opt = by_strike.get(strike - SPREAD_WIDTH)
                if not long_opt: continue

                s_bid = float(short_opt.get("bid", 0) or 0)
                s_ask = float(short_opt.get("ask", 0) or 0)
                s_ba_width = s_ask - s_bid
                
                if s_bid < MIN_BID_PRICE: continue
                if s_bid > 0 and (s_ask / s_bid) > MAX_BID_ASK_RATIO: continue
                if int(short_opt.get("open_interest", 0) or 0) < MIN_OI: continue
                if int(short_opt.get("volume", 0) or 0) < MIN_VOLUME: continue

                l_bid = float(long_opt.get("bid", 0) or 0)
                l_ask = float(long_opt.get("ask", 0) or 0)
                net_credit = (((s_bid + s_ask)/2) - ((l_bid + l_ask)/2)) * (1 - SLIPPAGE_ADJUST)
                max_loss = SPREAD_WIDTH - net_credit

                if net_credit <= 0.03: continue

                greeks_dict = short_opt.get("greeks", {})
                if not greeks_dict or greeks_dict.get("delta") is None: continue
                delta = float(greeks_dict.get("delta"))

                metrics = calculate_velocity_metrics(
                    delta, net_credit, max_loss, price, spy_price, 
                    dte, s_ba_width, ticker_beta=BETA_MAPPING.get(symbol, 1.2)
                )

                if metrics["pop"] < MIN_PROB_PROFIT or metrics["ev"] <= 0: continue

                spread_label = f"{strike}/{strike-SPREAD_WIDTH}P"
                spread_key = f"{symbol}|{spread_label}|{exp}"

                earnings_date, earnings_before_exp = earnings_check(symbol, exp, earnings_map)

                signal = {
                    "symbol": symbol,
                    "expiration": exp,
                    "dte": dte,
                    "spread": spread_label,
                    "spread_key": spread_key,
                    "underlying_price": price,
                    "net_credit": round(net_credit, 2),
                    "max_loss": round(max_loss, 2),
                    "velocity_edge_score": metrics["velocity_edge_score"],
                    "edge_ratio": metrics["edge_ratio"],
                    "pop_pct": metrics["pop"],
                    "rec_qty": metrics["qty"],
                    "bid_ask_spread_width": round(s_ba_width, 2),
                    "earnings_date": earnings_date,
                    "earnings_before_exp": earnings_before_exp
                }

                all_signals.append(signal)

        time.sleep(0.3)

    # Rank exclusively by the brand new High-Velocity Win score
    all_signals.sort(key=lambda x: x["velocity_edge_score"], reverse=True)

    report = {
        "scan_time": scan_time,
        "sorting_method": "Velocity Edge Engine (Optimized for Fast Fills & Rapid Theta Decay)",
        "top_signals": all_signals[:20]
    }

    # Anti-blank guard: if this scan found ZERO signals (e.g. a pre-market or
    # after-hours run where option bids are 0 and every leg gets filtered out),
    # do NOT overwrite a perfectly good existing signals.json. Keep the last
    # session's data on the dashboard until a live scan produces real signals.
    if not all_signals:
        existing = []
        if os.path.exists(OUTPUT_FILE):
            try:
                with open(OUTPUT_FILE, "r") as f:
                    existing = json.load(f).get("top_signals", [])
            except Exception:
                existing = []
        if existing:
            print(f"\n[SKIP] Scan produced 0 signals (likely market closed). "
                  f"Keeping previous {len(existing)} signals in {OUTPUT_FILE} "
                  f"instead of blanking the dashboard.")
            return

    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=4)

    print(f"\n[SUCCESS] Custom Scan finalized. 20 target positions prioritized via Velocity Decay written to {OUTPUT_FILE}")

if __name__ == "__main__":
    run_workstation_scan()
