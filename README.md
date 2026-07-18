# Crypto Tracker

A self-hosted crypto portfolio tracker that replaced a decade-old Excel
workbook. Flask + SQLite, no cloud: every transaction, price point, and
chart lives in one local database file you can copy anywhere.

Built to answer the questions the spreadsheet answered — *what's my lifetime
P/L, what did that dip cost me, when did I actually buy?* — without the
spreadsheet's fragility, and with the numbers verified against it to the cent
during migration.

## Features

| Tab | What it does |
|---|---|
| **Dashboard** | Portfolio value, net invested, realized + unrealized P/L, value-over-time vs a BTC benchmark, drawdown, allocation by coin/category, monthly investing activity, holdings table, rebalance preview, FIFO realized gains by year |
| **Transactions** | Add / edit / delete buys and sells; full searchable history |
| **Charts** | Price history per coin with your buys/sells and average-cost line overlaid |
| **Market** | Market cap, BTC/ETH dominance, Fear & Greed index with history, trending coins |
| **Coins** | Search CoinGecko and track new coins by category |

## Design notes

- **FIFO cost-basis engine** — realized gains computed lot-by-lot, matching
  how a careful spreadsheet (and a tax preparer) would do it.
- **Excel import as a first-class feature** (`import_excel.py`) — the
  migration path *was* the product; per-coin quantities and net costs were
  reconciled against the workbook before the workbook was retired.
- **Local-first pricing cache** — CoinGecko's free API is rate-limited, so
  every price fetched is stored; charts get faster and increasingly
  offline-capable over time.
- **Session auth with hashed passwords** — it's a LAN/VPN app, but it still
  assumes the network is hostile: loopback bind, fronted by a WireGuard mesh
  (Tailscale) for remote access, never port-forwarded.
- **Runs as a launchd service** with `KeepAlive` — starts at login, restarts
  on crash, survives reboots unattended.

## Running it

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
# → http://127.0.0.1:5178
```

First run creates `portfolio.db` and walks through setup. To import from an
Excel workbook: `./venv/bin/python import_excel.py "/path/to/workbook.xlsm"`.

## Stack

Flask · SQLite · vanilla JS + Chart.js · CoinGecko & alternative.me APIs ·
launchd
