"""Crypto Portfolio Tracker - local web app.

Run:  ./venv/bin/python app.py   (or double-click "Start Crypto Tracker.command")
Data: portfolio.db (SQLite, lives next to this file)
APIs: CoinGecko (prices, free tier) and alternative.me (Fear & Greed).

Modules: db (connection + schema), auth (login/passkeys/CSRF), market_data
(price APIs + api_cache), portfolio_math (FIFO/XIRR/balances), stocks
(RJ statements), coinbase_sync (Coinbase import). Route-owning modules
attach themselves via their register(app) at the bottom of this file.
"""
import os
import io
import csv
import sqlite3
import time
import json
import bisect
import re
import threading
import subprocess
from datetime import datetime, date, timedelta, timezone

import requests
from flask import Flask, jsonify, request, render_template, Response

import auth
import stocks
import coinbase_sync as cb
from db import APP_DIR, DB_PATH, get_db, close_db, init_db, get_setting
from market_data import (COINGECKO, cached_fetch, cg_get, get_market_data, get_history,
                         YAHOO_CRYPTO, ensure_yahoo_backfill, ensure_btc_backfill)
from portfolio_math import (get_balances, hot_balance_error, compute_fifo, compute_xirr,
                            _is_long_term, lot_tax_view, whatif_sale,
                            load_holdings_axis, walk_holdings)

try:
    os.chdir(APP_DIR)
except OSError:
    pass

app = Flask(__name__, root_path=APP_DIR, instance_path=os.path.join(APP_DIR, "instance"))
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
app.teardown_appcontext(close_db)


# ---------------------------------------------------------------- pages

@app.route("/")
def index():
    # never let the browser serve a stale app shell after an update
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ---------------------------------------------------------------- portfolio

@app.route("/api/portfolio")
def api_portfolio():
    db = get_db()
    market = get_market_data()
    holdings = []
    cold_map = {r["symbol"]: r["q"] for r in db.execute(
        """SELECT symbol, SUM(CASE WHEN direction='to_cold' THEN quantity ELSE -quantity END) q
           FROM transfers GROUP BY symbol""")}
    total_cold = 0.0
    rows = db.execute(
        """SELECT c.symbol, c.name, c.category, c.coingecko_id,
                  SUM(CASE WHEN t.side='buy' THEN t.quantity ELSE -t.quantity END) qty,
                  SUM(CASE WHEN t.side='buy' THEN t.total ELSE -t.total END) net_cost,
                  SUM(CASE WHEN t.side='buy' THEN t.total ELSE 0 END) invested,
                  SUM(CASE WHEN t.side='sell' THEN t.total ELSE 0 END) proceeds,
                  COUNT(*) n_tx
           FROM coins c JOIN transactions t ON t.symbol=c.symbol
           GROUP BY c.symbol ORDER BY 6 DESC"""
    ).fetchall()
    total_value = total_cost = 0.0
    for r in rows:
        m = market.get(r["coingecko_id"], {})
        price = m.get("current_price") or 0
        qty = r["qty"] or 0
        value = qty * price
        net_cost = r["net_cost"] or 0
        h = {
            "symbol": r["symbol"].upper(),
            "name": r["name"],
            "category": r["category"],
            "coingecko_id": r["coingecko_id"],
            "quantity": qty,
            "net_cost": net_cost,
            "invested": r["invested"],
            "proceeds": r["proceeds"],
            "cost_avg": (net_cost / qty) if qty > 1e-12 else None,
            "price": price,
            "value": value,
            "pl": value - net_cost,
            "pl_pct": ((value - net_cost) / net_cost * 100) if abs(net_cost) > 1e-9 else None,
            "change_24h": m.get("price_change_percentage_24h_in_currency"),
            "change_7d": m.get("price_change_percentage_7d_in_currency"),
            "market_cap_rank": m.get("market_cap_rank"),
            "image": m.get("image"),
            "ath": m.get("ath"),
            "ath_change_pct": m.get("ath_change_percentage"),
            "n_tx": r["n_tx"],
        }
        cold_qty = min(cold_map.get(r["symbol"], 0.0), qty) if qty > 0 else 0.0
        h["cold_qty"] = cold_qty
        h["cold_value"] = cold_qty * price
        h["cold_pct"] = (cold_qty / qty * 100) if qty > 1e-12 else None
        total_cold += h["cold_value"]
        total_value += value
        total_cost += net_cost
        holdings.append(h)
    # dust filter flag: positions worth under $1 count as closed
    # (fall back to a near-zero test when no market price is available)
    for h in holdings:
        h["closed"] = abs(h["value"]) < (1.0 if h["price"] else 0.01)
    # yearly P/L (invested vs proceeds per year) + FIFO realized gains
    realized_by_year, open_cost, _sales, _lots = compute_fifo(db)
    yearly = db.execute(
        """SELECT substr(date,1,4) year,
                  SUM(CASE WHEN side='buy' THEN total ELSE 0 END) invested,
                  SUM(CASE WHEN side='sell' THEN total ELSE 0 END) proceeds
           FROM transactions GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    return jsonify({
        "holdings": holdings,
        "total_value": total_value,
        "total_cost": total_cost,
        "total_pl": total_value - total_cost,
        "total_pl_pct": ((total_value - total_cost) / total_cost * 100) if total_cost else None,
        "total_realized": sum(realized_by_year.values()),
        "total_unrealized": total_value - sum(open_cost.values()),
        "total_cold": total_cold,
        "cold_pct": (total_cold / total_value * 100) if total_value else None,
        "xirr": compute_xirr(db, total_value),
        "yearly": [dict(y, realized=realized_by_year.get(y["year"], 0.0)) for y in yearly],
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ---------------------------------------------------------------- transactions

@app.route("/api/transactions", methods=["GET", "POST"])
def api_transactions():
    db = get_db()
    if request.method == "POST":
        d = request.get_json(force=True)
        symbol = d["symbol"].strip().lower()
        coin = db.execute("SELECT 1 FROM coins WHERE symbol=?", (symbol,)).fetchone()
        if not coin:
            return jsonify({"error": "Unknown coin '{}'. Add it on the Coins tab first.".format(symbol)}), 400
        qty = abs(float(d["quantity"]))
        if d["side"] == "sell":
            err = hot_balance_error(db, symbol, qty)
            if err:
                return jsonify({"error": err}), 400
        price = float(d["price"])
        fee = float(d.get("fee") or 0)
        side = d["side"]
        total = float(d["total"]) if d.get("total") not in (None, "") else (
            qty * price + fee if side == "buy" else qty * price - fee)
        cur = db.execute(
            "INSERT INTO transactions (date,symbol,side,quantity,price,fee,total,exchange,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d["date"], symbol, side, qty, price, fee, total,
             d.get("exchange", ""), d.get("notes", "")),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    q = "SELECT t.*, c.name FROM transactions t JOIN coins c ON c.symbol=t.symbol"
    args = []
    if request.args.get("symbol"):
        q += " WHERE t.symbol=?"
        args.append(request.args["symbol"].lower())
    q += " ORDER BY t.date DESC, t.id DESC"
    rows = [dict(r) for r in db.execute(q, args).fetchall()]
    return jsonify(rows)


@app.route("/api/transactions/<int:tx_id>", methods=["PUT", "DELETE"])
def api_transaction(tx_id):
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
        db.commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True)
    db.execute(
        "UPDATE transactions SET date=?, symbol=?, side=?, quantity=?, price=?, fee=?, total=?, exchange=?, notes=? WHERE id=?",
        (d["date"], d["symbol"].lower(), d["side"], abs(float(d["quantity"])), float(d["price"]),
         float(d.get("fee") or 0), float(d["total"]), d.get("exchange", ""), d.get("notes", ""), tx_id),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- coins

@app.route("/api/convert", methods=["POST"])
def api_convert():
    """Record a coin-to-coin conversion as a matched sell + buy pair.
    Small balance gaps (rounding/rewards) get a $0-basis adjustment;
    large gaps are rejected so missing transactions don't slip through."""
    d = request.get_json(force=True)
    db = get_db()
    frm = d["from_symbol"].strip().lower()
    to = d["to_symbol"].strip().lower()
    if frm == to:
        return jsonify({"error": "From and To are the same coin."}), 400
    for s in (frm, to):
        if not db.execute("SELECT 1 FROM coins WHERE symbol=?", (s,)).fetchone():
            return jsonify({"error": "Unknown coin '{}'. Add it on the Coins tab first.".format(s)}), 400
    try:
        from_qty = float(d["from_qty"])
        to_qty = float(d["to_qty"])
        usd = float(d["usd"])
        if from_qty <= 0 or to_qty <= 0 or usd <= 0:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        return jsonify({"error": "Amounts and USD value must be positive numbers."}), 400
    date_s = d["date"]
    note = "Converted {} -> {}".format(frm.upper(), to.upper())
    if d.get("notes"):
        note += " - " + d["notes"]
    bal, cold = get_balances(db, frm)
    adjustment = 0.0
    if from_qty > bal + 1e-9:
        gap = from_qty - bal
        gap_usd = gap * (usd / from_qty)
        if gap_usd > 10:
            return jsonify({"error":
                "The app shows only {:.8f} {} but this converts {:.8f} (missing ~${:.2f}). "
                "Record the missing buys/rewards first.".format(bal, frm.upper(), from_qty, gap_usd)}), 400
        db.execute(
            "INSERT INTO transactions (date,symbol,side,quantity,price,fee,total,exchange,notes) "
            "VALUES (?,?,?,?,0,0,0,?,?)",
            (date_s, frm, "buy", gap, d.get("exchange", "COINBASE"),
             "Balance adjustment: {:.8f} {} not in log (reward/rounding), $0 basis".format(gap, frm.upper())))
        adjustment = gap
        bal += gap
    # conversions sell from Coinbase only - coins in the cold wallet don't count
    err = hot_balance_error(db, frm, from_qty, bal=bal, cold=cold)
    if err:
        return jsonify({"error": err}), 400
    db.execute(
        "INSERT INTO transactions (date,symbol,side,quantity,price,fee,total,exchange,notes) "
        "VALUES (?,?,?,?,?,0,?,?,?)",
        (date_s, frm, "sell", from_qty, usd / from_qty, usd, d.get("exchange", "COINBASE"), note))
    db.execute(
        "INSERT INTO transactions (date,symbol,side,quantity,price,fee,total,exchange,notes) "
        "VALUES (?,?,?,?,?,0,?,?,?)",
        (date_s, to, "buy", to_qty, usd / to_qty, usd, d.get("exchange", "COINBASE"), note))
    db.commit()
    return jsonify({"ok": True, "adjustment": adjustment})


@app.route("/api/coins", methods=["GET", "POST"])
def api_coins():
    db = get_db()
    if request.method == "POST":
        d = request.get_json(force=True)
        db.execute(
            "INSERT OR REPLACE INTO coins (symbol, coingecko_id, name, category) VALUES (?,?,?,?)",
            (d["symbol"].strip().lower(), d["coingecko_id"].strip(), d["name"].strip(),
             d.get("category", "Other")),
        )
        db.commit()
        return jsonify({"ok": True})
    return jsonify([dict(r) for r in db.execute("SELECT * FROM coins ORDER BY symbol")])


@app.route("/api/todos", methods=["GET", "POST"])
def api_todos():
    db = get_db()
    if request.method == "POST":
        d = request.get_json(force=True)
        text = (d.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Empty to-do."}), 400
        db.execute("INSERT INTO todos (text, done, created) VALUES (?, 0, ?)",
                   (text, date.today().isoformat()))
        db.commit()
        return jsonify({"ok": True})
    rows = db.execute("SELECT * FROM todos ORDER BY done, id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/todos/<int:todo_id>", methods=["PUT", "DELETE"])
def api_todo(todo_id):
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    else:
        db.execute("UPDATE todos SET done = 1 - done WHERE id=?", (todo_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/transfers", methods=["GET", "POST"])
def api_transfers():
    """Cold-wallet ledger: moves between Coinbase and cold storage.
    Does not touch cost basis or P/L - only where the coins live."""
    db = get_db()
    if request.method == "POST":
        d = request.get_json(force=True)
        sym = d["symbol"].strip().lower()
        if not db.execute("SELECT 1 FROM coins WHERE symbol=?", (sym,)).fetchone():
            return jsonify({"error": "Unknown coin '{}'.".format(sym)}), 400
        try:
            qty = float(d["quantity"])
            if qty <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "Quantity must be a positive number."}), 400
        direction = d["direction"]
        if direction not in ("to_cold", "from_cold"):
            return jsonify({"error": "Bad direction."}), 400
        total, cold = get_balances(db, sym)
        eps = 1e-9
        if direction == "to_cold" and qty > (total - cold) + eps:
            return jsonify({"error": "Only {:.8f} {} is on Coinbase to move.".format(
                max(0.0, total - cold), sym.upper())}), 400
        if direction == "from_cold" and qty > cold + eps:
            return jsonify({"error": "Only {:.8f} {} is in cold storage.".format(
                max(0.0, cold), sym.upper())}), 400
        db.execute(
            "INSERT INTO transfers (date, symbol, quantity, direction, notes) VALUES (?,?,?,?,?)",
            (d["date"], sym, qty, direction, d.get("notes", "")))
        db.commit()
        return jsonify({"ok": True})
    rows = db.execute(
        "SELECT t.*, c.name FROM transfers t JOIN coins c ON c.symbol=t.symbol "
        "ORDER BY t.date DESC, t.id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transfers/<int:tf_id>", methods=["DELETE"])
def api_transfer_delete(tf_id):
    db = get_db()
    db.execute("DELETE FROM transfers WHERE id=?", (tf_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/targets", methods=["GET", "POST"])
def api_targets():
    """Saved target allocations (percent per symbol) for the rebalance preview."""
    db = get_db()
    if request.method == "POST":
        d = request.get_json(force=True)
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('rebalance_targets', ?)",
            (json.dumps(d.get("targets", {})),),
        )
        db.commit()
        return jsonify({"ok": True})
    row = db.execute("SELECT value FROM settings WHERE key='rebalance_targets'").fetchone()
    return jsonify(json.loads(row["value"]) if row else {})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    data = cg_get("/search", {"query": q}, ttl=3600)
    return jsonify(data.get("coins", [])[:10])


# ---------------------------------------------------------------- charts

@app.route("/api/history/coin/<cg_id>")
def api_coin_history(cg_id):
    days = int(request.args.get("days", 365))
    if days > 365 and cg_id in YAHOO_CRYPTO:
        ensure_yahoo_backfill(get_db(), cg_id, YAHOO_CRYPTO[cg_id])
    rows = get_history(cg_id, days)
    return jsonify([{"date": r["date"], "price": r["price"]} for r in rows])


@app.route("/api/history/ratio")
def api_ratio():
    """ETH/BTC ratio over time - the classic altcoin-cycle signal."""
    days = int(request.args.get("days", 1095))
    db = get_db()
    ensure_yahoo_backfill(db, "bitcoin", "BTC-USD")
    ensure_yahoo_backfill(db, "ethereum", "ETH-USD")
    start = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        """SELECT b.date, e.price / b.price AS ratio
           FROM price_history b
           JOIN price_history e ON e.date = b.date AND e.coingecko_id = 'ethereum'
           WHERE b.coingecko_id = 'bitcoin' AND b.price > 0 AND b.date >= ?
           ORDER BY b.date""", (start,)).fetchall()
    return jsonify([{"date": r["date"], "ratio": r["ratio"]} for r in rows])


@app.route("/api/history/dominance")
def api_dominance():
    """Self-recorded daily BTC dominance readings (starts 2026-07-05)."""
    rows = get_db().execute(
        "SELECT date, price FROM price_history WHERE coingecko_id='btc-dominance' ORDER BY date"
    ).fetchall()
    return jsonify([{"date": r["date"], "pct": r["price"]} for r in rows])


@app.route("/api/history/monthly")
def api_monthly():
    """Dollars invested / withdrawn per calendar month, for the DCA chart."""
    db = get_db()
    rows = db.execute(
        """SELECT substr(date,1,7) ym,
                  SUM(CASE WHEN side='buy' THEN total ELSE 0 END) invested,
                  SUM(CASE WHEN side='sell' THEN total ELSE 0 END) proceeds
           FROM transactions GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/allocation")
def api_allocation_history():
    """Per-coin portfolio value over time (weekly samples) for the stacked
    allocation chart. Top coins by current value; the rest grouped as Other."""
    days = int(request.args.get("days", 365))
    db = get_db()
    _all_tx, price_maps, all_dates, txs_by_sym = load_holdings_axis(db, days)
    per_coin = {s: [] for s in price_maps}   # aligned with all_dates
    for d, coin_state in walk_holdings(price_maps, all_dates, txs_by_sym):
        for sym in price_maps:
            st = coin_state[sym]
            per_coin[sym].append(st["qty"] * st["px"] if st["px"] is not None else 0.0)
    # weekly samples (plus the most recent day) keep the chart light
    idx = list(range(0, len(all_dates), 7))
    if all_dates and idx[-1] != len(all_dates) - 1:
        idx.append(len(all_dates) - 1)
    # top 8 coins by latest value; everything else becomes "Other"
    finals = sorted(per_coin.items(), key=lambda kv: kv[1][-1] if kv[1] else 0, reverse=True)
    top = [s for s, v in finals[:8] if v and v[-1] > 0.5]
    # clamp at 0: closed positions can hold a few cents of negative dust
    series = [{"label": s.upper(), "values": [round(max(0.0, per_coin[s][i]), 2) for i in idx]} for s in top]
    rest = [s for s in per_coin if s not in top]
    if rest:
        other = [round(max(0.0, sum(per_coin[s][i] for s in rest)), 2) for i in idx]
        if max(other, default=0) > 0.5:
            series.append({"label": "Other", "values": other})
    return jsonify({"dates": [all_dates[i] for i in idx], "series": series})


@app.route("/api/history/btc")
def api_btc_history():
    """Full BTC daily history for the Fear & Greed overlay and long-term
    trend chart, via ensure_btc_backfill."""
    days = int(request.args.get("days", 4000))
    db = get_db()
    ensure_btc_backfill(db)
    get_history("bitcoin", 30)  # top up the most recent days
    start = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT date, price FROM price_history WHERE coingecko_id='bitcoin' AND date>=? ORDER BY date",
        (start,),
    ).fetchall()
    return jsonify([{"date": r["date"], "price": r["price"]} for r in rows])


# ---------------------------------------------------------------- backups

_icloud = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
BACKUP_DIR = (os.path.join(_icloud, "Crypto Tracker Backups")
              if os.path.isdir(_icloud) else os.path.join(APP_DIR, "backups"))


def do_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, "portfolio-{}.db".format(datetime.now().strftime("%Y%m%d-%H%M%S")))
    src = sqlite3.connect(DB_PATH, timeout=10)
    dst = sqlite3.connect(dest)
    src.backup(dst)  # consistent snapshot even while the app is writing
    dst.close()
    src.close()
    files = sorted(f for f in os.listdir(BACKUP_DIR)
                   if f.startswith("portfolio-") and f.endswith(".db"))
    for f in files[:-14]:  # keep the newest 14
        os.remove(os.path.join(BACKUP_DIR, f))
    return dest


def backup_loop():
    while True:
        try:
            do_backup()
        except Exception as e:
            print(f"[backup_loop] failed: {e!r}", flush=True)
        time.sleep(86400)  # daily


@app.route("/api/backup", methods=["GET", "POST"])
def api_backup():
    if request.method == "POST":
        dest = do_backup()
        return jsonify({"ok": True, "path": dest})
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith("portfolio-") and f.endswith(".db"))
    except OSError:
        files = []
    return jsonify({"dir": BACKUP_DIR, "count": len(files),
                    "last": files[-1] if files else None,
                    "icloud": "CloudDocs" in BACKUP_DIR})


REWARD_RE = re.compile(r"reward|interest|stak", re.I)

FNG_BUCKETS = [(0, 25, "Extreme Fear"), (26, 45, "Fear"), (46, 55, "Neutral"),
               (56, 75, "Greed"), (76, 100, "Extreme Greed")]


@app.route("/api/market/buy_sentiment")
def api_buy_sentiment():
    """Every buy scored against the Fear & Greed index on the day it happened.

    Staking rewards and interest are excluded - they arrive on a schedule, not
    a decision, and would pile into whatever sentiment happened to be running.
    Also reports how the dollars committed in each mood have actually done."""
    db = get_db()

    def fetch_fng():
        resp = requests.get("https://api.alternative.me/fng/?limit=0", timeout=20)
        resp.raise_for_status()
        return resp.json()

    try:  # same cache key the Market tab already fills, so this is usually free
        pts = cached_fetch("fng:all", 3600, fetch_fng, stale="serve").get("data", [])
    except Exception:
        return jsonify({"error": "Fear & Greed history unavailable right now."}), 503
    if not pts:
        return jsonify({"error": "No Fear & Greed history returned."}), 503

    hist = sorted((datetime.fromtimestamp(int(p["timestamp"])).strftime("%Y-%m-%d"),
                   int(p["value"])) for p in pts)
    dates = [d for d, _ in hist]
    values = [v for _, v in hist]

    def fng_on(day):
        """Index value on `day`, or the most recent reading before it."""
        i = bisect.bisect_right(dates, day) - 1
        return values[i] if i >= 0 else None

    market = get_market_data()
    price = {r["symbol"]: (market.get(r["coingecko_id"], {}).get("current_price") or 0.0)
             for r in db.execute("SELECT symbol, coingecko_id FROM coins")}

    buys = db.execute(
        """SELECT date, symbol, quantity, total, COALESCE(notes,'') notes
           FROM transactions WHERE side='buy' AND total > 0 ORDER BY date"""
    ).fetchall()

    rows = {b[2]: {"bucket": b[2], "lo": b[0], "hi": b[1], "n": 0,
                   "invested": 0.0, "value_now": 0.0} for b in FNG_BUCKETS}
    scored = skipped_rewards = no_reading = 0
    wsum = 0.0          # dollar-weighted sentiment
    plain = []          # per-buy readings, unweighted
    first_day = last_day = None
    for t in buys:
        if REWARD_RE.search(t["notes"]):
            skipped_rewards += 1
            continue
        v = fng_on(t["date"])
        if v is None:
            no_reading += 1
            continue
        label = next(b[2] for b in FNG_BUCKETS if b[0] <= v <= b[1])
        r = rows[label]
        r["n"] += 1
        r["invested"] += t["total"]
        r["value_now"] += t["quantity"] * price.get(t["symbol"], 0.0)
        scored += 1
        wsum += v * t["total"]
        plain.append(v)
        first_day = first_day or t["date"]
        last_day = t["date"]

    invested = sum(r["invested"] for r in rows.values())
    for r in rows.values():
        r["share"] = (r["invested"] / invested * 100) if invested else 0.0
        r["return_pct"] = ((r["value_now"] - r["invested"]) / r["invested"] * 100
                           if r["invested"] > 0 else None)
    # what the index averaged over the same span, for an honest comparison
    window = [v for d, v in hist if first_day and first_day <= d <= last_day]
    return jsonify({
        "buckets": [rows[b[2]] for b in FNG_BUCKETS],
        "buys_scored": scored,
        "rewards_excluded": skipped_rewards,
        "no_reading": no_reading,
        "invested": invested,
        "value_now": sum(r["value_now"] for r in rows.values()),
        "avg_fng_weighted": (wsum / invested) if invested else None,
        "avg_fng_simple": (sum(plain) / len(plain)) if plain else None,
        "avg_fng_period": (sum(window) / len(window)) if window else None,
        "first": first_day, "last": last_day,
    })


@app.route("/api/tax/preview")
def api_tax_preview():
    """Unrealized position by holding period: what a sale today would be taxed
    as, per coin, plus lots about to cross the one-year line. Read-only."""
    db = get_db()
    _, _, _, lots = compute_fifo(db)
    market = get_market_data()
    prices = {r["symbol"]: (market.get(r["coingecko_id"], {}).get("current_price") or 0.0)
              for r in db.execute("SELECT symbol, coingecko_id FROM coins")}
    view = lot_tax_view(lots, prices)
    view["as_of"] = date.today().isoformat()
    return jsonify(view)


@app.route("/api/tax/whatif")
def api_tax_whatif():
    """Hypothetical sale: FIFO-walk `qty` of `symbol` at today's price and
    report the gain split. Nothing is written - this never touches the ledger."""
    sym = request.args.get("symbol", "").strip().lower()
    try:
        qty = float(request.args.get("qty", "0"))
    except ValueError:
        return jsonify({"error": "Quantity must be a number."}), 400
    if not sym or qty <= 0:
        return jsonify({"error": "Pick a coin and a quantity above zero."}), 400
    db = get_db()
    _, _, _, lots = compute_fifo(db)
    if sym not in lots or not lots[sym]:
        return jsonify({"error": "No open lots recorded for that coin."}), 400
    row = db.execute("SELECT coingecko_id FROM coins WHERE symbol=?", (sym,)).fetchone()
    price = (get_market_data().get(row["coingecko_id"], {}).get("current_price") or 0.0) if row else 0.0
    if price <= 0:
        return jsonify({"error": "No live price for that coin right now."}), 400
    held = sum(q for q, _, _ in lots[sym])
    if qty > held + 1e-9:
        return jsonify({"error": "You only hold %.8f %s." % (held, sym.upper())}), 400
    out = whatif_sale(lots, sym, qty, price)
    out["held"] = held
    # selling happens on Coinbase, never from the cold wallet - warn, don't block
    out["warning"] = hot_balance_error(db, sym, qty)
    return jsonify(out)


@app.route("/api/export/realized")
def api_export_realized():
    year = request.args.get("year", "").strip()
    _, _, sales, _ = compute_fifo(get_db())
    rows = [s for s in sales if not year or s["date"].startswith(year)]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date Sold", "Coin", "Quantity", "Proceeds USD",
                "Cost Basis USD (FIFO)", "Gain/Loss USD", "First Lot Acquired"])
    for s in rows:
        w.writerow([s["date"], s["symbol"].upper(), "%.10f" % s["qty"],
                    "%.2f" % s["proceeds"], "%.2f" % s["cost"],
                    "%.2f" % s["gain"], s["first_acquired"]])
    name = "realized-gains{}.csv".format("-" + year if year else "-all")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + name})


@app.route("/api/export/tax8949")
def api_export_tax8949():
    """Form-8949-style export: one row per FIFO lot consumed by each sale,
    with per-lot acquisition dates and the short/long-term split. Proceeds are
    prorated across lots by quantity so per-lot gain sums to the sale's gain."""
    year = request.args.get("year", "").strip()
    _, _, sales, _ = compute_fifo(get_db())
    rows = [s for s in sales if not year or s["date"].startswith(year)]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Description", "Date Acquired", "Date Sold", "Term",
                "Proceeds USD", "Cost Basis USD", "Gain/Loss USD", "Note"])
    tot = {"Short": [0.0, 0.0], "Long": [0.0, 0.0]}   # [proceeds, cost]
    for s in rows:
        for take, unit_cost, acquired in s["lots"]:
            proceeds = s["proceeds"] * (take / s["qty"]) if s["qty"] else 0.0
            basis = take * unit_cost
            term = "Long" if _is_long_term(acquired, s["date"]) else "Short"
            tot[term][0] += proceeds
            tot[term][1] += basis
            w.writerow(["%.10g %s" % (take, s["symbol"].upper()), acquired,
                        s["date"], term, "%.2f" % proceeds, "%.2f" % basis,
                        "%.2f" % (proceeds - basis), ""])
        if s["uncovered"] > 1e-12:
            proceeds = s["proceeds"] * (s["uncovered"] / s["qty"]) if s["qty"] else 0.0
            tot["Short"][0] += proceeds
            w.writerow(["%.10g %s" % (s["uncovered"], s["symbol"].upper()),
                        "UNKNOWN", s["date"], "Short", "%.2f" % proceeds,
                        "0.00", "%.2f" % proceeds,
                        "no recorded buy lot (transfer/reward) - zero basis"])
    w.writerow([])
    for term in ("Short", "Long"):
        p, c = tot[term]
        w.writerow(["TOTAL %s-term" % term.upper(), "", "", term,
                    "%.2f" % p, "%.2f" % c, "%.2f" % (p - c), ""])
    name = "tax-8949{}.csv".format("-" + year if year else "-all")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + name})


@app.route("/api/history/portfolio")
def api_portfolio_history():
    """Portfolio value over time: cumulative holdings x daily prices."""
    days = int(request.args.get("days", 365))
    db = get_db()
    start = (date.today() - timedelta(days=days)).isoformat()
    all_tx, price_maps, all_dates, txs_by_sym = load_holdings_axis(db, days)
    # benchmarks: the same cash flows, but every dollar into one asset instead
    ensure_yahoo_backfill(db, "bitcoin", "BTC-USD")
    ensure_yahoo_backfill(db, "ethereum", "ETH-USD")
    ensure_yahoo_backfill(db, "index-spx", "^GSPC")   # S&P 500
    ensure_yahoo_backfill(db, "index-ndq", "^IXIC")   # Nasdaq Composite

    def price_lookup(cg_id):
        rows = db.execute(
            "SELECT date, price FROM price_history WHERE coingecko_id=? ORDER BY date",
            (cg_id,)).fetchall()
        ds = [r["date"] for r in rows]
        ps = [r["price"] for r in rows]

        def px(d):
            i = bisect.bisect_right(ds, d) - 1
            return ps[i] if i >= 0 else None
        return px

    benchmarks = {
        "bench": price_lookup("bitcoin"),
        "bench_eth": price_lookup("ethereum"),
        "bench_spx": price_lookup("index-spx"),
        "bench_ndq": price_lookup("index-ndq"),
    }
    bench_qty = {k: 0.0 for k in benchmarks}

    out = []
    running_cost = 0.0
    cost_i = 0
    for d, coin_state in walk_holdings(price_maps, all_dates, txs_by_sym):
        total = 0.0
        for sym in price_maps:
            st = coin_state[sym]
            if st["px"] is not None:
                total += st["qty"] * st["px"]
        while cost_i < len(all_tx) and all_tx[cost_i]["date"] <= d:
            t = all_tx[cost_i]
            signed = t["total"] if t["side"] == "buy" else -t["total"]
            running_cost += signed
            for k, px in benchmarks.items():
                p = px(t["date"])
                if p:
                    bench_qty[k] += signed / p
            cost_i += 1
        entry = {"date": d, "value": total, "cost": running_cost}
        for k, px in benchmarks.items():
            p = px(d)
            entry[k] = bench_qty[k] * p if p else None
        out.append(entry)
    return jsonify(out)


@app.route("/api/stablecoins")
def api_stablecoins():
    """Total stablecoin market cap history from DeFiLlama (free), cached daily.
    Rising supply = money parked on the sidelines - a liquidity signal."""
    def fetch():
        resp = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=30)
        resp.raise_for_status()
        out = []
        for p in resp.json():
            mcap = (p.get("totalCirculatingUSD") or {}).get("peggedUSD")
            if mcap:
                out.append({"date": datetime.fromtimestamp(int(p["date"]), tz=timezone.utc).date().isoformat(),
                            "mcap": round(mcap)})
        return out

    try:
        return jsonify(cached_fetch("stablecoins", 86400, fetch))
    except Exception:
        return jsonify([])


# ---------------------------------------------------------------- market indicators

@app.route("/api/market")
def api_market():
    out = {}
    try:
        g_ = cg_get("/global", ttl=300)["data"]
        out["global"] = {
            "total_market_cap": g_["total_market_cap"]["usd"],
            "total_volume": g_["total_volume"]["usd"],
            "btc_dominance": g_["market_cap_percentage"]["btc"],
            "eth_dominance": g_["market_cap_percentage"]["eth"],
            "market_cap_change_24h": g_["market_cap_change_percentage_24h_usd"],
        }
    except Exception:
        out["global"] = None
    if out["global"]:
        # self-recorded BTC dominance series (no free historical source exists)
        try:
            gdb = get_db()
            gdb.execute("INSERT OR REPLACE INTO price_history (coingecko_id, date, price) "
                        "VALUES ('btc-dominance', ?, ?)",
                        (date.today().isoformat(), out["global"]["btc_dominance"]))
            gdb.commit()
        except Exception:
            pass
    try:
        t = cg_get("/search/trending", ttl=600)
        out["trending"] = [
            {
                "name": i["item"]["name"],
                "symbol": i["item"]["symbol"],
                "rank": i["item"]["market_cap_rank"],
                "price": (i["item"].get("data") or {}).get("price"),
                "change_24h": ((i["item"].get("data") or {}).get("price_change_percentage_24h") or {}).get("usd"),
                "thumb": i["item"].get("thumb"),
            }
            for i in t.get("coins", [])
        ]
    except Exception:
        out["trending"] = []
    # Fear & Greed from alternative.me (same source as the spreadsheet)
    def fetch_fng():
        # limit=0 -> full history (back to 2018), so the UI can offer long ranges
        resp = requests.get("https://api.alternative.me/fng/?limit=0", timeout=20)
        resp.raise_for_status()
        return resp.json()

    try:
        fng = cached_fetch("fng:all", 3600, fetch_fng, stale="raise")
        pts = fng.get("data", [])
        out["fear_greed"] = {
            "value": int(pts[0]["value"]),
            "label": pts[0]["value_classification"],
            "history": [
                {"date": datetime.fromtimestamp(int(p["timestamp"])).strftime("%Y-%m-%d"),
                 "value": int(p["value"])}
                for p in reversed(pts)
            ],
        } if pts else None
    except Exception:
        out["fear_greed"] = None
    # BTC network fees (mempool.space) - useful when planning cold-wallet moves
    try:
        out["btc_fees"] = cached_fetch("btcfees", 600, lambda: requests.get(
            "https://mempool.space/api/v1/fees/recommended", timeout=15).json(), stale="raise")
    except Exception:
        out["btc_fees"] = None
    # halving countdown from current block height (every 210,000 blocks)
    try:
        # cached as a bare int, so round-trip it back through int()
        height = int(cached_fetch("btctip", 3600, lambda: int(requests.get(
            "https://mempool.space/api/blocks/tip/height", timeout=15).text), stale="raise"))
        remaining = (height // 210000 + 1) * 210000 - height
        est = datetime.now() + timedelta(minutes=10 * remaining)
        out["halving"] = {"height": height, "blocks_remaining": remaining,
                          "days": remaining * 10 // 1440,
                          "estimated_date": est.strftime("%Y-%m-%d")}
    except Exception:
        out["halving"] = None
    return jsonify(out)


# ---------------------------------------------------------------- price alerts

@app.route("/api/alerts", methods=["GET", "POST"])
def api_alerts():
    db = get_db()
    if request.method == "POST":
        d = request.get_json(force=True)
        sym = d["symbol"].strip().lower()
        if not db.execute("SELECT 1 FROM coins WHERE symbol=?", (sym,)).fetchone():
            return jsonify({"error": "Unknown coin '{}'.".format(sym)}), 400
        try:
            price = float(d["price"])
            if price <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "Price must be a positive number."}), 400
        if d.get("condition") not in ("above", "below"):
            return jsonify({"error": "Bad condition."}), 400
        db.execute("INSERT INTO alerts (symbol, condition, price, active, created) VALUES (?,?,?,1,?)",
                   (sym, d["condition"], price, date.today().isoformat()))
        db.commit()
        return jsonify({"ok": True})
    rows = db.execute(
        "SELECT a.*, c.name FROM alerts a JOIN coins c ON c.symbol=a.symbol "
        "ORDER BY a.active DESC, a.id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def api_alert_delete(alert_id):
    db = get_db()
    db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    db.commit()
    return jsonify({"ok": True})


def mac_notify(message):
    subprocess.run(
        ["osascript", "-e",
         'display notification "{}" with title "Crypto Tracker" sound name "Glass"'.format(
             message.replace('"', "'"))],
        timeout=10, capture_output=True)


def alerts_loop():
    """Check active alerts every 5 minutes; fire a macOS notification once
    when a threshold is crossed, then deactivate that alert."""
    while True:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT a.*, c.coingecko_id FROM alerts a JOIN coins c ON c.symbol=a.symbol "
                "WHERE a.active=1").fetchall()
            if rows:
                ids = ",".join(sorted({r["coingecko_id"] for r in rows}))
                px = requests.get(COINGECKO + "/simple/price",
                                  params={"ids": ids, "vs_currencies": "usd"}, timeout=20).json()
                for r in rows:
                    p = (px.get(r["coingecko_id"]) or {}).get("usd")
                    if p is None:
                        continue
                    hit = p >= r["price"] if r["condition"] == "above" else p <= r["price"]
                    if hit:
                        try:
                            mac_notify("{} is ${:,.2f} - {} your ${:,.0f} alert".format(
                                r["symbol"].upper(), p, r["condition"], r["price"]))
                        except Exception:
                            pass
                        conn.execute("UPDATE alerts SET active=0, triggered_at=? WHERE id=?",
                                     (datetime.now().strftime("%Y-%m-%d %H:%M"), r["id"]))
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"[alerts_loop] failed: {e!r}", flush=True)
        time.sleep(300)


# ---------------------------------------------------------------- main

auth.register(app)      # CSRF + login hooks and /login, /logout, passkeys
stocks.register(app)    # /api/stocks*
cb.register(app)        # /api/coinbase/*

init_db()
app.secret_key = get_setting("secret_key")
app.permanent_session_lifetime = timedelta(days=30)  # stay signed in for 30 days

if __name__ == "__main__":
    threading.Thread(target=backup_loop, daemon=True).start()      # daily backups
    threading.Thread(target=cb.cb_sync_loop, daemon=True).start()  # Coinbase sync every 6h
    threading.Thread(target=alerts_loop, daemon=True).start()      # price alerts every 5 min
    # 0.0.0.0 = reachable from other devices on the home network,
    # e.g. http://<this-macs-name>.local:5178
    app.run(host="0.0.0.0", port=5178, debug=False)
