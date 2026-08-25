"""Stocks tab: Raymond James statement parsing and the /api/stocks* routes."""
import os
import io
import json
import re
from datetime import date, timedelta

from flask import jsonify, request

from db import get_db, get_setting
from market_data import ensure_yahoo_backfill, yahoo_snapshot

# Account-number -> friendly-name map. Real account numbers do not belong in
# source, so they live in rj_accounts.json (gitignored). Without that file the
# statement's own account number is shown instead - nothing breaks.
def _load_acct_names():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rj_accounts.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


RJ_ACCT_NAMES = _load_acct_names()
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
                "quarter_label": fj.get("quarter"), "expected_annual": fq * 4,
                # per-account annualised fee, for the fee-vs-income breakdown
                "accounts": {name: {"quarter": a.get("q") or 0.0,
                                    "ytd": a.get("ytd") or 0.0,
                                    "annual": (a.get("q") or 0.0) * 4}
                             for name, a in (fj.get("accounts") or {}).items()}}
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


def register(app):
    app.add_url_rule("/api/stocks/import_statements", view_func=api_stocks_import_statements,
                     methods=["POST"])
    app.add_url_rule("/api/stocks", view_func=api_stocks)
    app.add_url_rule("/api/history/stocks", view_func=api_stocks_history)
    app.add_url_rule("/api/history/stocks/each", view_func=api_stocks_each)
