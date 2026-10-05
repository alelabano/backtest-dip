"""
Servizio backtest per Railway. Ogni LOOP_INTERVAL_SECONDS (default 4 ore) fa
tre cose distinte:

1) CAPITALE NECESSARIO PER I PARAMETRI ATTUALI DEL BOT: simula il bot con i
   parametri realmente in uso (DIP_PERCENT e TAKE_PROFIT_PERCENT letti
   dall'ambiente, gli stessi del bot vero) sugli ultimi BACKTEST_DAYS giorni,
   e calcola sia quanto capitale servirebbe per non saltare nessun BUY con le
   impostazioni correnti, sia quanto capitale servirebbe per raggiungere l'obiettivo
   mensile prefissato (TARGET_MONTHLY_PROFIT, default $30/mese).

2) ANALISI ESPLORATIVA E TRACCIAMENTO SOGLIE FREQUENTI: SOLO INFORMATIVA,
   non cambia il bot. Il periodo viene diviso in MESI DI CALENDARIO e ogni
   combinazione candidata viene simulata sull'intero periodo in modo continuo.
   DIP e TP vengono quindi analizzati anche in modo INCROCIATO, non soltanto
   in cascata.

   - Mantiene come baseline reale DIP 2%, TP 4%, BUY_USD attuale ($10 di default).
   - Per TP 2%, dato SELL_PERCENT=95 e MIN_ORDER_USD=$10, calcola il BUY_USD
     minimo matematico che rende il SELL eseguibile e testa quel valore e una
     griglia di BUY superiori.
   - Per ogni combinazione TP 2% + DIP + BUY viene calcolato il profitto
     realizzato per mese sulla stessa simulazione continua; si misura quante
     volte la combinazione è la migliore nel mese (frequenza) e il rendimento
     complessivo sul periodo.
   - La combinazione TP 2% più frequente viene confrontata direttamente con il
     baseline attuale. In caso di pari frequenza prevale il rendimento totale,
     poi il minor capitale necessario.
   - L'analisi precedente DIP/TP viene mantenuta come riferimento storico.

3) RIEPILOGO SINTETICO SU TELEGRAM: se TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID sono
   impostate, invia un messaggio molto compatto con capitale, risultato attuale,
   soglie più frequenti, capitale necessario per $30/mese e suggerimento operativo.

Il log completo di tutti i dettagli rimane consultabile nei log di Railway.

Include il calcolo del capitale reale (USDC + coin gestite + coin extra),
la gestione dei BUY saltati per fondi insufficienti, la stima del capitale
tramite raddoppi e bisezione, cache dei dati e dei metadata per tutta la vita
del processo e retry con backoff sulle chiamate API in caso di rate limit (429).

Nota sul TP: con BUY_USD basso e SELL_PERCENT=95, il 95% di un lotto vale
meno del minimo d'ordine finché il prezzo non è salito di circa
100/SELL_PERCENT*100 - 100 % (~5.3% con SELL_PERCENT=95). Per TP 2% il codice
calcola quindi il BUY_USD minimo effettivamente necessario, lasciando sempre
MIN_ORDER_USD=$10 come minimo dell'ordine di vendita.
"""

import json
import os
import statistics
import math
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import requests
from dotenv import load_dotenv

HOUR_MS = 3600 * 1000


# ============================================================
# RETRY CON BACKOFF SULLE CHIAMATE API (rate limit 429)
# ============================================================

def _with_retry(fn, *args, attempts=4, base_delay=5, **kwargs):
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            status = getattr(e, "status_code", None)
            rate_limited = status == 429 or "429" in str(e)

            if rate_limited and attempt < attempts:
                delay = base_delay * attempt
                log(f"API RATE LIMIT (429) | tentativo {attempt}/{attempts}, riprovo in {delay}s")
                time.sleep(delay)
                continue

            raise


# ============================================================
# DATI E SIMULAZIONE
# ============================================================

def fetch_candles(coins, days, existing_data=None, info=None, meta=None):
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    if info is None:
        info = Info(constants.MAINNET_API_URL, skip_ws=True)

    if meta is None:
        meta = _with_retry(info.spot_meta)

    usdc = next(
        i for i, t in enumerate(meta["tokens"])
        if t["name"] == "USDC"
    )

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    full_start_ms = end_ms - days * 24 * HOUR_MS

    UPDATE_HOURS = 6
    incremental_start_ms = end_ms - UPDATE_HOURS * HOUR_MS

    data = {}
    if existing_data:
        data = {
            c: list(existing_data[c])
            for c in coins
            if c in existing_data
        }

    for coin in coins:
        market = None

        for idx, token in enumerate(meta["tokens"]):
            if token["name"] in (coin, "U" + coin):
                market = next(
                    (
                        m["name"]
                        for m in meta["universe"]
                        if m["tokens"] == [idx, usdc]
                    ),
                    None,
                )
                if market:
                    break

        if not market:
            log(f"ATTENZIONE: nessun mercato spot {coin}/USDC: coin ignorata")
            continue

        if coin in data and data[coin]:
            start_ms = incremental_start_ms
        else:
            start_ms = full_start_ms

        candles = _with_retry(
            info.candles_snapshot,
            market,
            "1h",
            start_ms,
            end_ms,
        )

        new_candles = [
            {
                "t": c["t"],
                "h": float(c["h"]),
                "c": float(c["c"]),
            }
            for c in candles
        ]

        if coin not in data:
            data[coin] = new_candles
            continue

        merged = {c["t"]: c for c in data[coin]}

        for candle in new_candles:
            merged[candle["t"]] = candle

        cutoff_ms = end_ms - days * 24 * HOUR_MS

        data[coin] = [
            candle
            for candle in sorted(merged.values(), key=lambda x: x["t"])
            if candle["t"] >= cutoff_ms
        ]

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


def simulate(times, closes, highs, buy_usd, dip, tp, interval, a, unlimited_cash=False):
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
    missed_buys = 0
    min_cash = a.capital
    monthly_realized = {}

    for i in range(23, len(times), interval):
        px = {c: closes[c][i] for c in coins}

        equity = usdc + sum(l["qty"] * px[l["coin"]] for l in lots)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100) if peak > 0 else 0.0
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

            month_key = datetime.fromtimestamp(times[i] / 1000, timezone.utc).strftime("%Y-%m")
            monthly_realized[month_key] = monthly_realized.get(month_key, 0.0) + pnl

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

        cash_blocked = False

        for drop, c in drops:
            if drop < dip:
                break

            held = sum(l["qty"] for l in lots if l["coin"] == c) * px[c]

            if held + buy_usd > a.max_position or weekly.get(week, 0) >= a.weekly_buys:
                continue

            if not unlimited_cash and usdc < buy_usd:
                cash_blocked = True
                continue

            fill = px[c] * (1 + a.slippage)
            qty = buy_usd / fill * (1 - a.fee)

            usdc -= buy_usd
            min_cash = min(min_cash, usdc)
            lots.append({"coin": c, "qty": qty, "buy_price": fill, "target": fill * (1 + tp / 100), "cost": buy_usd / qty})
            weekly[week] = weekly.get(week, 0) + 1
            per[c]["buys"] += 1
            buys += 1
            cash_blocked = False
            break

        if cash_blocked:
            missed_buys += 1

    last = {c: closes[c][-1] for c in coins}
    open_value = sum(l["qty"] * last[l["coin"]] for l in lots)
    final = usdc + open_value

    for c in coins:
        c_lots = [l for l in lots if l["coin"] == c]
        per[c]["unrealized"] = sum(l["qty"] * (last[c] - l["cost"]) for l in c_lots)
        per[c]["open"] = sum(1 for l in c_lots if l["qty"] * last[c] >= a.min_order)

    return {
        "buy": buy_usd, "dip": dip, "tp": tp, "int": interval,
        "ret": (final - a.capital) / a.capital * 100 if a.capital else 0.0,
        "final": final,
        "realized": realized,
        "unrealized": open_value - sum(l["qty"] * l["cost"] for l in lots),
        "buys": buys, "sells": sells,
        "open": sum(1 for l in lots if l["qty"] * last[l["coin"]] >= a.min_order),
        "dd": max_dd,
        "deployed": max_deployed,
        "per": per,
        "missed_buys": missed_buys,
        "min_cash": min_cash,
        "monthly_realized": monthly_realized,
    }


def print_report(title, r, a, closes, days):
    print("=" * 60)
    print(title)
    print("=" * 60)
    print(f"Parametri: BUY ${r['buy']:.0f} | DIP {r['dip']:.1f}% | TP {r['tp']:.1f}% | ciclo ogni {r['int']}h | SELL {a.sell_percent:.0f}%")
    print(f"Capitale iniziale (equity odierna)  ${a.capital:>10.2f}")
    print(f"Valore finale                       ${r['final']:>10.2f}   ({r['ret']:+.2f}%)")
    print(f"  profitto realizzato               ${r['realized']:>10.2f}")
    print(f"  non realizzato                    ${r['unrealized']:>10.2f}   (lotti ancora aperti)")
    print(f"Acquisti / vendite                  {r['buys']:>4} / {r['sells']:<4}   lotti aperti: {r['open']}")
    print(f"BUY segnalati ma saltati (fondi insuff.) {r['missed_buys']:>3}")
    print(f"Max capitale investito              ${r['deployed']:>10.2f}   -> ritorno sull'investito {(r['final'] - a.capital) / r['deployed'] * 100 if r['deployed'] else 0:+.2f}%")
    print(f"Max drawdown                         {r['dd']:>10.2f}%")
    print()
    print(f"{'coin':<6} {'buy':>5} {'sell':>5} {'realiz.':>9} {'non real.':>10} {'aperti':>7} {'coin nel periodo':>17}")

    for c, v in r["per"].items():
        move = (closes[c][-1] / closes[c][23] - 1) * 100 if len(closes[c]) > 23 else 0.0
        print(f"{c:<6} {v['buys']:>5} {v['sells']:>5} {v['realized']:>9.2f} {v['unrealized']:>10.2f} {v['open']:>7} {move:>+16.1f}%")
    print("=" * 60, flush=True)


def capital_needed(times, closes, highs, buy_usd, dip, tp, interval, a):
    unlimited = simulate(times, closes, highs, buy_usd, dip, tp, interval, a, unlimited_cash=True)
    required_extra = max(0.0, -unlimited["min_cash"])
    return a.capital + required_extra


def find_capital_for_monthly_target(times, closes, highs, buy_usd, dip, tp, interval, a, target, months):
    lo = a.capital
    lo_median, _, _ = monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, lo, months)

    if lo_median >= target:
        return lo, lo_median, True

    hi = max(lo * 2, 50.0)
    prev_median = lo_median
    hi_median = lo_median

    for _ in range(12):
        hi_median, _, _ = monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, hi, months)

        if hi_median >= target:
            break

        if hi_median <= prev_median * 1.02:
            return None, hi_median, False

        prev_median = hi_median
        hi *= 2
    else:
        return None, hi_median, False

    for _ in range(12):
        mid = (lo + hi) / 2
        mid_median, _, _ = monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, mid, months)

        if mid_median >= target:
            hi = mid
        else:
            lo = mid

    final_median, _, _ = monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, hi, months)

    return hi, final_median, True


# ============================================================
# SERVIZIO & CONFIGURAZIONE
# ============================================================

load_dotenv()

sys.stdout.reconfigure(line_buffering=True)

COINS = [c.strip().upper() for c in os.getenv("COINS", "HYPE,ZEC,ETH,SOL").split(",") if c.strip()]
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "14400"))
BUY_USD = float(os.getenv("BUY_USD", "10"))

CURRENT_DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2.0"))
CURRENT_TP_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "4.0"))

SELL_PERCENT = float(os.getenv("SELL_PERCENT", "95"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "10"))

BACKTEST_DAYS = min(int(os.getenv("BACKTEST_DAYS", "200")), 208)

TARGET_MONTHLY_PROFIT = float(os.getenv("TARGET_MONTHLY_PROFIT", "30"))
GRID_SIZE = int(os.getenv("GRID_SIZE", "8"))

ACCOUNT_ADDRESS = (
    os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
    or os.getenv("HL_ACCOUNT_ADDRESS")
    or os.getenv("ACCOUNT_ADDRESS")
)

CAPITAL_EXTRA_COINS = [c.strip().upper() for c in os.getenv("CAPITAL_EXTRA_COINS", "").split(",") if c.strip()]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

args = SimpleNamespace(
    capital=float(os.getenv("BACKTEST_CAPITAL", "1000")),
    sell_percent=SELL_PERCENT,
    max_position=float(os.getenv("MAX_POSITION_USD", "200")),
    weekly_buys=int(os.getenv("MAX_WEEKLY_BUYS", "10")),
    min_order=MIN_ORDER_USD,
    fee=float(os.getenv("BACKTEST_FEE", "0.0007")),
    slippage=float(os.getenv("BACKTEST_SLIPPAGE", "0.0005")),
)


def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except Exception as e:
        log(f"TELEGRAM ERRORE | {e}")


def get_real_capital(coins, last_prices, info, meta):
    user_state = _with_retry(info.spot_user_state, ACCOUNT_ADDRESS)

    extra_prices = {}

    if CAPITAL_EXTRA_COINS:
        usdc_idx = next(i for i, t in enumerate(meta["tokens"]) if t["name"] == "USDC")

        for coin in CAPITAL_EXTRA_COINS:
            found_token = False

            for idx, token in enumerate(meta["tokens"]):
                if token["name"] in (coin, "U" + coin):
                    found_token = True

                    market = next((m["name"] for m in meta["universe"] if m["tokens"] == [idx, usdc_idx]), None)

                    if not market:
                        log(f"CAPITALE REALE | {coin}: token trovato ma nessun mercato spot {coin}/USDC")
                        break

                    book = _with_retry(info.l2_snapshot, market)
                    levels = book.get("levels", [])

                    if len(levels) == 2 and levels[0] and levels[1]:
                        extra_prices[coin] = (float(levels[0][0]["px"]) + float(levels[1][0]["px"])) / 2
                    else:
                        log(f"CAPITALE REALE | {coin}: orderbook {market} vuoto o non disponibile")

                    break

            if not found_token:
                log(f"CAPITALE REALE | {coin}: nessun token '{coin}' o 'U{coin}' nei metadata Spot")

    usdc_balance = 0.0
    coins_value = 0.0
    extra_value = 0.0

    for balance in user_state.get("balances", []):
        name = balance.get("coin")
        total = float(balance.get("total", 0) or 0)

        if name == "USDC":
            usdc_balance = total
            continue

        for coin in coins:
            if name in (coin, "U" + coin) and coin in last_prices:
                coins_value += total * last_prices[coin]
                break
        else:
            for coin in CAPITAL_EXTRA_COINS:
                if name in (coin, "U" + coin) and coin in extra_prices:
                    extra_value += total * extra_prices[coin]
                    break

    log(f"CAPITALE REALE | USDC ${usdc_balance:.2f} + coin gestite ${coins_value:.2f} + coin extra ${extra_value:.2f} = ${usdc_balance + coins_value + extra_value:.2f}")

    return usdc_balance + coins_value + extra_value


def _percentile_grid(values, floor=0.0):
    if not values:
        return [max(floor, x) for x in [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]][:GRID_SIZE]

    values = sorted(values)
    n = len(values)

    percentiles = [round(40 + i * (98.5 - 40) / (GRID_SIZE - 1), 1) for i in range(GRID_SIZE)]

    grid = []

    for p in percentiles:
        idx = min(int(n * p / 100), n - 1)
        grid.append(round(values[idx] * 2) / 2)

    seen = set()
    out = []

    for v in grid:
        v = max(floor, v)

        if v not in seen:
            seen.add(v)
            out.append(v)

    return sorted(out)


def build_dip_grid(closes, highs):
    drops = []

    for c, cl in closes.items():
        hi = highs[c]

        for i in range(23, len(cl)):
            h24 = max(hi[i - 23:i + 1])

            if h24 > 0:
                d = (h24 - cl[i]) / h24 * 100

                if d > 0:
                    drops.append(d)

    return _percentile_grid(drops, floor=0.5)


def build_tp_grid(closes):
    rises = []

    for c, cl in closes.items():
        for i in range(23, len(cl)):
            low24 = min(cl[i - 23:i + 1])

            if low24 > 0:
                r = (cl[i] - low24) / low24 * 100

                if r > 0:
                    rises.append(r)

    effective_floor = (100 / SELL_PERCENT * 100 - 100) if SELL_PERCENT < 100 else 0.0
    floor = max(0.5, round((effective_floor + 0.3) * 2) / 2)

    return _percentile_grid(rises, floor=floor)


def calendar_months_in_range(times):
    first = datetime.fromtimestamp(times[0] / 1000, timezone.utc)
    last = datetime.fromtimestamp(times[-1] / 1000, timezone.utc)

    months = []
    y, m = first.year, first.month

    while (y, m) <= (last.year, last.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    return months


def monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, capital, months):
    a2 = SimpleNamespace(**vars(a))
    a2.capital = capital

    r = simulate(times, closes, highs, buy_usd, dip, tp, interval, a2)
    pnls = monthly_pnls(r, months)

    median = statistics.median(pnls)
    stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0

    return median, stdev, r


def monthly_pnls(result, months):
    mr = result["monthly_realized"]
    return [mr.get(m, 0.0) for m in months]


def find_target_param(label, times, closes, highs, candidates, months, sim_fn, target, results):
    print(f"\nRICERCA {label} PER OBIETTIVO ~${target:.0f}/MESE (mediana su {len(months)} mesi di calendario)")
    print("-" * 70)
    print(f"{label:<8} {'mediana €/mese':>15} {'dev.std':>10} {'scarto da target':>18} {'rendimento tot.':>16}")

    scored = []

    for v in candidates:
        r = sim_fn(times, closes, highs, v)
        pnls = monthly_pnls(r, months)

        median = statistics.median(pnls)
        stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
        diff = abs(median - target)

        scored.append((diff, stdev, v, r, median))

        print(f"{v:>6.1f}% {median:>15.2f} {stdev:>10.2f} {diff:>18.2f} {r['ret']:>15.2f}%")

    scored.sort(key=lambda x: (x[0], x[1]))

    best_diff, best_stdev, best_v, best_r, best_median = scored[0]

    print(f"\n-> {label} PIU' VICINO ALL'OBIETTIVO: {best_v:.1f}% (mediana ${best_median:.2f}/mese, scarto ${best_diff:.2f}, dev.std ${best_stdev:.2f})\n")

    return best_v, best_r, best_median, best_stdev



# ============================================================
# ANALISI INCROCIATA TP 2% + DIP + BUY_USD
# ============================================================

def minimum_buy_for_tp(tp, sell_percent, min_order, fee):
    """BUY minimo affinché il SELL_PERCENT del lotto raggiunga MIN_ORDER_USD."""
    if sell_percent <= 0 or (1 + tp / 100) <= 0:
        return float("inf")
    factor = (sell_percent / 100) * (1 + tp / 100) * (1 - fee)
    return min_order / factor if factor > 0 else float("inf")


def build_tp2_buy_grid(a, tp=2.0):
    """Griglia centrata sul minimo eseguibile, senza mai abbassare MIN_ORDER_USD."""
    minimum = minimum_buy_for_tp(tp, a.sell_percent, a.min_order, a.fee)
    minimum = math.ceil(minimum * 100) / 100

    # Il primo valore è il minimo eseguibile al centesimo; gli altri servono
    # a verificare se un BUY leggermente maggiore migliora robustezza/rendimento.
    candidates = [
        minimum,
        math.ceil((minimum + 0.25) * 100) / 100,
        math.ceil((minimum + 0.50) * 100) / 100,
        11.0,
        12.0,
        15.0,
        20.0,
    ]
    return sorted(set(round(x, 2) for x in candidates if x > a.min_order))


def cross_tp2_analysis(times, closes, highs, dip_grid, months, a, target=2.0):
    """Analizza DIP x BUY per TP fisso al 2% su simulazioni continue."""
    buy_grid = build_tp2_buy_grid(a, target)
    print(f"\nANALISI INCROCIATA TP {target:.1f}% | DIP x BUY_USD")
    print(f"BUY minimo eseguibile con MIN_ORDER ${a.min_order:.2f}: ${buy_grid[0]:.2f}")
    print("-" * 90)

    results = []
    for dip in dip_grid:
        for buy in buy_grid:
            r = simulate(times, closes, highs, buy, dip, target, 1, a)
            pnls = monthly_pnls(r, months)
            results.append({
                "dip": dip,
                "buy": buy,
                "tp": target,
                "r": r,
                "pnls": pnls,
                "median": statistics.median(pnls),
            })

    # Frequenza: quante volte la combinazione è prima tra tutte le combinazioni.
    # In caso di pari profitto mensile, vengono assegnate tutte le combinazioni
    # a pari merito, così la frequenza non dipende dall'ordine del ciclo.
    wins = Counter()
    for idx, month in enumerate(months):
        vals = [(x["pnls"][idx], x) for x in results]
        best_month = max(v for v, _ in vals)
        for value, x in vals:
            if abs(value - best_month) < 1e-9:
                wins[(x["dip"], x["buy"])] += 1

    for x in results:
        x["wins"] = wins[(x["dip"], x["buy"])]
        x["freq"] = x["wins"] / len(months) * 100 if months else 0.0

    # Prima frequenza, poi rendimento totale, poi capitale richiesto per non
    # saltare BUY, poi minore BUY. Questo privilegia robustezza senza perdere
    # il rendimento complessivo.
    for x in results:
        x["capital_required"] = capital_needed(
            times, closes, highs, x["buy"], x["dip"], target, 1, a
        )

    best = max(
        results,
        key=lambda x: (
            x["freq"],
            x["r"]["ret"],
            -x["capital_required"],
            -x["buy"],
        ),
    )

    # Migliore combinazione per rendimento complessivo, utile per il confronto.
    best_return = max(results, key=lambda x: x["r"]["ret"])

    print(f"Combinazione più frequente: DIP {best['dip']:.1f}% / TP {target:.1f}% / BUY ${best['buy']:.2f} -> {best['freq']:.0f}% dei mesi, rendimento {best['r']['ret']:+.2f}%")
    print(f"Miglior rendimento pieno periodo: DIP {best_return['dip']:.1f}% / TP {target:.1f}% / BUY ${best_return['buy']:.2f} -> {best_return['r']['ret']:+.2f}%")

    return best, best_return, results, buy_grid


def cross_recommendation(current_result, cross_best, cross_best_return, current_required, cross_capital):
    """Suggerimento basato su frequenza + rendimento, senza sostituire automaticamente il baseline."""
    cb = cross_best["r"]
    freq = cross_best["freq"]
    current_ret = current_result["ret"]
    cross_ret = cb["ret"]

    if freq >= 60 and cross_ret > current_ret:
        return (
            f"Valutare TP 2%: DIP {cross_best['dip']:.1f}% / BUY ${cross_best['buy']:.2f}; "
            f"vincente nel {freq:.0f}% dei mesi e rendimento {cross_ret:+.2f}% vs attuale {current_ret:+.2f}%."
        )
    if freq >= 50 and cross_ret > current_ret:
        return (
            f"TP 2% interessante ma non ancora dominante: DIP {cross_best['dip']:.1f}% / BUY ${cross_best['buy']:.2f}; "
            f"frequenza {freq:.0f}%, rendimento {cross_ret:+.2f}% vs attuale {current_ret:+.2f}%."
        )
    if cross_ret > current_ret:
        return (
            f"TP 2% ha rendimento superiore ({cross_ret:+.2f}%), ma frequenza solo {freq:.0f}%; "
            f"per ora mantenere DIP {CURRENT_DIP_PERCENT:.1f}% / TP {CURRENT_TP_PERCENT:.1f}%."
        )
    return (
        f"Mantenere DIP {CURRENT_DIP_PERCENT:.1f}% / TP {CURRENT_TP_PERCENT:.1f}%: "
        f"TP 2% non supera l'attuale ({cross_ret:+.2f}% vs {current_ret:+.2f}%)."
    )


# ============================================================
# CACHE E STORICO IN MEMORIA
# ============================================================

DATA_CACHE = None
INFO_CACHE = None
META_CACHE = None

DIP_HISTORY = []
TP_HISTORY = []

HISTORY_MAX = int(os.getenv("HISTORY_MAX", "200"))
STABILITY_MIN_SAMPLES = int(os.getenv("STABILITY_MIN_SAMPLES", "5"))
STABILITY_BAND = float(os.getenv("STABILITY_BAND", "1.0"))


# ============================================================
# RUNNER PRINCIPALE DEL CICLO
# ============================================================

def run():
    global DATA_CACHE, INFO_CACHE, META_CACHE, DIP_HISTORY, TP_HISTORY

    log("============================================================")
    log("AVVIO NUOVO CICLO DI BACKTEST")
    log("============================================================")

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    if INFO_CACHE is None:
        INFO_CACHE = Info(constants.MAINNET_API_URL, skip_ws=True)

    if META_CACHE is None:
        META_CACHE = _with_retry(INFO_CACHE.spot_meta)

    log(f"Download/Aggiornamento candele 1h per {COINS} sugli ultimi {BACKTEST_DAYS} giorni...")
    DATA_CACHE = fetch_candles(
        COINS, BACKTEST_DAYS, existing_data=DATA_CACHE, info=INFO_CACHE, meta=META_CACHE
    )

    times, closes, highs = align(DATA_CACHE)
    months = calendar_months_in_range(times)
    num_months = len(months)

    last_prices = {c: closes[c][-1] for c in COINS}

    if ACCOUNT_ADDRESS:
        try:
            real_cap = get_real_capital(COINS, last_prices, INFO_CACHE, META_CACHE)
            args.capital = real_cap
        except Exception as e:
            log(f"ATTENZIONE: Errore lettura capitale reale dal conto ({e}). Uso default ${args.capital:.2f}")
            traceback.print_exc()

    log(f"Capitale di partenza simulato: ${args.capital:.2f}")

    # ============================================================
    # 1) PARAMETRI ATTUALI
    # ============================================================
    log("\n1) SIMULAZIONE PARAMETRI ATTUALI DEL BOT...")
    current_result = simulate(
        times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, 1, args
    )
    current_required = capital_needed(
        times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, 1, args
    )

    cap_for_30_curr, _, reach_curr = find_capital_for_monthly_target(
        times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, 1, args, TARGET_MONTHLY_PROFIT, months
    )

    print_report("PARAMETRI ATTUALI DEL BOT", current_result, args, closes, BACKTEST_DAYS)

    # ============================================================
    # 2) ANALISI ESPLORATIVA & SOGLIE FREQUENTI
    # ============================================================
    log("\n2) ANALISI ESPLORATIVA DIP/TP E INCROCIO TP 2%...")
    dip_grid = build_dip_grid(closes, highs)
    tp_grid = build_tp_grid(closes)

    # Analisi storica precedente: DIP e TP in cascata sull'obiettivo mensile.
    best_dip, dip_r, dip_median, dip_stdev = find_target_param(
        "DIP",
        times,
        closes,
        highs,
        dip_grid,
        months,
        lambda t, c, h, v: simulate(t, c, h, BUY_USD, v, CURRENT_TP_PERCENT, 1, args),
        TARGET_MONTHLY_PROFIT,
        results={},
    )

    best_tp, combined_r, combined_median, combined_stdev = find_target_param(
        "TP",
        times,
        closes,
        highs,
        tp_grid,
        months,
        lambda t, c, h, v: simulate(t, c, h, BUY_USD, best_dip, v, 1, args),
        TARGET_MONTHLY_PROFIT,
        results={},
    )

    cap_for_30_best, _, reach_best = find_capital_for_monthly_target(
        times, closes, highs, BUY_USD, best_dip, best_tp, 1, args, TARGET_MONTHLY_PROFIT, months
    )

    # Nuova analisi richiesta: vero incrocio DIP x BUY con TP 2%, mantenendo
    # MIN_ORDER_USD = $10. Il BUY minimo viene calcolato, non scelto arbitrariamente.
    cross_best, cross_best_return, cross_results, tp2_buy_grid = cross_tp2_analysis(
        times, closes, highs, dip_grid, months, args, target=2.0
    )

    cross_cap_for_30, _, cross_reach = find_capital_for_monthly_target(
        times, closes, highs,
        cross_best["buy"], cross_best["dip"], 2.0, 1, args,
        TARGET_MONTHLY_PROFIT, months
    )

    DIP_HISTORY.append(best_dip)
    TP_HISTORY.append(best_tp)
    if len(DIP_HISTORY) > HISTORY_MAX:
        DIP_HISTORY.pop(0)
        TP_HISTORY.pop(0)

    mode_dip = Counter(DIP_HISTORY).most_common(1)[0][0]
    mode_tp = Counter(TP_HISTORY).most_common(1)[0][0]

    n_samples = len(DIP_HISTORY)
    freq_dip = (sum(1 for x in DIP_HISTORY if abs(x - mode_dip) <= STABILITY_BAND) / n_samples) * 100
    freq_tp = (sum(1 for x in TP_HISTORY if abs(x - mode_tp) <= STABILITY_BAND) / n_samples) * 100

    suggestion = cross_recommendation(
        current_result,
        cross_best,
        cross_best_return,
        current_required,
        cross_cap_for_30,
    )

    # ============================================================
    # 3) MESSAGGIO TELEGRAM SINTETICO
    # ============================================================
    cap_curr_txt = f"${cap_for_30_curr:.0f}" if (reach_curr and cap_for_30_curr) else "N/D"
    cap_cross_txt = f"${cross_cap_for_30:.0f}" if (cross_reach and cross_cap_for_30) else "N/D"

    tg_msg = (
        f"Backtest {num_months} mesi | capitale ${args.capital:.2f}\n"
        f"Attuale: DIP {CURRENT_DIP_PERCENT:.1f}%/TP {CURRENT_TP_PERCENT:.1f}% -> profitto {BACKTEST_DAYS}gg: ${current_result['realized']:+.2f}\n"
        f"Più frequente: DIP {mode_dip:.1f}%/TP {mode_tp:.1f}%\n"
        f"Capitale x ${TARGET_MONTHLY_PROFIT:.0f}/mese con target attuali: {cap_curr_txt}\n"
        f"TP 2%: DIP {cross_best['dip']:.1f}% / BUY ${cross_best['buy']:.2f} -> {cross_best['freq']:.0f}% mesi, rendimento {cross_best['r']['ret']:+.2f}%\n"
        f"Capitale x ${TARGET_MONTHLY_PROFIT:.0f}/mese TP 2%: {cap_cross_txt}\n"
        f"Suggerimento: {suggestion}"
    )

    send_telegram(tg_msg)
    log(f"\n[TELEGRAM INVIATO]\n{tg_msg}")


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":
    log("Servizio Backtest Avviato...")
    while True:
        try:
            run()
        except Exception as e:
            log(f"ERRORE NEL CICLO DI BACKTEST: {e}")
            traceback.print_exc()

        log(f"In attesa del prossimo ciclo tra {LOOP_INTERVAL_SECONDS} secondi...")
        time.sleep(LOOP_INTERVAL_SECONDS)
