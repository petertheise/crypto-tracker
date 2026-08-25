"""Portfolio math: balances, the cold-storage guard, FIFO lot matching,
XIRR, and the shared holdings-over-time walk used by the value charts."""
from datetime import date, timedelta

from market_data import get_history


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


def hot_balance_error(db, sym, qty, bal=None, cold=None):
    """Cold-storage guard for sells/conversions: if qty exceeds what's actually
    on Coinbase (total minus cold storage), return the standard rejection
    message, else None. Pass bal/cold to reuse balances already loaded."""
    if bal is None or cold is None:
        bal, cold = get_balances(db, sym)
    if qty > (bal - cold) + 1e-9:
        return ("Only {:.8f} {} is on Coinbase ({:.8f} is in cold storage). "
                "Record a transfer back from the cold wallet first.".format(
                    max(0.0, bal - cold), sym.upper(), max(0.0, cold)))
    return None


def compute_fifo(db):
    """FIFO lot matching. Returns (realized gain by sale year, remaining open
    cost per coin, per-sale records for tax reporting, remaining open lots). Sells that exceed
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
    # lots is the same structure the walk above consumed from: per symbol,
    # [qty, unit_cost, acquired_date] oldest first. Handed back untouched so
    # callers can read holding periods without redoing the matching.
    return realized_by_year, open_cost, sales, lots


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


def long_term_date(acquired):
    """First date on which a lot bought on `acquired` counts as long-term:
    the day after its first anniversary (mirrors _is_long_term exactly)."""
    a = date.fromisoformat(acquired[:10])
    try:
        anniversary = a.replace(year=a.year + 1)
    except ValueError:                     # Feb 29 in a non-leap year
        anniversary = a.replace(year=a.year + 1, month=3, day=1)
    return anniversary + timedelta(days=1)


def lot_tax_view(lots, prices, today=None, soon_days=90):
    """Bucket every open lot short vs long-term at today's prices.

    lots:   {symbol: [[qty, unit_cost, acquired], ...]} straight from compute_fifo
    prices: {symbol: current USD price}
    Returns per-coin rows plus portfolio totals and the lots about to cross
    into long-term within `soon_days`. Unrealized only - nothing here is a
    taxable event until something is actually sold."""
    today = today or date.today()
    empty = lambda: {"qty": 0.0, "cost": 0.0, "value": 0.0, "gain": 0.0}
    coins, totals, soon = [], {"short": empty(), "long": empty()}, []
    for sym, L in sorted(lots.items()):
        px = prices.get(sym) or 0.0
        row = {"symbol": sym.upper(), "price": px,
               "short": empty(), "long": empty(), "lots": [], "next_cross": None}
        for qty, unit_cost, acquired in L:
            if qty <= 1e-12:
                continue
            crosses = long_term_date(acquired)
            is_long = today >= crosses
            days_left = None if is_long else (crosses - today).days
            cost, value = qty * unit_cost, qty * px
            b = row["long" if is_long else "short"]
            b["qty"] += qty; b["cost"] += cost
            b["value"] += value; b["gain"] += value - cost
            lot = {"qty": qty, "unit_cost": unit_cost, "acquired": acquired,
                   "cost": cost, "value": value, "gain": value - cost,
                   "long_term": is_long, "crosses": crosses.isoformat(),
                   "days_left": days_left}
            row["lots"].append(lot)
            if days_left is not None:
                # earliest crossing for this coin, however far out
                if row["next_cross"] is None or days_left < row["next_cross"]["days_left"]:
                    row["next_cross"] = lot
                if days_left <= soon_days:
                    soon.append(dict(lot, symbol=sym.upper()))
        if row["short"]["qty"] > 1e-12 or row["long"]["qty"] > 1e-12:
            for k in ("short", "long"):
                for f in ("qty", "cost", "value", "gain"):
                    totals[k][f] += row[k][f]
            coins.append(row)
    coins.sort(key=lambda r: -(r["short"]["value"] + r["long"]["value"]))
    soon.sort(key=lambda l: l["days_left"])
    return {"coins": coins, "totals": totals, "soon": soon}


def whatif_sale(lots, sym, qty, price, today=None):
    """Walk the FIFO queue as if `qty` of `sym` were sold right now at `price`,
    without touching anything. Returns the realized gain split short vs long."""
    today = today or date.today()
    empty = lambda: {"qty": 0.0, "cost": 0.0, "proceeds": 0.0, "gain": 0.0}
    out = {"symbol": sym.upper(), "qty": qty, "price": price,
           "short": empty(), "long": empty(), "lots": [],
           "proceeds": qty * price, "cost": 0.0, "gain": 0.0, "uncovered": 0.0}
    remaining = qty
    for lot_qty, unit_cost, acquired in lots.get(sym, []):
        if remaining <= 1e-12:
            break
        take = min(lot_qty, remaining)
        cost, proceeds = take * unit_cost, take * price
        is_long = today >= long_term_date(acquired)
        b = out["long" if is_long else "short"]
        b["qty"] += take; b["cost"] += cost
        b["proceeds"] += proceeds; b["gain"] += proceeds - cost
        out["lots"].append({"qty": take, "unit_cost": unit_cost, "acquired": acquired,
                            "cost": cost, "proceeds": proceeds,
                            "gain": proceeds - cost, "long_term": is_long})
        out["cost"] += cost
        remaining -= take
    out["uncovered"] = max(remaining, 0.0)   # sold more than recorded buys: $0 basis
    out["gain"] = out["proceeds"] - out["cost"]
    return out


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
