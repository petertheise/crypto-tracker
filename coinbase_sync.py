"""Coinbase sync: view-only API client, balance checks, ledger sync and its loop.

Named coinbase_sync (not coinbase) so it does not shadow the coinbase SDK package."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from flask import jsonify

from db import APP_DIR, DB_PATH, get_db, get_setting
from market_data import get_market_data
from portfolio_math import get_balances

CB_KEY_PATH = os.path.join(APP_DIR, "coinbase_key.json")


def cb_client():
    if not os.path.exists(CB_KEY_PATH):
        return None
    from coinbase.rest import RESTClient
    k = json.load(open(CB_KEY_PATH))
    return RESTClient(api_key=k["name"], api_secret=k["privateKey"])


def cb_pages(client, path):
    """Yield successive data pages from a paginated Coinbase v2 endpoint,
    following starting_after/next_uri until the last page."""
    starting_after = None
    while True:
        params = {"limit": "100"}
        if starting_after:
            params["starting_after"] = starting_after
        res = client.get(path, params=params)
        data = res.get("data", [])
        yield data
        if not data or not (res.get("pagination") or {}).get("next_uri"):
            break
        starting_after = data[-1]["id"]


def cb_balances(client):
    """All Coinbase balances per coin, including staked wallets and vaults
    (v2 accounts - the v3 endpoint hides staked positions)."""
    out = {}
    for page in cb_pages(client, "/v2/accounts"):
        for a in page:
            bal = float(a["balance"]["amount"])
            if bal > 1e-9:
                cur = a["currency"]["code"].lower()
                out[cur] = out.get(cur, 0.0) + bal
    return out


def coinbase_sync(conn):
    """Pull new Coinbase activity (buys, sells, converts, rewards, sends)
    into the ledger. Only transactions after the baseline (cb_sync_since)
    are considered; every imported id is remembered so nothing duplicates."""
    client = cb_client()
    if client is None:
        return {"error": "No coinbase_key.json in the app folder."}
    row = conn.execute("SELECT value FROM settings WHERE key='cb_sync_since'").fetchone()
    if row:
        since = row[0]
    else:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("INSERT INTO settings (key, value) VALUES ('cb_sync_since', ?)", (since,))
    known = {r[0] for r in conn.execute("SELECT symbol FROM coins")}
    seen = {r[0] for r in conn.execute("SELECT cb_id FROM cb_synced")}
    imported = {"buys": 0, "sells": 0, "transfers": 0, "ignored": 0}
    warnings = set()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def mark(tid, kind, local_id):
        conn.execute("INSERT OR IGNORE INTO cb_synced (cb_id, local_kind, local_id, synced_at) "
                     "VALUES (?,?,?,?)", (tid, kind, local_id, now_iso))
        seen.add(tid)

    def handle(t):
        tid = t["id"]
        if tid in seen:
            return
        if t.get("status") != "completed":
            return  # pending: revisit on a later sync
        cur = t["amount"]["currency"].lower()
        amt = float(t["amount"]["amount"])
        native = abs(float(t["native_amount"]["amount"])) if t.get("native_amount") else 0.0
        typ = t.get("type", "")
        d = t.get("created_at", "")[:10]
        if cur == "usd" or abs(amt) < 1e-12:
            mark(tid, "skip", None)
            imported["ignored"] += 1
            return
        trade_types = {"buy", "sell", "trade", "advanced_trade_fill",
                       "staking_reward", "interest", "inflation_reward", "reward"}
        if typ in trade_types:
            if cur not in known:
                warnings.add("Unknown coin {} - add it on the Coins tab, then sync again.".format(cur.upper()))
                return  # not marked: retried next sync
            side = "buy" if amt > 0 else "sell"
            qty = abs(amt)
            label = {"trade": "convert", "advanced_trade_fill": "trade",
                     "staking_reward": "staking reward"}.get(typ, typ)
            c = conn.execute(
                "INSERT INTO transactions (date,symbol,side,quantity,price,fee,total,exchange,notes) "
                "VALUES (?,?,?,?,?,0,?,'COINBASE',?)",
                (d, cur, side, qty, native / qty if qty else 0, native,
                 "Coinbase sync: " + label))
            mark(tid, "tx", c.lastrowid)
            imported["buys" if side == "buy" else "sells"] += 1
            return
        if typ == "send":
            if cur not in known:
                warnings.add("Unknown coin {} - add it on the Coins tab, then sync again.".format(cur.upper()))
                return
            direction = "to_cold" if amt < 0 else "from_cold"
            note = ("Coinbase sync: sent off Coinbase - assumed cold wallet, verify"
                    if amt < 0 else "Coinbase sync: received to Coinbase - verify source")
            c = conn.execute(
                "INSERT INTO transfers (date,symbol,quantity,direction,notes) VALUES (?,?,?,?,?)",
                (d, cur, abs(amt), direction, note))
            mark(tid, "transfer", c.lastrowid)
            imported["transfers"] += 1
            return
        mark(tid, "skip", None)  # fiat movements etc.
        imported["ignored"] += 1

    try:
        # all v2 accounts (paginated)
        accounts = []
        for page in cb_pages(client, "/v2/accounts"):
            accounts += page
        for acct in accounts:
            cur = acct["currency"]["code"].lower()
            if cur not in known and float(acct["balance"]["amount"]) <= 0:
                continue
            done = False
            for data in cb_pages(client, "/v2/accounts/{}/transactions".format(acct["id"])):
                for t in data:
                    if t.get("created_at", "") < since:
                        done = True
                        break
                    handle(t)
                if done:
                    break
    except Exception as e:
        conn.commit()
        return {"error": "Coinbase API error: {}".format(str(e)[:200]), "imported": imported}
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cb_last_sync', ?)", (now_iso,))
    conn.commit()
    return {"ok": True, "imported": imported, "warnings": sorted(warnings), "since": since}


def cb_sync_loop():
    while True:
        time.sleep(6 * 3600)  # every 6 hours
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            coinbase_sync(conn)
            conn.close()
        except Exception as e:
            print(f"[cb_sync_loop] failed: {e!r}", flush=True)


def api_cb_status():
    client = cb_client()
    if client is None:
        return jsonify({"connected": False})
    db = get_db()
    out = {"connected": True,
           "last_sync": get_setting("cb_last_sync"),
           "since": get_setting("cb_sync_since")}
    try:
        cb = cb_balances(client)
        market = get_market_data()
        prices = {r["symbol"]: (market.get(r["coingecko_id"], {}) or {}).get("current_price")
                  for r in db.execute("SELECT symbol, coingecko_id FROM coins")}
        app_syms = [r[0] for r in db.execute("SELECT symbol FROM coins")]
        checks = []
        for sym in sorted(set(app_syms) | set(cb.keys())):
            if sym == "usd":
                continue
            total, cold = get_balances(db, sym)
            hot = total - cold
            actual = cb.get(sym, 0.0)
            px = prices.get(sym)
            # hide dust rows: both sides under $1 (or negligible qty if unpriced)
            if px:
                if abs(hot) * px < 1 and actual * px < 1:
                    continue
            elif abs(hot) < 0.01 and actual < 0.01:
                continue
            diff = abs(hot - actual)
            ok = diff < 1e-6 or (px is not None and diff * px < 1.0)
            checks.append({"symbol": sym.upper(), "app_hot": hot, "coinbase": actual, "ok": ok})
        out["balances"] = checks
    except Exception as e:
        out["balance_error"] = str(e)[:200]
    return jsonify(out)


def api_cb_sync():
    return jsonify(coinbase_sync(get_db()))


def register(app):
    app.add_url_rule("/api/coinbase/status", view_func=api_cb_status)
    app.add_url_rule("/api/coinbase/sync", view_func=api_cb_sync, methods=["POST"])
