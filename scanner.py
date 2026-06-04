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

# ── DTE band: short enough for brisk decay, but with a floor so a losing trade
#    can be held a day or two WITHOUT being forced into expiration (assignment).
MIN_DTE            = 5
MAX_DTE            = 14
DTE_BAND_CENTER    = 9       # ranking peaks here, tapers toward the band edges

# ── Assignment-risk controls ──
MAX_SHORT_DELTA    = 0.10    # short put must be far OTM (~<=10% chance ITM); primary guard
EXPECTED_MOVE_MULT = 1.0     # short strike must sit >= 1 expected move below price
MIN_DISCOUNT_PCT   = 0.20    # price floor under the short strike; delta does the heavy lifting

# ── Exit-cost / liquidity controls (you pay the bid/ask twice on a fast trade) ──
MAX_SHORT_BA_WIDTH = 0.10    # HARD filter on absolute short-leg bid/ask width
MAX_BID_ASK_RATIO  = 2.2     # relative width guard (catches cheap, wide options)

# ── Trend filter: skip selling puts into a CONFIRMED downtrend ──
USE_TREND_FILTER   = True
TREND_FAST_MA      = 20
TREND_SLOW_MA      = 50

MIN_PROB_PROFIT    = 85.0
SLIPPAGE_ADJUST    = 0.02
MIN_BID_PRICE      = 0.05
MIN_OI             = 50
MIN_VOLUME         = 50

OUTPUT_FILE = "signals.json"
EARNINGS_FILE = "earnings.json"   # written biweekly by fetch_earnings.py

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept": "application/json"
}

# ─────────────────────────────────────────────
# VELOCITY RANKING ENGINE
# ─────────────────────────────────────────────

def calculate_velocity_metrics(short_delta, short_theta, net_credit, max_loss, price, spy_price, dte, short_ba_width, ticker_beta=1.2):
    # Baseline expectancy: EV per dollar of risk (probability-weighted).
    p_loss = abs(short_delta)
    p_win = 1.0 - p_loss
    ev = (p_win * net_credit) - (p_loss * max_loss)
    pop = p_win * 100
    edge_ratio = ev / max_loss if max_loss > 0 else 0

    # Execution layer: penalize wider bid/ask (we also HARD-filter at MAX_SHORT_BA_WIDTH).
    liquidity_factor = 1.0
    if short_ba_width > 0.10:
        liquidity_factor = max(0.1, 1.0 - ((short_ba_width - 0.10) * 4))

    # DTE band preference: peaks at DTE_BAND_CENTER and tapers toward the edges,
    # instead of "shortest always wins" (which favored un-holdable 0-1 DTE).
    dte_factor = max(0.4, 1.0 - abs(dte - DTE_BAND_CENTER) / 15.0)

    # Theta efficiency: daily premium decay captured per dollar of risk — the
    # truest "fast money" metric for a same-day / next-day exit. Boosts score up to ~2x.
    risk_dollars = max_loss * 100
    theta_eff = (abs(short_theta) / risk_dollars) if (short_theta and risk_dollars > 0) else 0.0
    theta_factor = 1.0 + min(theta_eff * 2000.0, 1.0)

    # Combined edge score
    velocity_edge_score = edge_ratio * dte_factor * liquidity_factor * theta_factor

    # Position sizing (risk-capped)
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
        "theta_efficiency": round(theta_eff, 6),
        "dte_factor": round(dte_factor, 3),
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
        "start": (datetime.date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    })
    history = data.get("history", {}).get("day", []) if data else []
    return [float(d["close"]) for d in history if "close" in d]


def moving_average(values, period):
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def trend_ok(price, hist):
    """Reject only a CONFIRMED downtrend (price below the slow MA AND fast MA below
    slow MA). Neutral, consolidating, and uptrending names pass; passes if data is thin."""
    if not USE_TREND_FILTER:
        return True
    ma_fast = moving_average(hist, TREND_FAST_MA)
    ma_slow = moving_average(hist, TREND_SLOW_MA)
    if ma_fast is None or ma_slow is None:
        return True
    return not (price < ma_slow and ma_fast < ma_slow)

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

def price_chain_credits(symbol, expiration, strike_pairs):
    """Fetch ONE option chain and return {short_strike: net_credit or None} for the
    requested (short_strike, long_strike) pairs. Used to re-price archived spreads
    that have dropped off the main list, batched per (symbol, expiration)."""
    chain = fetch_data("markets/options/chains",
                       {"symbol": symbol, "expiration": expiration, "greeks": "false"})
    options = chain.get("options", {}).get("option", []) if chain else []
    if isinstance(options, dict):
        options = [options]
    by_strike = {float(o["strike"]): o for o in options if o.get("option_type") == "put"}

    out = {}
    for short_strike, long_strike in strike_pairs:
        s = by_strike.get(short_strike)
        l = by_strike.get(long_strike)
        if not s or not l:
            out[short_strike] = None
            continue
        s_bid = float(s.get("bid", 0) or 0); s_ask = float(s.get("ask", 0) or 0)
        l_bid = float(l.get("bid", 0) or 0); l_ask = float(l.get("ask", 0) or 0)
        if s_bid == 0 and s_ask == 0:          # no live market (e.g. closed) -> skip point
            out[short_strike] = None
            continue
        nc = (((s_bid + s_ask) / 2) - ((l_bid + l_ask) / 2)) * (1 - SLIPPAGE_ADJUST)
        out[short_strike] = round(nc, 2)
    return out


def update_archive(archive, top_signals, scan_time):
    """Maintain spread_archive: append a credit point for every tracked spread each
    run (re-pricing ones that fell off the main list), and drop expired entries."""
    today = datetime.date.today()
    current_keys = set()

    # 1) Spreads in the current top list: upsert + append their freshly-scanned credit.
    for s in top_signals:
        key = s["spread_key"]
        current_keys.add(key)
        entry = archive.get(key)
        if entry is None:
            entry = {
                "spread_key":   key,
                "symbol":       s["symbol"],
                "expiration":   s["expiration"],
                "spread":       s["spread"],
                "short_strike": s.get("short_strike"),
                "long_strike":  s.get("long_strike"),
                "first_seen":   scan_time,
                "credit_history": []
            }
            archive[key] = entry
        entry["last_seen"] = scan_time
        entry["in_current_scan"] = True
        entry["credit_history"].append({"time": scan_time, "net_credit": s["net_credit"]})

    # 2) Archived spreads NOT in the current list and not expired: re-fetch a live
    #    quote and append a point. Group by (symbol, expiration) = one chain call each.
    groups = {}
    for key, entry in archive.items():
        if key in current_keys:
            continue
        try:
            exp_date = datetime.datetime.strptime(entry.get("expiration", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        if exp_date < today:
            continue  # expired -> dropped in step 3
        ss, ls = entry.get("short_strike"), entry.get("long_strike")
        if ss is None or ls is None:
            entry["in_current_scan"] = False
            continue
        groups.setdefault((entry["symbol"], entry["expiration"]), []).append((float(ss), float(ls), key))

    for (symbol, exp), items in groups.items():
        credits = price_chain_credits(symbol, exp, [(ss, ls) for ss, ls, _ in items])
        for ss, ls, key in items:
            entry = archive[key]
            entry["in_current_scan"] = False
            nc = credits.get(ss)
            if nc is not None:
                entry["last_seen"] = scan_time
                entry["credit_history"].append({"time": scan_time, "net_credit": nc})

    # 3) Drop expired entries so the file stays bounded.
    for key in list(archive.keys()):
        try:
            if datetime.datetime.strptime(archive[key].get("expiration", ""), "%Y-%m-%d").date() < today:
                del archive[key]
        except Exception:
            pass

    return archive


def run_workstation_scan():
    print(f"\n[SYSTEM] Initializing Custom Velocity Edge Scan...")
    
    archive = load_existing_archive()
    prior_archive_keys = set(archive.keys())
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

        # Trend gate: skip the whole symbol if it's in a confirmed downtrend.
        if not trend_ok(price, hist):
            print(f"  > {symbol}: downtrend — skipping".ljust(60))
            continue

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
                if s_ba_width > MAX_SHORT_BA_WIDTH: continue           # HARD exit-cost filter
                if s_bid > 0 and (s_ask / s_bid) > MAX_BID_ASK_RATIO: continue
                if int(short_opt.get("open_interest", 0) or 0) < MIN_OI: continue
                if int(short_opt.get("volume", 0) or 0) < MIN_VOLUME: continue

                greeks_dict = short_opt.get("greeks", {})
                if not greeks_dict or greeks_dict.get("delta") is None: continue
                delta = float(greeks_dict.get("delta"))
                theta = greeks_dict.get("theta")
                theta = float(theta) if theta is not None else 0.0
                iv = greeks_dict.get("mid_iv") or greeks_dict.get("smv_vol") or greeks_dict.get("ask_iv") or 0
                iv = float(iv) if iv else 0.0

                # Assignment-risk control: short put must be far OTM by delta.
                if abs(delta) > MAX_SHORT_DELTA: continue

                # Expected-move cushion: short strike at least 1 expected move below price.
                if iv > 0:
                    exp_move = price * iv * math.sqrt(max(dte, 1) / 365.0)
                    if (price - strike) < EXPECTED_MOVE_MULT * exp_move: continue

                l_bid = float(long_opt.get("bid", 0) or 0)
                l_ask = float(long_opt.get("ask", 0) or 0)
                net_credit = (((s_bid + s_ask)/2) - ((l_bid + l_ask)/2)) * (1 - SLIPPAGE_ADJUST)
                max_loss = SPREAD_WIDTH - net_credit

                if net_credit <= 0.03: continue

                metrics = calculate_velocity_metrics(
                    delta, theta, net_credit, max_loss, price, spy_price,
                    dte, s_ba_width, ticker_beta=BETA_MAPPING.get(symbol, 1.2)
                )

                if metrics["pop"] < MIN_PROB_PROFIT or metrics["ev"] <= 0: continue

                spread_label = f"{strike}/{strike-SPREAD_WIDTH}P"
                spread_key = f"{symbol}|{spread_label}|{exp}"

                earnings_date, earnings_before_exp = earnings_check(symbol, exp, earnings_map)

                total_risk = round(metrics["qty"] * max_loss * 100)

                signal = {
                    "symbol": symbol,
                    "expiration": exp,
                    "dte": dte,
                    "spread": spread_label,
                    "spread_key": spread_key,
                    "short_strike": strike,
                    "long_strike": strike - SPREAD_WIDTH,
                    "is_new": (spread_key not in prior_archive_keys),
                    "price": round(price, 2),
                    "underlying_price": round(price, 2),
                    "net_credit": round(net_credit, 2),
                    "max_loss": round(max_loss, 2),
                    "ev": metrics["ev"],
                    "pop_pct": metrics["pop"],
                    "rec_qty": metrics["qty"],
                    "spy_delta_eq": metrics["spy_weighted_delta"],
                    "correlation_spy": correlation,
                    "edge_ratio": metrics["edge_ratio"],
                    "total_risk": total_risk,
                    "short_delta": round(delta, 4),
                    "theta_efficiency": metrics["theta_efficiency"],
                    "velocity_edge_score": metrics["velocity_edge_score"],
                    "bid_ask_spread_width": round(s_ba_width, 2),
                    "earnings_date": earnings_date,
                    "earnings_before_exp": earnings_before_exp
                }

                all_signals.append(signal)

        time.sleep(0.3)

    # Rank exclusively by the brand new High-Velocity Win score
    all_signals.sort(key=lambda x: x["velocity_edge_score"], reverse=True)
    top_signals = all_signals[:20]

    # Anti-blank guard: if this scan found ZERO signals (e.g. a pre-market or
    # after-hours run where option bids are 0 and every leg gets filtered out),
    # do NOT overwrite a good existing file and do NOT append junk to the archive.
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

    # Maintain the credit-history archive: append a point for every tracked spread
    # (re-pricing ones that fell off the list), drop expired entries.
    archive = update_archive(archive, top_signals, scan_time)

    report = {
        "scan_time": scan_time,
        "sorting_method": "Velocity Edge Engine (Optimized for Fast Fills & Rapid Theta Decay)",
        "account_basis": ACCOUNT_SIZE,
        "benchmark_spy": round(spy_price, 2),
        "top_signals": top_signals,
        "spread_archive": archive
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=4)

    archived_only = sum(1 for e in archive.values() if not e.get("in_current_scan"))
    print(f"\n[SUCCESS] Scan finalized: {len(top_signals)} live signal(s), "
          f"{len(archive)} spread(s) in archive ({archived_only} off-list, still tracked).")

if __name__ == "__main__":
    run_workstation_scan()
