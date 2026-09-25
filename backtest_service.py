"""
Servizio backtest per Railway (file unico, nessuna dipendenza da altri file del repo).
Ogni LOOP_INTERVAL_SECONDS (default 4 ore, come il bot) rifa il backtest sugli ultimi
BACKTEST_DAYS giorni con gli stessi parametri del bot (stesse variabili d'ambiente)
e stampa il report nei log. Usa solo dati pubblici: non servono chiavi private ne' Volume.

Approssimazioni: niente tick/szDecimals, prezzo del ciclo = chiusura della candela 1h,
fee e slippage uguali su entrambi i lati.
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

def fetch_candles(coins, days, cache):
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


# ============================================================
# SIMULAZIONE
# ============================================================

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

        # ---- SELL: lotto col rendimento maggiore tra quelli a target ----
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

        # ---- BUY: coin col ribasso piu' forte (poi le successive se bloccata) ----
        # riferimento: max 24h per il primo lotto della coin, poi il lotto col prezzo
        # di acquisto piu' basso tra quelli aperti (stessa logica del bot)
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


def print_report(r, a, closes, days):
    print("=" * 60)
    print(f"PERFORMANCE DEL BOT NEGLI ULTIMI {days:.0f} GIORNI")
    print("=" * 60)
    print(f"Parametri: BUY ${r['buy']:.0f} | DIP {r['dip']:.1f}% | TP {r['tp']:.1f}% | ciclo ogni {r['int']}h | SELL {a.sell_percent:.0f}%")
    print(f"Capitale iniziale        ${a.capital:>10.2f}")
    print(f"Valore finale            ${r['final']:>10.2f}   ({r['ret']:+.2f}%)")
    print(f"  profitto realizzato    ${r['realized']:>10.2f}")
    print(f"  non realizzato         ${r['unrealized']:>10.2f}   (lotti ancora aperti)")
    print(f"Acquisti / vendite       {r['buys']:>4} / {r['sells']:<4}   lotti aperti: {r['open']}")
    print(f"Max capitale investito   ${r['deployed']:>10.2f}   -> ritorno sull'investito {(r['final'] - a.capital) / r['deployed'] * 100 if r['deployed'] else 0:+.2f}%")
    print(f"Max drawdown             {r['dd']:>10.2f}%")
    print()
    print(f"{'coin':<6} {'buy':>5} {'sell':>5} {'realiz.':>9} {'non real.':>10} {'aperti':>7} {'coin nel periodo':>17}")

    for c, v in r["per"].items():
        move = (closes[c][-1] / closes[c][23] - 1) * 100
        print(f"{c:<6} {v['buys']:>5} {v['sells']:>5} {v['realized']:>9.2f} {v['unrealized']:>10.2f} {v['open']:>7} {move:>+16.1f}%")



# ============================================================
# SERVIZIO
# ============================================================

load_dotenv()

sys.stdout.reconfigure(line_buffering=True)

COINS = [c.strip().upper() for c in os.getenv("COINS", "HYPE,ZEC,ETH,SOL").split(",") if c.strip()]

# Stesse variabili del bot: cosi' il backtest segue i parametri del bot
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "14400"))
BUY_USD = float(os.getenv("BUY_USD", "12"))
DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2"))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "4"))

# Solo backtest (l'API restituisce al massimo ~5000 candele 1h, circa 208 giorni)
BACKTEST_DAYS = min(int(os.getenv("BACKTEST_DAYS", "200")), 208)

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


def run():
    data = fetch_candles(COINS, BACKTEST_DAYS, None)

    if not data:
        raise RuntimeError("Nessun dato scaricato")

    times, closes, highs = align(data)

    if len(times) < 48:
        raise RuntimeError("Dati insufficienti")

    days = (times[-1] - times[0]) / HOUR_MS / 24

    # le candele sono da 1h: il ciclo simulato non puo' essere piu' corto
    interval = max(1, round(LOOP_INTERVAL_SECONDS / 3600))

    log(f"BACKTEST | coin {','.join(closes)} | {days:.0f} giorni | {len(times)} candele 1h")

    result = simulate(times, closes, highs, BUY_USD, DIP_PERCENT, TAKE_PROFIT_PERCENT, interval, args)

    print_report(result, args, closes, days)


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            log(f"ERRORE BACKTEST | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
