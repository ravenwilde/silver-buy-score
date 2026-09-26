# Silver Buy-Score

Daily silver (XAG/USD) buy-score automation for a jeweler deciding whether to buy silver stock. Runs weekdays at 9:00 AM as a scheduled task and delivers a visual report card.

## Layout

```
scripts/silver_buy_score.py   # main script (stdlib only, Python 3)
state/silver_history.csv      # daily closes: date,close — single source of truth
state/backfill_done           # one-time history-backfill flag (do not delete)
```

## Run

```bash
python3 scripts/silver_buy_score.py
```

## Data sources (in fallback order)

1. Yahoo Finance `SI=F` (also the bulk-history source; blocked in some sandbox networks — fails fast)
2. gold-api.com (`/price/XAG`)
3. Swissquote public quotes (XAG/USD bid/ask midpoint)

If all sources fail, the last cached price is reported and clearly marked STALE.

## History

`state/silver_history.csv` holds daily closes from 2015-03-02 onward. Seeded 2026-09-26 from a Stooq XAGUSD daily CSV (official closes win on overlap); each run appends one live spot quote, deduplicated by date. The one-time Yahoo backfill is controlled by `state/backfill_done` — once that flag exists the backfill never retries.

## Scoring (1–10)

| Sub-score | Basis |
|---|---|
| RSI(14) | Oversold scores high, overbought low |
| MA position | Price vs 20/50-day SMA, penalizes overextension |
| Momentum | 7/30-day % change; mildly positive is best |
| Volatility | 30-day annualized; calm scores high |

Labels: 1–3 Avoid · 4–5 Cautious · 6–7 Fair · 8–10 Strong Buy. Trend projection is a least-squares slope over the last 30 closes, projected 7/14/30 days.

## Jeweler math

Sterling (.925) value = spot × 0.925; 1 troy oz = 31.1035 g.

Decision aid only, not financial advice.
