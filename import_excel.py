"""One-time import of transactions from the Excel workbook into portfolio.db.

Usage: ./venv/bin/python import_excel.py "/path/to/Crypto refresh (version 3.4).xlsm"
Re-running wipes and re-imports the transactions table.
"""
import sys
import sqlite3
import datetime
import os

import openpyxl

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "portfolio.db")

# symbol -> (coingecko id, display name, category from the Dashboard sheet)
COIN_MAP = {
    "btc":    ("bitcoin",           "Bitcoin",       "Core"),
    "eth":    ("ethereum",          "Ethereum",      "Core"),
    "sol":    ("solana",            "Solana",        "Core"),
    "xrp":    ("ripple",            "XRP",           "Core"),
    "usdc":   ("usd-coin",          "USDC",          "Core"),
    "tao":    ("bittensor",         "Bittensor",     "AI"),
    "render": ("render-token",      "Render",        "AI"),
    "fet":    ("fetch-ai",          "Fetch.ai (ASI)","AI"),
    "ath":    ("aethir",            "Aethir",        "Infra"),
    "ondo":   ("ondo-finance",      "Ondo",          "Infra"),
    "pyth":   ("pyth-network",      "Pyth Network",  "Infra"),
    "apt":    ("aptos",             "Aptos",         "Infra"),
    "sei":    ("sei-network",       "Sei",           "Infra"),
    "icp":    ("internet-computer", "Internet Computer", "Infra"),
    "hbar":   ("hedera-hashgraph",  "Hedera",        "Infra"),
    "matic":  ("matic-network",     "Polygon (MATIC)", "Infra"),
    "qnt":    ("quant-network",     "Quant",         "Infra"),
    "pepe":   ("pepe",              "Pepe",          "Meme"),
    "shib":   ("shiba-inu",         "Shiba Inu",     "Meme"),
}


def main(xlsm_path):
    from app import init_db
    init_db()
    db = sqlite3.connect(DB_PATH)

    for sym, (cg, name, cat) in COIN_MAP.items():
        db.execute(
            "INSERT OR REPLACE INTO coins (symbol, coingecko_id, name, category) VALUES (?,?,?,?)",
            (sym, cg, name, cat),
        )

    wb = openpyxl.load_workbook(xlsm_path, data_only=True, read_only=True)
    ws = wb["Transactions"]
    db.execute("DELETE FROM transactions")
    n = skipped = 0
    for r in ws.iter_rows(min_row=2, max_col=12, values_only=True):
        dt, sym, exchange, price, amount, fee, total = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        notes = r[11] or ""
        if dt is None or sym is None or amount is None:
            continue
        sym = str(sym).strip().lower()
        if sym not in COIN_MAP:
            print("  ! unknown symbol, skipped:", sym)
            skipped += 1
            continue
        if isinstance(dt, datetime.datetime):
            d = dt.date().isoformat()
        else:
            d = str(dt)[:10]
        amount = float(amount)
        total = float(total or 0)
        side = "buy" if amount >= 0 else "sell"
        db.execute(
            "INSERT INTO transactions (date,symbol,side,quantity,price,fee,total,exchange,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d, sym, side, abs(amount), float(price or 0), float(fee or 0),
             abs(total), str(exchange or ""), str(notes)),
        )
        n += 1
    db.commit()

    # sanity check: per-coin quantity should match the workbook dashboard
    print("Imported {} transactions ({} skipped).".format(n, skipped))
    for row in db.execute(
        """SELECT symbol, SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) q,
                  SUM(CASE WHEN side='buy' THEN total ELSE -total END) c
           FROM transactions GROUP BY symbol ORDER BY 3 DESC"""
    ):
        print("  {:8} qty={:>18.8f}  net_cost=${:>12.2f}".format(row[0], row[1], row[2]))
    db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python import_excel.py <workbook.xlsm>")
    main(sys.argv[1])
