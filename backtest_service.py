"""
Servizio backtest per Railway con ottimizzazione del DIP % (Grid Search).
Mantiene TP fisso (default 6%) e valuta diversi livelli di DIP % sui 200 giorni.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace

from dotenv import load_dotenv

HOUR_MS = 3600 * 1000

# ============================================================
# DATI E SIMULAZIONE
# ============================================================

def fetch_candles(coins, days, cache=None):
    if cache and os.path.exists(cache):
        with open(cache) as f:
            data = json.load(f)
        if set(coins) <= set(data):
            return {c: data[c] for c in coins}

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    meta = info.spot_meta()

    usdc = next(i for i, t in enumerate(meta["tokens"]) if t["name"] == "USDC")

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * HOUR_MS

    data = {}

    for coin in coins:
        market = None

        for idx, token in enumerate(meta["tokens"]):
            if token["name"] in (coin, "U" + coin):
                market = next((m["name"] for m in meta["universe"] if m["tokens"] == [idx, usdc]), None)

                if market:
                    break

        if not market:
            print(f"ATTENZIONE: nessun mercato spot {coin}/USDC: coin ignorata")
            continue

        candles = info.candles_snapshot(market, "1h", start_ms, end_ms)
        data[coin] = [{"t": c["t"], "h": float(c["h"]), "c": float(c["c"])} for c in candles]

    if cache:
        with open(cache, "w") as f:
            json.dump(data, f)

    return data


def align(data):
    common = sorted(set.intersection(*[{c["t"] for c in v} for v in data.values()]))

    closes = {}
    highs = {}

    for coin, v in data.items():
        by_t = {c["t"]: c for c in v}
        closes[coin] = [by_t[t]["c"] for t in common]
        highs[coin] = [by_t[t]["h"] for t in common]

    return common, closes, highs


def simulate(times, closes, highs, buy_usd, dip, tp, interval, a):
    coins = list(closes)

    usdc = a.capital
    lots = []
    weekly = {}

    realized = 0.0
    buys = 0
    sells = 0
    peak = a.capital
    max_dd = 0.0
    max_deployed = 0.0
    per = {c: {"buys": 0, "sells": 0, "realized": 0.0} for c in coins}

    for i in range(23, len(times), interval):
        px = {c: closes[c][i] for c in coins}

        equity = usdc + sum(l["qty"] * px[l["coin"]] for l in lots)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        max_deployed = max(max_deployed, sum(l["qty"] * l["cost"] for l in lots))

        # ---- SELL ----
        best = None

        for lot in lots:
            p = px[lot["coin"]]

            if p >= lot["target"] and lot["qty"] * a.sell_percent / 100 * p >= a.min_order:
                ret = p / lot["buy_price"]

                if best is None or ret > best[0]:
                    best = (ret, lot)

        if best:
            lot = best[1]
            qty = lot["qty"] * a.sell_percent / 100
            proceeds = qty * px[lot["coin"]] * (1 - a.slippage) * (1 - a.fee)

            usdc += proceeds
            pnl = proceeds - qty * lot["cost"]
            realized += pnl
            per[lot["coin"]]["realized"] += pnl
            per[lot["coin"]]["sells"] += 1
            lot["qty"] -= qty
            sells += 1
            continue

        # ---- BUY ----
        week = datetime.fromtimestamp(times[i] / 1000, timezone.utc).strftime("%G-W%V")

        drops = []

        for c in coins:
            open_lots = [l for l in lots if l["coin"] == c]

            if open_lots:
                reference = min(l["buy_price"] for l in open_lots)
            else:
                reference = max(highs[c][i - 23:i + 1])

            drops.append(((reference - px[c]) / reference * 100, c))

        drops.sort(reverse=True)

        for drop, c in drops:
            if drop < dip:
                break

            held = sum(l["qty"] for l in lots if l["coin"] == c) * px[c]

            if held + buy_usd > a.max_position or weekly.get(week, 0) >= a.weekly_buys or usdc < buy_usd:
                continue

            fill = px[c] * (1 + a.slippage)
            qty = buy_usd / fill * (1 - a.fee)

            usdc -= buy_usd
            lots.append({"coin": c, "qty": qty, "buy_price": fill, "target": fill * (1 + tp / 100), "cost": buy_usd / qty})
            weekly[week] = weekly.get(week, 0) + 1
            per[c]["buys"] += 1
            buys += 1
            break

    last = {c: closes[c][-1] for c in coins}
    open_value = sum(l["qty"] * last[l["coin"]] for l in lots)
    final = usdc + open_value

    for c in coins:
        c_lots = [l for l in lots if l["coin"] == c]
        per[c]["unrealized"] = sum(l["qty"] * (last[c] - l["cost"]) for l in c_lots)
        per[c]["open"] = sum(1 for l in c_lots if l["qty"] * last[c] >= a.min_order)

    return {
        "buy": buy_usd, "dip": dip, "tp": tp, "int": interval,
        "ret": (final - a.capital) / a.capital * 100,
        "final": final,
        "realized": realized,
        "unrealized": open_value - sum(l["qty"] * l["cost"] for l in lots),
        "buys": buys, "sells": sells,
        "open": sum(1 for l in lots if l["qty"] * last[l["coin"]] >= a.min_order),
        "dd": max_dd,
        "deployed": max_deployed,
        "per": per,
    }

# ============================================================
# SERVIZIO E OTTIMIZZAZIONE
# ============================================================

load_dotenv()

sys.stdout.reconfigure(line_buffering=True)

COINS = [c.strip().upper() for c in os.getenv("COINS", "HYPE,ZEC,ETH,SOL").split(",") if c.strip()]
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "14400"))
BUY_USD = float(os.getenv("BUY_USD", "10"))

# Target Take Profit fisso al 6% come da analisi costi
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "6.0"))

BACKTEST_DAYS = min(int(os.getenv("BACKTEST_DAYS", "200")), 208)

ACCOUNT_ADDRESS = (
    os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS") 
    or os.getenv("HL_ACCOUNT_ADDRESS") 
    or os.getenv("ACCOUNT_ADDRESS")
)

args = SimpleNamespace(
    capital=float(os.getenv("BACKTEST_CAPITAL", "1000")),
    sell_percent=float(os.getenv("SELL_PERCENT", "95")),
    max_position=float(os.getenv("MAX_POSITION_USD", "200")),
    weekly_buys=int(os.getenv("MAX_WEEKLY_BUYS", "10")),
    min_order=float(os.getenv("MIN_ORDER_USD", "10")),
    fee=float(os.getenv("BACKTEST_FEE", "0.0007")),
    slippage=float(os.getenv("BACKTEST_SLIPPAGE", "0.0005")),
)


def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def get_real_capital(coins, last_prices):
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    user_state = info.spot_user_state(ACCOUNT_ADDRESS)

    usdc_balance = 0.0
    coins_value = 0.0

    for balance in user_state.get("balances", []):
        name = balance.get("coin")
        total = float(balance.get("total", 0) or 0)

        if name == "USDC":
            usdc_balance = total
        else:
            for coin in coins:
                if name in (coin, "U" + coin) and coin in last_prices:
                    coins_value += total * last_prices[coin]

    return usdc_balance + coins_value


def run_optimization():
    data = fetch_candles(COINS, BACKTEST_DAYS, None)

    if not data:
        raise RuntimeError("Nessun dato scaricato")

    times, closes, highs = align(data)

    if len(times) < 48:
        raise RuntimeError("Dati insufficienti")

    days = (times[-1] - times[0]) / HOUR_MS / 24
    interval = max(1, round(LOOP_INTERVAL_SECONDS / 3600))

    if ACCOUNT_ADDRESS:
        try:
            last_prices = {c: closes[c][-1] for c in closes}
            real_capital = get_real_capital(closes, last_prices)
            if real_capital > 0:
                args.capital = real_capital
                log(f"CAPITALE REALE INIZIALE | ${args.capital:.2f} USDC")
        except Exception as e:
            log(f"CAPITALE REALE ERRORE | {e} | uso BACKTEST_CAPITAL=${args.capital:.2f}")

    log(f"AVVIO RICERCA DIP OTTIMALE (TP FISSO = {TAKE_PROFIT_PERCENT}%) su {days:.0f} giorni")

    # Range di DIP % da testare (es. da 1.5% a 8.0% a passi di 0.5%)
    dip_candidates = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0]
    results = []

    for dip in dip_candidates:
        res = simulate(times, closes, highs, BUY_USD, dip, TAKE_PROFIT_PERCENT, interval, args)
        results.append(res)

    # Stampa la tabella comparativa nei log
    print("\n" + "=" * 80)
    print(f"CONFRONTO OTTIMIZZAZIONE DIP % (TP Fisso: {TAKE_PROFIT_PERCENT}%)")
    print("=" * 80)
    print(f"{'DIP %':<7} {'Rendimento':>12} {'Valore Fin.':>12} {'Realizzato':>12} {'Buys/Sells':>12} {'Open':>6} {'Max DD':>9}")
    print("-" * 80)

    best_res = None
    for r in results:
        if best_res is None or r["ret"] > best_res["ret"]:
            best_res = r

        print(f"{r['dip']:<7.1f}% {r['ret']:>11.2f}% ${r['final']:>11.2f} ${r['realized']:>11.2f} {r['buys']:>5}/{r['sells']:<5} {r['open']:>6} {r['dd']:>8.1f}%")

    print("=" * 80)
    print(f"-> MIGLIOR DIP TROVATO: {best_res['dip']}% con Rendimento del {best_res['ret']:+.2f}%")
    print("=" * 80 + "\n", flush=True)


if __name__ == "__main__":
    while True:
        try:
            run_optimization()
        except Exception as e:
            log(f"ERRORE OPTIMIZATION | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
