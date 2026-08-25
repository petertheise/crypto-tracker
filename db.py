"""Database: paths, connection helpers, schema and one-off settings reads."""
import os
import secrets
import sqlite3

from flask import g

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "portfolio.db")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


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
