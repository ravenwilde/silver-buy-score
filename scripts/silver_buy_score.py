#!/usr/bin/env python3
"""Daily silver buy-score check.

Fetches XAG/USD spot (Yahoo Finance SI=F primary; gold-api.com then Swissquote
as fallbacks), maintains a price-history cache in ../state/silver_history.csv,
computes RSI/MA/momentum/volatility sub-scores, and prints a concise report
with a 1-10 buy score for a jeweler deciding whether to buy silver stock.
"""

import csv
import json
import math
import os
import statistics
import sys
import urllib.request
from datetime import datetime, timezone, date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
HISTORY_CSV = os.path.join(STATE_DIR, "silver_history.csv")
TROY_OZ_GRAMS = 31.1035
STERLING_PURITY = 0.925
BACKFILL_FLAG = os.path.join(STATE_DIR, "backfill_done")

UA = {"User-Agent": "Mozilla/5.0 (silver-buy-score)"}


def _get_json(url, timeout=20):
    # Yahoo can hang where it is network-blocked: fail fast so fallbacks engage
    if "yahoo" in url:
        timeout = 8
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- price sources ----------------

def fetch_yahoo():
    """Return (spot, source, asof, seeded_history) from Yahoo Finance SI=F."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/SI=F"
           "?interval=1d&range=1y")
    data = _get_json(url)
    result = data["chart"]["result"][0]
    ts = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    hist = []
    for t, c in zip(ts, closes):
        if c is not None:
            d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
            hist.append((d, round(float(c), 4)))
    if not hist:
        raise ValueError("Yahoo returned no closes")
    spot = hist[-1][1]
    asof = hist[-1][0]
    return spot, "Yahoo Finance (SI=F)", asof, hist


def fetch_gold_api():
    data = _get_json("https://api.gold-api.com/price/XAG")
    price = float(data["price"])
    asof = data.get("updatedAt", date.today().isoformat())
    return price, "gold-api.com", str(asof)[:10], None


def fetch_swissquote():
    url = ("https://forex-data-feed.swissquote.com/public-quotes/bboquotes"
           "/instrument/XAG/USD")
    data = _get_json(url)
    node = data[0] if isinstance(data, list) else data
    profiles = node.get("spreadProfilePrices") or []
    if profiles:
        bid = float(profiles[0]["bid"]); ask = float(profiles[0]["ask"])
        price = (bid + ask) / 2.0
    else:
        price = float(node["topo"]["price"])
    asof = str(node.get("ts") or node.get("date") or date.today().isoformat())[:10]
    return price, "Swissquote", asof, None


def get_spot():
    errors = []
    for fn in (fetch_yahoo, fetch_gold_api, fetch_swissquote):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError("All price sources failed -> " + " | ".join(errors))


# ---------------- history cache ----------------

def load_history():
    rows = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="") as f:
            for d, p in csv.reader(f):
                try:
                    rows.append((d, float(p)))
                except ValueError:
                    continue
    return rows


def save_history(rows):
    os.makedirs(STATE_DIR, exist_ok=True)
    dedup = {}
    for d, p in rows:
        dedup[d] = p
    rows = sorted(dedup.items())
    with open(HISTORY_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)
    return rows


# ---------------- one-time history backfill ----------------

def try_backfill(existing_rows):
    """One-time attempt to seed ~1y of daily history from Yahoo Finance.

    Runs only if state/backfill_done is absent. Whether it succeeds or fails,
    the flag file is written so it never retries (spec: one-time backfill).
    Returns the seeded rows if successful, else None.
    """
    if os.path.exists(BACKFILL_FLAG):
        return None
    result = None
    try:
        _spot, _src, _asof, seeded = fetch_yahoo()
        if seeded and len(seeded) > len(existing_rows):
            result = seeded
    except Exception as e:  # noqa: BLE001
        print(f"(one-time history backfill unavailable: {e})")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(BACKFILL_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat()
                    + (" seeded" if result else " failed"))
    except OSError:
        pass
    return result


# ---------------- indicators ----------------

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-period - 1:-1], closes[-period:]):
        ch = b - a
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def pct_change(closes, n):
    if len(closes) < n + 1:
        return None
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


def volatility(closes, n=30):
    if len(closes) < n + 1:
        return None
    rets = [(b / a - 1.0) for a, b in zip(closes[-n - 1:-1], closes[-n:]) if a > 0]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(252) * 100.0  # annualized %


def trend_projection(closes):
    """Least-squares slope (%/day) over the last 30 closes; project 7/14/30d."""
    window = closes[-30:] if len(closes) >= 30 else closes
    n = len(window)
    if n < 5:
        return None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(window) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0 or my == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, window)) / denom
    daily_pct = slope / my * 100.0
    return {d: daily_pct * d for d in (7, 14, 30)}


# ---------------- scoring ----------------

def clamp(v, lo=0.0, hi=10.0):
    return max(lo, min(hi, v))


def score_rsi(r):
    if r is None:
        return None
    if r <= 20:
        return 10.0
    if r >= 80:
        return 0.0
    if r <= 50:
        return 10.0 - (r - 20) / 30.0 * 4.0      # 20->10, 50->6
    return 6.0 - (r - 50) / 30.0 * 6.0           # 50->6, 80->0


def score_ma(price, s20, s50):
    if s20 is None or s50 is None:
        return None
    above = (1 if price > s20 else 0) + (1 if price > s50 else 0)
    dev = ((price / s20 - 1.0) + (price / s50 - 1.0)) / 2.0 * 100.0
    stretch = clamp(5.0 - dev / 2.0)             # extended above MA = less attractive
    return clamp(above * 2.0 + stretch * 0.6)


def score_momentum(m7, m30):
    vals = [m for m in (m7, m30) if m is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    # mildly positive momentum is best; strong spikes = chase risk
    if avg <= -8:
        return 2.0
    if avg >= 8:
        return 4.0
    return clamp(5.0 + avg / 2.0) if avg < 0 else clamp(6.0 + avg / 4.0 - max(0.0, avg - 4.0) / 2.0)


def score_volatility(v):
    if v is None:
        return None
    if v <= 15:
        return 8.0
    if v >= 45:
        return 1.0
    return clamp(8.0 - (v - 15) / 30.0 * 7.0)


def label_for(score):
    if score <= 3:
        return "Avoid"
    if score <= 5:
        return "Cautious"
    if score <= 7:
        return "Fair"
    return "Strong Buy"


def interpretation(score, r, m7, proj):
    bits = []
    if r is not None:
        bits.append("oversold" if r < 30 else "overbought" if r > 70 else "neutral momentum")
    if m7 is not None:
        bits.append(f"{'up' if m7 >= 0 else 'down'} {abs(m7):.1f}% over the past week")
    trend = ""
    if proj:
        t = proj[30]
        trend = " Prices are trending " + ("upward" if t > 1 else "downward" if t < -1 else "sideways") + "."
    if score >= 8:
        act = "conditions look favorable for stocking up today."
    elif score >= 6:
        act = "a reasonable day to buy, though not a standout opportunity."
    elif score >= 4:
        act = "consider buying only what you need for near-term orders."
    else:
        act = "better to hold off on new stock purchases if you can."
    return (f"Silver looks {bits[0] if bits else 'steady'}"
            + (f" and is {bits[1]}" if len(bits) > 1 else "")
            + "." + trend + f" Overall, {act}")


# ---------------- main ----------------

def main():
    stale = False
    try:
        spot, source, asof, seeded = get_spot()
    except RuntimeError as e:
        hist = load_history()
        if not hist:
            print("ERROR: no price sources available and no cached history.")
            print(str(e))
            return 1
        spot, source, asof = hist[-1][1], "cache (ALL SOURCES FAILED - STALE DATA)", hist[-1][0]
        seeded = None
        stale = True

    hist = load_history()
    backfilled = try_backfill(hist)
    if backfilled:
        hist = save_history(backfilled + [(asof, spot)])
        print(f"(one-time backfill succeeded: {len(backfilled)} days of history loaded)")
    elif seeded:
        hist = save_history(seeded + [(asof, spot)])
    else:
        hist = save_history(hist + [(asof, spot)])
    closes = [p for _, p in hist]
    price = closes[-1]

    r = rsi(closes)
    s20, s50 = sma(closes, 20), sma(closes, 50)
    m7, m30 = pct_change(closes, 7), pct_change(closes, 30)
    vol = volatility(closes)
    proj = trend_projection(closes)

    subs = {
        "RSI(14)": score_rsi(r),
        "MA position": score_ma(price, s20, s50),
        "Momentum": score_momentum(m7, m30),
        "Volatility": score_volatility(vol),
    }
    available = [v for v in subs.values() if v is not None]
    score = round(sum(available) / len(available)) if available else 5
    score = int(clamp(score, 1, 10))
    label = label_for(score)

    sterling_oz = price * STERLING_PURITY
    sterling_g = sterling_oz / TROY_OZ_GRAMS

    print(f"Silver spot: ${price:,.2f}/oz (fine, {source}, as of {asof})")
    print(f"Sterling .925: ${sterling_oz:,.2f}/oz | ${sterling_g:,.3f}/g")
    print(f"BUY SCORE: {score}/10 - {label}")
    for k, v in subs.items():
        print(f"  - {k}: {'n/a' if v is None else f'{v:.1f}/10'}")
    if proj:
        def arrow(x):
            return f"{'+' if x >= 0 else ''}{x:.1f}%"
        print(f"Trend projection: 7d {arrow(proj[7])} | 14d {arrow(proj[14])} | 30d {arrow(proj[30])}")
    else:
        print("Trend projection: insufficient history yet")
    if any(v is None for v in subs.values()):
        print(f"(Indicators still warming up: {len(closes)} day(s) of history cached; "
              "full sub-scores need 15-50 days. Score uses available indicators only.)")
    print("Interpretation: " + interpretation(score, r, m7, proj))
    if stale:
        print("WARNING: all live sources failed; figures above use the last cached price.")
    print("Note: decision aid only, not financial advice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
