"""Crypto Portfolio Tracker - local web app.

Run:  ./venv/bin/python app.py   (or double-click "Start Crypto Tracker.command")
Data: portfolio.db (SQLite, lives next to this file)
APIs: CoinGecko (prices, free tier) and alternative.me (Fear & Greed).
"""
import os
import io
import re
import csv
import sqlite3
import time
import json
import bisect
import secrets
import threading
import subprocess
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify, request, render_template, g, session, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "portfolio.db")
COINGECKO = "https://api.coingecko.com/api/v3"

try:
    os.chdir(APP_DIR)
except OSError:
    pass

app = Flask(__name__, root_path=APP_DIR, instance_path=os.path.join(APP_DIR, "instance"))
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    symbol      TEXT PRIMARY KEY,        -- lowercase ticker, e.g. 'btc'
    coingecko_id TEXT NOT NULL,          -- e.g. 'bitcoin'
    name        TEXT NOT NULL,
    category    TEXT DEFAULT 'Other'     -- Core / AI / Infra / Meme / Other
);
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,           -- YYYY-MM-DD
    symbol      TEXT NOT NULL REFERENCES coins(symbol),
    side        TEXT NOT NULL CHECK (side IN ('buy','sell')),
    quantity    REAL NOT NULL,           -- always positive
    price       REAL NOT NULL,           -- USD per coin
    fee         REAL DEFAULT 0,
    total       REAL NOT NULL,           -- USD: cost for buys, proceeds for sells
    exchange    TEXT DEFAULT '',
    notes       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS price_cache (
    coingecko_id TEXT PRIMARY KEY,
    data        TEXT NOT NULL,           -- JSON blob from /coins/markets
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    coingecko_id TEXT NOT NULL,
    date        TEXT NOT NULL,           -- YYYY-MM-DD
    price       REAL NOT NULL,
    PRIMARY KEY (coingecko_id, date)
);
CREATE TABLE IF NOT EXISTS api_cache (
    key         TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    done        INTEGER DEFAULT 0,
    created     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    symbol      TEXT NOT NULL REFERENCES coins(symbol),
    quantity    REAL NOT NULL,           -- always positive
    direction   TEXT NOT NULL CHECK (direction IN ('to_cold','from_cold')),
    notes       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cb_synced (
    cb_id       TEXT PRIMARY KEY,        -- Coinbase transaction id, for dedupe
    local_kind  TEXT,                    -- 'tx', 'transfer' or 'skip'
    local_id    INTEGER,
    synced_at   TEXT
);
CREATE TABLE IF NOT EXISTS stocks (
    symbol      TEXT NOT NULL,           -- ticker, or CASH for sweep balances
    account     TEXT NOT NULL DEFAULT '',-- which RJ portfolio holds it
    name        TEXT NOT NULL,
    product_type TEXT DEFAULT '',
    quantity    REAL NOT NULL,
    invested    REAL DEFAULT 0,          -- Amount Invested from the RJ export
    income      REAL DEFAULT 0,          -- Estimated Annual Income
    rj_price    REAL DEFAULT 0,          -- price from the export, fallback if Yahoo lacks it
    PRIMARY KEY (symbol, account)
);
CREATE TABLE IF NOT EXISTS passkeys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id TEXT UNIQUE NOT NULL,  -- base64url
    public_key  TEXT NOT NULL,           -- base64url
    sign_count  INTEGER DEFAULT 0,
    device_name TEXT DEFAULT '',
    created     TEXT NOT NULL,
    last_used   TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL REFERENCES coins(symbol),
    condition   TEXT NOT NULL CHECK (condition IN ('above','below')),
    price       REAL NOT NULL,
    active      INTEGER DEFAULT 1,       -- one-shot: goes 0 when triggered
    created     TEXT NOT NULL,
    triggered_at TEXT
);
"""


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    # WAL: three background writer threads + threaded requests share this file;
    # without it a colliding commit raises 'database is locked'.
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    # secret key for session cookies, generated once
    if not db.execute("SELECT 1 FROM settings WHERE key='secret_key'").fetchone():
        db.execute("INSERT INTO settings (key, value) VALUES ('secret_key', ?)",
                   (secrets.token_hex(32),))
    db.commit()
    db.close()


def get_setting(key):
    db = sqlite3.connect(DB_PATH, timeout=10)
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    db.close()
    return row[0] if row else None


# ---------------------------------------------------------------- auth

@app.before_request
def block_cross_site_writes():
    """CSRF guard: browsers attach an Origin/Referer header to requests a web
    page triggers. Writes must come from this app's own pages - a request
    started by some other website gets rejected. Requests without either
    header (curl, scripts) are allowed; browsers always send one cross-site."""
    if request.method in ("POST", "PUT", "DELETE"):
        src = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if not src:
            return
        # own address, the address a reverse proxy (tailscale serve) forwarded
        # for, or any tailnet HTTPS name - only Peter's tailnet can reach this
        ok_hosts = {request.host, request.headers.get("X-Forwarded-Host", "")}
        netloc = urlsplit(src).netloc
        if src == "null" or (netloc not in ok_hosts
                             and not netloc.split(":")[0].endswith(".ts.net")):
            return jsonify({"error": "cross-site request blocked"}), 403


SHORT_SESSION_IDLE = 30 * 60   # mobile: sign in again after 30 min idle


def finish_login(short):
    """Set up the session after a successful password or passkey sign-in.
    Mobile devices get a short sliding session; desktops keep the 30-day one."""
    user_row = get_setting("username") or "peter"
    session.permanent = True
    session["user"] = user_row
    if short:
        session["short"] = True
        session["exp"] = time.time() + SHORT_SESSION_IDLE
    else:
        session.pop("short", None)
        session.pop("exp", None)


@app.before_request
def require_login():
    if (request.path.startswith("/static/")
            or request.path in ("/login", "/favicon.ico",
                                "/api/passkey/auth/options", "/api/passkey/auth/verify")):
        return
    if not get_db().execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone():
        return  # no account set up yet -> app stays open (localhost-style)
    if session.get("user"):
        if not session.get("short"):
            return
        if time.time() <= session.get("exp", 0):
            session["exp"] = time.time() + SHORT_SESSION_IDLE  # sliding window
            return
        session.clear()  # idle too long on a mobile device -> re-auth
    if request.path.startswith("/api/"):
        return jsonify({"error": "auth required"}), 401
    return redirect("/login")


_login_failures = {"count": 0, "locked_until": 0.0}  # in-memory brute-force lockout


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
    if not row:
        return redirect("/")
    error = None
    if request.method == "POST":
        if time.time() < _login_failures["locked_until"]:
            error = "Too many wrong attempts — wait a minute and try again."
            return render_template("login.html", error=error)
        user_row = db.execute("SELECT value FROM settings WHERE key='username'").fetchone()
        username = user_row["value"] if user_row else "peter"
        if (request.form.get("username", "").strip().lower() == username
                and check_password_hash(row["value"], request.form.get("password", ""))):
            _login_failures.update(count=0, locked_until=0.0)
            finish_login(short=request.form.get("mobile") == "1")
            return redirect("/")
        time.sleep(0.7)  # slow down password guessing
        _login_failures["count"] += 1
        if _login_failures["count"] >= 5:  # 5 misses -> 60s lockout
            _login_failures.update(count=0, locked_until=time.time() + 60)
        error = "Wrong username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/change_password", methods=["POST"])
def api_change_password():
    d = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
    if not row or not check_password_hash(row["value"], d.get("current", "")):
        return jsonify({"error": "Current password is incorrect."}), 400
    new = d.get("new", "")
    if len(new) < 6:
        return jsonify({"error": "New password must be at least 6 characters."}), 400
    # pbkdf2: the default (scrypt) is missing from this Mac's Python build
    db.execute("UPDATE settings SET value=? WHERE key='password_hash'",
               (generate_password_hash(new, method="pbkdf2:sha256:600000"),))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- passkeys (Face ID / Touch ID)

def _webauthn_ctx():
    """Relying-party id + origin from the request (works behind tailscale serve)."""
    host = request.headers.get("Host", request.host)
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return host.split(":")[0], "{}://{}".format(proto, host)


@app.route("/api/passkey/register/options", methods=["POST"])
def api_pk_reg_options():
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes, options_to_json
    from webauthn.helpers.structs import (PublicKeyCredentialDescriptor,
        AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement)
    db = get_db()
    rp_id, _ = _webauthn_ctx()
    user = get_setting("username") or "peter"
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
               for r in db.execute("SELECT credential_id FROM passkeys")]
    opts = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name="Crypto Tracker",
        user_id=user.encode(), user_name=user, user_display_name=user.title(),
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED))
    session["pk_challenge"] = bytes_to_base64url(opts.challenge)
    return Response(options_to_json(opts), mimetype="application/json")


@app.route("/api/passkey/register/verify", methods=["POST"])
def api_pk_reg_verify():
    import webauthn
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    d = request.get_json(force=True)
    rp_id, origin = _webauthn_ctx()
    challenge = session.pop("pk_challenge", "")
    if not challenge:
        return jsonify({"error": "No registration in progress - try again."}), 400
    try:
        v = webauthn.verify_registration_response(
            credential=d["credential"],
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id, expected_origin=origin)
    except Exception as e:
        return jsonify({"error": "Registration failed: " + str(e)[:150]}), 400
    db = get_db()
    db.execute("INSERT OR REPLACE INTO passkeys (credential_id, public_key, sign_count, device_name, created) "
               "VALUES (?,?,?,?,?)",
               (bytes_to_base64url(v.credential_id), bytes_to_base64url(v.credential_public_key),
                v.sign_count, (d.get("device_name") or "Device")[:40],
                datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/passkey/auth/options", methods=["POST"])
def api_pk_auth_options():
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
    db = get_db()
    creds = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
             for r in db.execute("SELECT credential_id FROM passkeys")]
    if not creds:
        return jsonify({"error": "No passkeys registered yet - sign in with your password, "
                                 "then add this device in Settings."}), 400
    rp_id, _ = _webauthn_ctx()
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id, allow_credentials=creds,
        user_verification=UserVerificationRequirement.REQUIRED)
    session["pk_challenge"] = bytes_to_base64url(opts.challenge)
    return Response(options_to_json(opts), mimetype="application/json")


@app.route("/api/passkey/auth/verify", methods=["POST"])
def api_pk_auth_verify():
    import webauthn
    from webauthn.helpers import base64url_to_bytes
    d = request.get_json(force=True)
    cred = d.get("credential") or {}
    db = get_db()
    row = db.execute("SELECT * FROM passkeys WHERE credential_id=?",
                     (cred.get("id", ""),)).fetchone()
    challenge = session.pop("pk_challenge", "")
    if not row or not challenge:
        time.sleep(0.5)
        return jsonify({"error": "Unknown passkey or no sign-in in progress."}), 400
    rp_id, origin = _webauthn_ctx()
    try:
        v = webauthn.verify_authentication_response(
            credential=cred,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=base64url_to_bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"])
    except Exception as e:
        time.sleep(0.5)
        return jsonify({"error": "Sign-in failed: " + str(e)[:150]}), 400
    db.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?",
               (v.new_sign_count, datetime.now().strftime("%Y-%m-%d %H:%M"), row["id"]))
    db.commit()
    finish_login(short=bool(d.get("mobile")))
    return jsonify({"ok": True})


@app.route("/api/passkey/list")
def api_pk_list():
    rows = get_db().execute(
        "SELECT id, device_name, created, last_used FROM passkeys ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/passkey/<int:pk_id>", methods=["DELETE"])
def api_pk_delete(pk_id):
    db = get_db()
    db.execute("DELETE FROM passkeys WHERE id=?", (pk_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API helpers

def cached_fetch(key, ttl, fetch_fn, stale="serve"):
    """Return the cached JSON for key if it is fresher than ttl, else call
    fetch_fn(), store the result and return it. stale= picks what happens when
    the fetch fails and we do have an older row: 'serve' hands the old data
    back as-is, 'touch' hands it back and re-stamps it so the next attempt
    waits a full ttl, 'raise' lets the error through. With no cached row at all
    the error always propagates."""
    db = get_db()
    row = db.execute("SELECT data, updated_at FROM api_cache WHERE key=?", (key,)).fetchone()
    if row and time.time() - row["updated_at"] < ttl:
        return json.loads(row["data"])
    try:
        data = fetch_fn()
    except Exception:
        if row is None or stale == "raise":
            raise
        data = json.loads(row["data"])
        if stale == "serve":
            return data
    db.execute("INSERT OR REPLACE INTO api_cache (key,data,updated_at) VALUES (?,?,?)",
               (key, json.dumps(data), time.time()))
    db.commit()
    return data


def cg_get(path, params=None, cache_key=None, ttl=60):
    """GET from CoinGecko with a small SQLite cache to respect rate limits."""
    db = get_db()
    key = cache_key or (path + "?" + json.dumps(params or {}, sort_keys=True))
    row = db.execute("SELECT data, updated_at FROM api_cache WHERE key=?", (key,)).fetchone()
    if row and time.time() - row["updated_at"] < ttl:
        return json.loads(row["data"])
    try:
        resp = requests.get(COINGECKO + path, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if row:  # network/rate-limit trouble: serve stale data rather than fail
            return json.loads(row["data"])
        raise
    db.execute(
        "INSERT OR REPLACE INTO api_cache (key, data, updated_at) VALUES (?,?,?)",
        (key, json.dumps(data), time.time()),
    )
    db.commit()
    return data


def get_market_data():
    """Live market rows for every coin in the coins table, cached ~90s."""
    db = get_db()
    ids = [r["coingecko_id"] for r in db.execute("SELECT DISTINCT coingecko_id FROM coins")]
    if not ids:
        return {}
    data = cg_get(
        "/coins/markets",
        {"vs_currency": "usd", "ids": ",".join(ids), "price_change_percentage": "1h,24h,7d,30d,1y"},
        cache_key="markets:" + ",".join(sorted(ids)),
        ttl=150,  # > the 120s dashboard poll, so ticks hit cache instead of CoinGecko
    )
    return {row["id"]: row for row in data}


def get_history(cg_id, days=365):
    """Daily closing prices for a coin. Past days come from the local DB;
    only missing recent days are fetched from the API."""
    db = get_db()
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    have = db.execute(
        "SELECT MAX(date) m FROM price_history WHERE coingecko_id=? AND date<?",
        (cg_id, today),
    ).fetchone()["m"]
    need_days = days if not have else min(days, (date.today() - date.fromisoformat(have)).days + 1)
    if need_days > 0:
        try:
            data = cg_get(
                "/coins/{}/market_chart".format(cg_id),
                {"vs_currency": "usd", "days": min(need_days, 365), "interval": "daily"},
                cache_key="chart:{}:{}".format(cg_id, min(need_days, 365)),
                ttl=3600,
            )
            rows = [
                (cg_id, datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat(), price)
                for ts, price in data.get("prices", [])
            ]
            db.executemany(
                "INSERT OR REPLACE INTO price_history (coingecko_id, date, price) VALUES (?,?,?)",
                rows,
            )
            db.commit()
        except Exception:
            pass  # fall back to whatever history we have stored
    return db.execute(
        "SELECT date, price FROM price_history WHERE coingecko_id=? AND date>=? ORDER BY date",
        (cg_id, start),
    ).fetchall()


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
    realized_by_year, open_cost, _sales = compute_fifo(db)
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

def get_balances(db, sym):
    """Returns (total holdings, in cold storage) for a coin. What's sellable
    on Coinbase is total - cold: sells never happen from the cold wallet."""
    total = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END),0) "
        "FROM transactions WHERE symbol=?", (sym,)).fetchone()[0]
    cold = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN direction='to_cold' THEN quantity ELSE -quantity END),0) "
        "FROM transfers WHERE symbol=?", (sym,)).fetchone()[0]
    return total, cold


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
            total_bal, cold = get_balances(db, symbol)
            available = total_bal - cold
            if qty > available + 1e-9:
                return jsonify({"error":
                    "Only {:.8f} {} is on Coinbase ({:.8f} is in cold storage). "
                    "Record a transfer back from the cold wallet first.".format(
                        max(0.0, available), symbol.upper(), max(0.0, cold))}), 400
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
    if from_qty > (bal - cold) + 1e-9:
        return jsonify({"error":
            "Only {:.8f} {} is on Coinbase ({:.8f} is in cold storage). "
            "Record a transfer back from the cold wallet first.".format(
                max(0.0, bal - cold), frm.upper(), max(0.0, cold))}), 400
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
        total = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END),0) "
            "FROM transactions WHERE symbol=?", (sym,)).fetchone()[0]
        cold = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='to_cold' THEN quantity ELSE -quantity END),0) "
            "FROM transfers WHERE symbol=?", (sym,)).fetchone()[0]
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

# Yahoo tickers for deep history (CoinGecko free tier stops at 365 days)
YAHOO_CRYPTO = {
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "solana": "SOL-USD", "ripple": "XRP-USD",
    "ondo-finance": "ONDO-USD", "render-token": "RENDER-USD", "bittensor": "TAO22974-USD",
    "fetch-ai": "FET-USD", "hedera-hashgraph": "HBAR-USD", "internet-computer": "ICP-USD",
    "matic-network": "POL28321-USD", "aptos": "APT21794-USD", "sei-network": "SEI-USD",
    "pepe": "PEPE24478-USD", "shiba-inu": "SHIB-USD", "cosmos": "ATOM-USD",
    "usd-coin": "USDC-USD", "quant-network": "QNT-USD", "pyth-network": "PYTH-USD",
}


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


def load_holdings_axis(db, days):
    """Shared setup for the value-over-time charts: per-coin daily price
    lookups for coins we hold, the union date axis, and transactions grouped by
    coin. A coin may be missing recent dates if the price API was rate-limited,
    so the walk below carries its last known price forward."""
    coins = db.execute(
        """SELECT c.symbol, c.coingecko_id FROM coins c
           WHERE EXISTS (SELECT 1 FROM transactions t WHERE t.symbol=c.symbol)"""
    ).fetchall()
    all_tx = db.execute(
        "SELECT date, symbol, side, quantity, total FROM transactions ORDER BY date"
    ).fetchall()
    price_maps = {}
    for c in coins:
        hist = get_history(c["coingecko_id"], days)
        if hist:
            price_maps[c["symbol"]] = {r["date"]: r["price"] for r in hist}
    all_dates = sorted({d for pm in price_maps.values() for d in pm})
    txs_by_sym = {}
    for t in all_tx:
        txs_by_sym.setdefault(t["symbol"], []).append(t)
    return all_tx, price_maps, all_dates, txs_by_sym


def walk_holdings(price_maps, all_dates, txs_by_sym):
    """Walk the date axis once, advancing each coin's transaction pointer and
    carrying its last known price forward. Yields (date, coin_state) where
    coin_state[sym] is that day's live {"i","qty","px"} dict. Callers decide
    what to accumulate - keep their per-coin order as price_maps' order."""
    coin_state = {s: {"i": 0, "qty": 0.0, "px": None} for s in price_maps}
    for d in all_dates:
        for sym, pm in price_maps.items():
            st = coin_state[sym]
            txs = txs_by_sym.get(sym, [])
            while st["i"] < len(txs) and txs[st["i"]]["date"] <= d:
                t = txs[st["i"]]
                st["qty"] += t["quantity"] if t["side"] == "buy" else -t["quantity"]
                st["i"] += 1
            if d in pm:
                st["px"] = pm[d]
        yield d, coin_state


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


def ensure_yahoo_backfill(db, cg_id, yahoo_symbol):
    """Daily closes from Yahoo Finance (keyless): a one-time 10y backfill,
    plus a top-up when the series goes stale (needed for stock indices,
    which CoinGecko doesn't refresh)."""
    earliest = db.execute(
        "SELECT MIN(date) m FROM price_history WHERE coingecko_id=?", (cg_id,)
    ).fetchone()["m"]
    latest = db.execute(
        "SELECT MAX(date) m FROM price_history WHERE coingecko_id=?", (cg_id,)
    ).fetchone()["m"]
    # don't re-attempt a full backfill more than weekly: young coins simply
    # don't have 2018 data, so "earliest > 2018" alone would refetch forever
    attempted = db.execute("SELECT updated_at FROM api_cache WHERE key=?",
                           ("ybf:" + cg_id,)).fetchone()
    fresh_attempt = attempted and time.time() - attempted[0] < 7 * 86400
    if (earliest is None or earliest > "2018-02-01") and not fresh_attempt:
        span = "10y"
        db.execute("INSERT OR REPLACE INTO api_cache (key,data,updated_at) VALUES (?,?,?)",
                   ("ybf:" + cg_id, "1", time.time()))
        db.commit()
    elif latest and latest < (date.today() - timedelta(days=4)).isoformat():
        span = "3mo"
    else:
        return
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/" + yahoo_symbol,
            params={"range": span, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},  # Yahoo rejects default UA
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        rows = [
            (cg_id, datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(), px)
            for ts, px in zip(result["timestamp"], closes) if px
        ]
        # IGNORE: keep the CoinGecko values we already have for recent days
        db.executemany(
            "INSERT OR IGNORE INTO price_history (coingecko_id, date, price) VALUES (?,?,?)",
            rows,
        )
        db.commit()
    except Exception:
        pass  # charts simply cover less history if the backfill fails


def ensure_btc_backfill(db):
    ensure_yahoo_backfill(db, "bitcoin", "BTC-USD")


def compute_fifo(db):
    """FIFO lot matching. Returns (realized gain by sale year, remaining open
    cost per coin, per-sale records for tax reporting). Sells that exceed
    recorded buys (untracked transfers, rewards) get a $0 basis for the
    uncovered part - amounts are pennies."""
    txs = db.execute(
        "SELECT date, symbol, side, quantity, total FROM transactions ORDER BY date, id"
    ).fetchall()
    lots = {}                 # symbol -> [[qty, unit_cost, acquired_date], ...] oldest first
    realized_by_year = {}
    sales = []
    for t in txs:
        sym = t["symbol"]
        lots.setdefault(sym, [])
        if t["side"] == "buy":
            if t["quantity"] > 0:
                lots[sym].append([t["quantity"], t["total"] / t["quantity"], t["date"]])
            continue
        remaining = t["quantity"]
        cost = 0.0
        first_acq = None
        sale_lots = []            # [qty consumed, unit cost, acquired date] per lot
        while remaining > 1e-12 and lots[sym]:
            lot = lots[sym][0]
            take = min(lot[0], remaining)
            cost += take * lot[1]
            if first_acq is None:
                first_acq = lot[2]
            sale_lots.append([take, lot[1], lot[2]])
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                lots[sym].pop(0)
        year = t["date"][:4]
        gain = t["total"] - cost
        realized_by_year[year] = realized_by_year.get(year, 0.0) + gain
        sales.append({"date": t["date"], "symbol": sym, "qty": t["quantity"],
                      "proceeds": t["total"], "cost": cost, "gain": gain,
                      "first_acquired": first_acq or "",
                      "lots": sale_lots, "uncovered": max(remaining, 0.0)})
    open_cost = {s: sum(q * c for q, c, _ in L) for s, L in lots.items()}
    return realized_by_year, open_cost, sales


def compute_xirr(db, total_value):
    """Money-weighted annualized return: every buy is a negative cash flow,
    every sell positive, today's portfolio value closes the position."""
    txs = db.execute("SELECT date, side, total FROM transactions ORDER BY date").fetchall()
    if not txs:
        return None
    today = date.today()
    t0 = date.fromisoformat(txs[0]["date"])  # time runs forward from the first buy
    flows = [((date.fromisoformat(t["date"]) - t0).days,
              -t["total"] if t["side"] == "buy" else t["total"]) for t in txs]
    flows.append(((today - t0).days, total_value))
    if total_value <= 0 or not any(a < 0 for _, a in flows):
        return None

    def npv(r):
        return sum(a / (1 + r) ** (d / 365.0) for d, a in flows)

    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2 * 100


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


@app.route("/api/export/realized")
def api_export_realized():
    year = request.args.get("year", "").strip()
    _, _, sales = compute_fifo(get_db())
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


def _is_long_term(acquired, sold):
    """IRS holding period: long-term means held MORE than one year — strictly
    after the acquisition date's first anniversary (Feb 29 rolls to Mar 1)."""
    a = date.fromisoformat(acquired[:10])
    s = date.fromisoformat(sold[:10])
    try:
        anniversary = a.replace(year=a.year + 1)
    except ValueError:                     # Feb 29 in a non-leap year
        anniversary = a.replace(year=a.year + 1, month=3, day=1)
    return s > anniversary


@app.route("/api/export/tax8949")
def api_export_tax8949():
    """Form-8949-style export: one row per FIFO lot consumed by each sale,
    with per-lot acquisition dates and the short/long-term split. Proceeds are
    prorated across lots by quantity so per-lot gain sums to the sale's gain."""
    year = request.args.get("year", "").strip()
    _, _, sales = compute_fifo(get_db())
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


# ---------------------------------------------------------------- stocks (Raymond James)

def yahoo_snapshot(sym):
    """Current price plus the previous close and the close ~7 days ago,
    so the UI can show daily and weekly moves. Cached 15 minutes."""
    if sym == "CASH":
        return {"price": 1.0, "prev": 1.0, "week": 1.0}

    def fetch():
        resp = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + sym,
                            params={"range": "1mo", "interval": "1d"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        meta = res["meta"]
        pairs = [(t, c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
        price = meta.get("regularMarketPrice") or (pairs[-1][1] if pairs else None)
        prev = meta.get("previousClose")
        if prev is None and len(pairs) >= 2:
            prev = pairs[-2][1]
        # last close at or before 7 days ago
        cutoff = time.time() - 7 * 86400
        week = next((c for t, c in reversed(pairs) if t <= cutoff), pairs[0][1] if pairs else None)
        if not price:
            raise ValueError("no price in Yahoo response")  # -> fall back to cache
        return {"price": price, "prev": prev or price, "week": week or price}

    # 'touch': a failed fetch re-stamps the old snapshot, so we don't hammer
    # Yahoo on every request while it is down
    try:
        return cached_fetch("ys:" + sym, 900, fetch, stale="touch")
    except Exception:
        return None



RJ_ACCT_NAMES = {"[REDACTED-RJ-ACCOUNT]": "Joint", "[REDACTED-RJ-ACCOUNT]": "Jennifer IRA", "[REDACTED-RJ-ACCOUNT]": "Peter IRA",
                 "[REDACTED-RJ-ACCOUNT]": "Peter Roth", "[REDACTED-RJ-ACCOUNT]": "Jennifer Roth", "[REDACTED-RJ-ACCOUNT]": "Aaron (Custodial)"}
RJ_SYM_BLACKLIST = {"RJA", "SIPC", "IRA", "ETF", "FDIC", "LOSS", "USA", "NYSE"}


def parse_rj_statement(pdf_bytes):
    """Parse one Raymond James monthly statement PDF: account, closing value,
    cash sweep, positions (qty/cost basis/price/value/income) and advisory fees.
    Self-validating: every position needs qty*price ~= value, and the account
    only counts as valid if cash + positions reconcile to the printed closing value."""
    from pypdf import PdfReader
    full = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(pdf_bytes)).pages)
    acct_m = re.search(r"AccountNo\.\s*([0-9A-Z]{8})", full)
    if not acct_m:
        raise ValueError("no Raymond James account number found - is this an RJ statement?")
    acct = acct_m.group(1)
    closing = float(re.search(r"Closing\s*Value\s*\$([\d,\.]+)", full).group(1).replace(",", ""))
    cash_m = re.search(r"BankDepositProgram\s*Total\s*\$([\d,\.]+)", full)
    cash = float(cash_m.group(1).replace(",", "")) if cash_m else 0.0

    positions = {}
    for m in re.finditer(r"\((([A-Z]{2,6})|30338H656)\)", full):
        sym = "FCMXPX" if m.group(1) == "30338H656" else m.group(1)
        if sym in RJ_SYM_BLACKLIST or sym in positions:
            continue
        tail = full[m.end():m.end() + 260]
        qm = re.match(r"\s*([\d,]+\.\d{3})", tail)   # RJ prints quantities with 3 decimals
        if not qm:
            continue
        qty = float(qm.group(1).replace(",", ""))
        seg = tail[qm.end():].split("LOT")[0]
        seg = re.sub(r"^c?\s*(\d{2}/\d{2}/\d{4})?", "", seg)  # covered flag + glued acquired-date
        d = [float(x.replace(",", "")) for x in re.findall(r"\$([\d,]+\.\d{2})", seg)]
        if len(d) < 2:
            continue
        # amounts are glued; search pairs from the END because unit-cost*qty also
        # equals cost basis - the (price, market value) pair is the last that fits
        for pi in range(len(d) - 2, -1, -1):
            price, value = d[pi], d[pi + 1]
            if price > 0 and value > 0 and abs(qty * price - value) / value < 0.02:
                cost = d[pi - 1] if pi >= 1 else 0.0
                income = 0.0
                vi = seg.find(format(value, ",.2f"))
                if vi != -1:
                    ym = re.match(r"(\d{1,2}\.\d{2})%\$([\d,]+\.\d{2})",
                                  seg[vi + len(format(value, ",.2f")):].lstrip())
                    if ym:
                        income = float(ym.group(2).replace(",", ""))
                # zero-income rows skip the yield column, so a small gain% can
                # masquerade as yield - reject "income" that equals the gain
                if income and abs(income - (value - cost)) < 1.0:
                    income = 0.0
                positions[sym] = {"qty": qty, "cost": cost, "price": price,
                                  "value": value, "income": income}
                break

    computed = cash + sum(p["value"] for p in positions.values())
    fee_m = re.search(r"Fees?\s*\$\(([\d,]+\.\d{2})\)\$\(([\d,]+\.\d{2})\)", full)
    rate_m = re.search(r"(\dQ)Fees\s*for\s*\d+/365Days\s*at\s*([\d\.]+)%", full)
    period_m = re.search(r"([A-Za-z]+\s*\d+\s*to\s*[A-Za-z]+\s*\d+,\s*20\d\d)", full.replace("to", " to ", 1))
    return {
        "acct": acct, "name": RJ_ACCT_NAMES.get(acct, acct),
        "closing": closing, "cash": cash, "positions": positions,
        "computed": round(computed, 2),
        "valid": bool(closing) and abs(computed - closing) / closing < 0.005,
        "fee_q": float(fee_m.group(1).replace(",", "")) if fee_m else 0.0,
        "fee_ytd": float(fee_m.group(2).replace(",", "")) if fee_m else 0.0,
        "fee_rate": float(rate_m.group(2)) if rate_m else None,
        "fee_quarter": rate_m.group(1) if rate_m else None,
        "period": period_m.group(1) if period_m else None,
    }


@app.route("/api/stocks/import_statements", methods=["POST"])
def api_stocks_import_statements():
    """Monthly refresh: upload the six RJ statement PDFs. Each account is
    validated against its printed closing value; only valid accounts are applied."""
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files received."}), 400
    db = get_db()
    meta = {r["symbol"]: (r["name"], r["product_type"])
            for r in db.execute("SELECT DISTINCT symbol, name, product_type FROM stocks")}
    results = []
    parsed = {}
    for f in files:
        try:
            st = parse_rj_statement(f.read())
        except Exception as e:
            results.append({"file": f.filename, "ok": False, "error": str(e)[:140]})
            continue
        if st["acct"] in parsed:
            results.append({"file": f.filename, "account": st["name"], "ok": False,
                            "error": "duplicate of another uploaded statement"})
            continue
        parsed[st["acct"]] = st
        results.append({"file": f.filename, "account": st["name"], "ok": st["valid"],
                        "closing": st["closing"], "computed": st["computed"],
                        "error": None if st["valid"] else
                        "positions don't reconcile to the statement's closing value - not applied"})
    applied = []
    row = db.execute("SELECT value FROM settings WHERE key='advisory_fees'").fetchone()
    fees = json.loads(row["value"]) if row else {"accounts": {}}
    period = None
    for st in parsed.values():
        if not st["valid"]:
            continue
        name = st["name"]
        db.execute("DELETE FROM stocks WHERE account=?", (name,))
        for sym, p in st["positions"].items():
            nm, pt = meta.get(sym, (sym, "Funds"))
            db.execute("INSERT OR REPLACE INTO stocks (symbol,account,name,product_type,quantity,invested,income,rj_price) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (sym, name, nm, pt, p["qty"], p["cost"], p["income"], p["price"]))
        db.execute("INSERT OR REPLACE INTO stocks (symbol,account,name,product_type,quantity,invested,income,rj_price) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("CASH", name, "Raymond James Bank Deposit", "Cash & Cash Alternatives",
                    st["cash"], st["cash"], 0, 1.0))
        fees["accounts"][name] = {"q": st["fee_q"], "ytd": st["fee_ytd"]}
        if st["fee_rate"]:
            fees["rate"] = st["fee_rate"]
        if st["fee_quarter"]:
            fees["quarter"] = st["fee_quarter"]
        period = st["period"] or period
        applied.append(name)
    if applied:
        mapping = {}
        for r in db.execute("SELECT symbol, account, quantity FROM stocks"):
            mapping.setdefault(r["symbol"], {})[r["account"]] = r["quantity"]
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('stock_account_map', ?)",
                   (json.dumps(mapping),))
        if period:
            fees["as_of"] = period
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('stocks_as_of', ?)",
                       (period + " (statements)",))
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('advisory_fees', ?)",
                   (json.dumps(fees),))
    db.commit()
    return jsonify({"results": results, "applied": applied})


@app.route("/api/stocks")
def api_stocks():
    db = get_db()
    rows = db.execute("SELECT * FROM stocks ORDER BY symbol").fetchall()
    snaps = {}
    for r in rows:
        if r["symbol"] not in snaps:
            snaps[r["symbol"]] = yahoo_snapshot(r["symbol"])
    out = []
    accounts = {}
    totals = {"value": 0.0, "invested": 0.0, "income": 0.0, "day": 0.0, "week": 0.0}
    for r in rows:
        snap = snaps[r["symbol"]]
        price = (snap or {}).get("price") or r["rj_price"]
        prev = (snap or {}).get("prev") or price
        week = (snap or {}).get("week") or price
        qty = r["quantity"]
        value = qty * price
        day_change = qty * (price - prev)
        week_change = qty * (price - week)
        out.append({"symbol": r["symbol"], "account": r["account"], "name": r["name"],
                    "type": r["product_type"], "quantity": qty, "price": price,
                    "value": value, "invested": r["invested"], "gain": value - r["invested"],
                    "gain_pct": ((value - r["invested"]) / r["invested"] * 100) if r["invested"] else None,
                    "income": r["income"], "live": snap is not None,
                    "day_change": day_change,
                    "day_pct": ((price / prev - 1) * 100) if prev else None,
                    "week_change": week_change,
                    "week_pct": ((price / week - 1) * 100) if week else None})
        a = accounts.setdefault(r["account"], {"account": r["account"], "value": 0.0,
                                               "invested": 0.0, "income": 0.0,
                                               "day_change": 0.0, "week_change": 0.0})
        a["value"] += value
        a["invested"] += r["invested"]
        a["income"] += r["income"]
        a["day_change"] += day_change
        a["week_change"] += week_change
        totals["value"] += value
        totals["invested"] += r["invested"]
        totals["income"] += r["income"]
        totals["day"] += day_change
        totals["week"] += week_change
    out.sort(key=lambda x: -x["value"])
    acct_list = sorted(accounts.values(), key=lambda a: -a["value"])
    for a in acct_list:
        a["gain"] = a["value"] - a["invested"]
        base_d = a["value"] - a["day_change"]
        base_w = a["value"] - a["week_change"]
        a["day_pct"] = (a["day_change"] / base_d * 100) if base_d else None
        a["week_pct"] = (a["week_change"] / base_w * 100) if base_w else None
    base_d = totals["value"] - totals["day"]
    base_w = totals["value"] - totals["week"]
    fees_row = db.execute("SELECT value FROM settings WHERE key='advisory_fees'").fetchone()
    fees = None
    if fees_row:
        fj = json.loads(fees_row["value"])
        fq = sum(a.get("q", 0) for a in fj.get("accounts", {}).values())
        fytd = sum(a.get("ytd", 0) for a in fj.get("accounts", {}).values())
        fees = {"quarter": fq, "ytd": fytd, "rate": fj.get("rate"),
                "quarter_label": fj.get("quarter"), "expected_annual": fq * 4}
    return jsonify({"holdings": out, "accounts": acct_list, "fees": fees,
                    "total_value": totals["value"],
                    "total_invested": totals["invested"],
                    "total_gain": totals["value"] - totals["invested"],
                    "total_income": totals["income"],
                    "day_change": totals["day"],
                    "day_pct": (totals["day"] / base_d * 100) if base_d else None,
                    "week_change": totals["week"],
                    "week_pct": (totals["week"] / base_w * 100) if base_w else None,
                    "as_of": get_setting("stocks_as_of")})


@app.route("/api/history/stocks")
def api_stocks_history():
    """Current stock positions valued back in time (quantities held constant -
    the export has no purchase dates, so this shows the positions, not the account)."""
    days = int(request.args.get("days", 365))
    db = get_db()
    rows = db.execute(
        "SELECT symbol, SUM(quantity) AS quantity, MAX(rj_price) AS rj_price "
        "FROM stocks GROUP BY symbol").fetchall()
    start = (date.today() - timedelta(days=days)).isoformat()
    maps = {}
    missing = []
    for r in rows:
        if r["symbol"] == "CASH":
            continue
        cg_id = "stock-" + r["symbol"]
        ensure_yahoo_backfill(db, cg_id, r["symbol"])
        hist = db.execute("SELECT date, price FROM price_history WHERE coingecko_id=? AND date>=? ORDER BY date",
                          (cg_id, start)).fetchall()
        if hist:
            maps[r["symbol"]] = {h["date"]: h["price"] for h in hist}
        else:
            missing.append(r["symbol"])
    all_dates = sorted({d for pm in maps.values() for d in pm})
    qty = {r["symbol"]: r["quantity"] for r in rows}
    cash = sum(r["quantity"] for r in rows if r["symbol"] == "CASH")
    last = {}
    out = []
    for d in all_dates:
        total = cash
        for sym, pm in maps.items():
            if d in pm:
                last[sym] = pm[d]
            if sym in last:
                total += qty[sym] * last[sym]
        out.append({"date": d, "value": total})
    return jsonify({"points": out, "missing": missing})


@app.route("/api/history/stocks/each")
def api_stocks_each():
    """Per-ticker daily prices for the individual-performance chart,
    aligned to a shared date axis with carry-forward."""
    days = int(request.args.get("days", 365))
    db = get_db()
    rows = db.execute("SELECT DISTINCT symbol FROM stocks WHERE symbol != 'CASH'").fetchall()
    start = (date.today() - timedelta(days=days)).isoformat()
    maps = {}
    for r in rows:
        cg_id = "stock-" + r["symbol"]
        ensure_yahoo_backfill(db, cg_id, r["symbol"])
        hist = db.execute(
            "SELECT date, price FROM price_history WHERE coingecko_id=? AND date>=? ORDER BY date",
            (cg_id, start)).fetchall()
        if len(hist) >= 2:
            maps[r["symbol"]] = {h["date"]: h["price"] for h in hist}
    all_dates = sorted({d for pm in maps.values() for d in pm})
    step = 5 if len(all_dates) > 900 else 1   # sample long ranges to keep payload light
    idx = list(range(0, len(all_dates), step))
    if idx and idx[-1] != len(all_dates) - 1:
        idx.append(len(all_dates) - 1)
    dates = [all_dates[i] for i in idx]
    series = []
    for sym, pm in sorted(maps.items()):
        last = None
        full = []   # carry-forward across the shared axis, then sample
        for d in all_dates:
            if d in pm:
                last = pm[d]
            full.append(last)
        series.append({"symbol": sym,
                       "values": [round(full[i], 4) if full[i] is not None else None for i in idx]})
    return jsonify({"dates": dates, "series": series})


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


# ---------------------------------------------------------------- coinbase sync

CB_KEY_PATH = os.path.join(APP_DIR, "coinbase_key.json")


def cb_client():
    if not os.path.exists(CB_KEY_PATH):
        return None
    from coinbase.rest import RESTClient
    k = json.load(open(CB_KEY_PATH))
    return RESTClient(api_key=k["name"], api_secret=k["privateKey"])


def cb_balances(client):
    """All Coinbase balances per coin, including staked wallets and vaults
    (v2 accounts - the v3 endpoint hides staked positions)."""
    out = {}
    starting_after = None
    while True:
        params = {"limit": "100"}
        if starting_after:
            params["starting_after"] = starting_after
        res = client.get("/v2/accounts", params=params)
        data = res.get("data", [])
        for a in data:
            bal = float(a["balance"]["amount"])
            if bal > 1e-9:
                cur = a["currency"]["code"].lower()
                out[cur] = out.get(cur, 0.0) + bal
        if not (res.get("pagination") or {}).get("next_uri"):
            break
        starting_after = data[-1]["id"]
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
        starting_after = None
        while True:
            params = {"limit": "100"}
            if starting_after:
                params["starting_after"] = starting_after
            res = client.get("/v2/accounts", params=params)
            accounts += res.get("data", [])
            if not (res.get("pagination") or {}).get("next_uri"):
                break
            starting_after = accounts[-1]["id"]
        for acct in accounts:
            cur = acct["currency"]["code"].lower()
            if cur not in known and float(acct["balance"]["amount"]) <= 0:
                continue
            starting_after = None
            done = False
            while not done:
                params = {"limit": "100"}
                if starting_after:
                    params["starting_after"] = starting_after
                res = client.get("/v2/accounts/{}/transactions".format(acct["id"]), params=params)
                data = res.get("data", [])
                if not data:
                    break
                for t in data:
                    if t.get("created_at", "") < since:
                        done = True
                        break
                    handle(t)
                if done or not (res.get("pagination") or {}).get("next_uri"):
                    break
                starting_after = data[-1]["id"]
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


@app.route("/api/coinbase/status")
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


@app.route("/api/coinbase/sync", methods=["POST"])
def api_cb_sync():
    return jsonify(coinbase_sync(get_db()))


# ---------------------------------------------------------------- main

init_db()
app.secret_key = get_setting("secret_key")
app.permanent_session_lifetime = timedelta(days=30)  # stay signed in for 30 days

if __name__ == "__main__":
    threading.Thread(target=backup_loop, daemon=True).start()   # daily backups
    threading.Thread(target=cb_sync_loop, daemon=True).start()  # Coinbase sync every 6h
    threading.Thread(target=alerts_loop, daemon=True).start()   # price alerts every 5 min
    # 0.0.0.0 = reachable from other devices on the home network,
    # e.g. http://<this-macs-name>.local:5178
    app.run(host="0.0.0.0", port=5178, debug=False)
