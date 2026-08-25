"""External price data: the shared api_cache helpers, CoinGecko fetches,
daily price history, and the Yahoo Finance backfills/snapshots."""
import json
import time
from datetime import datetime, date, timedelta, timezone

import requests

from db import get_db

COINGECKO = "https://api.coingecko.com/api/v3"


def cache_put(db, key, data):
    """Store one api_cache row. The single writer for that table - used by
    cached_fetch and by ensure_yahoo_backfill's attempt marker."""
    db.execute("INSERT OR REPLACE INTO api_cache (key,data,updated_at) VALUES (?,?,?)",
               (key, json.dumps(data), time.time()))
    db.commit()


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
    cache_put(db, key, data)
    return data


def cg_get(path, params=None, cache_key=None, ttl=60):
    """GET from CoinGecko with a small SQLite cache to respect rate limits.
    A thin wrapper over cached_fetch; a failed fetch serves stale data."""
    key = cache_key or (path + "?" + json.dumps(params or {}, sort_keys=True))

    def fetch():
        resp = requests.get(COINGECKO + path, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    return cached_fetch(key, ttl, fetch)


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


# Yahoo tickers for deep history (CoinGecko free tier stops at 365 days)
YAHOO_CRYPTO = {
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "solana": "SOL-USD", "ripple": "XRP-USD",
    "ondo-finance": "ONDO-USD", "render-token": "RENDER-USD", "bittensor": "TAO22974-USD",
    "fetch-ai": "FET-USD", "hedera-hashgraph": "HBAR-USD", "internet-computer": "ICP-USD",
    "matic-network": "POL28321-USD", "aptos": "APT21794-USD", "sei-network": "SEI-USD",
    "pepe": "PEPE24478-USD", "shiba-inu": "SHIB-USD", "cosmos": "ATOM-USD",
    "usd-coin": "USDC-USD", "quant-network": "QNT-USD", "pyth-network": "PYTH-USD",
}


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
        cache_put(db, "ybf:" + cg_id, "1")
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
