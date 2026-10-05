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
   non cambia il bot. Il periodo viene diviso in MESI DI CALENDARIO (non
   finestre arbitrarie), e per ciascun DIP/TP candidato si calcola la
   MEDIANA del profitto REALIZZATO per mese di calendario, su un'UNICA
   simulazione continua sull'intero periodo (i lotti aperti in un mese
   possono chiudersi nel mese successivo, come accade davvero al bot: non
   si riparte da zero a ogni mese). 

   - Viene identificata la combinazione ottimale del singolo ciclo (DIP e TP
     incrociati in cascata su obiettivo mensile) e ne viene calcolato il
     capitale necessario per raggiungere il target di $30/mese.
   - I valori ottimali di ogni ciclo vengono salvati in memoria (DIP_HISTORY e
     TP_HISTORY) per determinare la MODA e la STABILITÀ (frequenza %) dei parametri
     nel tempo.
   - Viene generato un SUGGERIMENTO OPERATIVO basato sulla convergenza storica:
     consiglia di cambiare target solo se la moda differisce dai parametri attuali
     con una frequenza/stabilità >= 60% e un numero minimo di cicli di osservazione.

3) RIEPILOGO SYNTHETIC SU TELEGRAM: se TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID sono
   impostate, invia un messaggio ultra-compatto della seguente struttura:
   
     Backtest {X} mesi | capitale ${Cap}
     Attuale: DIP {X}%/TP {Y}% -> profitto {D}gg: ${P}
     Capitale x ${Target}/mese: ${CapReq} (req. reale: ${ReqReale})

     Più frequente: DIP {X}%/TP {Y}% (ottimale ciclo: DIP {A}%/TP {B}%)
     Capitale x ${Target}/mese (con target ottimali): ${CapOpt}

     Suggerimento: {Suggerimento Operativo}

   Il log completo di tutti i dettagli rimane consultabile nei log di Railway.

Include il calcolo del capitale reale (USDC + coin gestite + coin extra),
la gestione dei BUY saltati per fondi insufficienti, la stima del capitale
tramite raddoppi e bisezione (find_capital_for_monthly_target), cache dei dati e
dei metadata per tutta la vita del processo (solo le ultime ore vengono
riscaricate a ogni ciclo, non l'intero periodo), e retry con backoff sulle
chiamate API in caso di rate limit (429).

Nota sul TP: con BUY_USD basso e SELL_PERCENT=95, il 95% di un lotto vale
meno del minimo d'ordine finché il prezzo non è salito di circa
100/SELL_PERCENT*100 - 100 % (~5.3% con SELL_PERCENT=95). Un
TAKE_PROFIT_PERCENT sotto quella soglia non ha alcun effetto: il lotto resta
scartato dal controllo sul minimo d'ordine indipendentemente dal target. La
griglia del TP parte sempre da sopra questa soglia.
"""

import json
import os
import statistics
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
    log("\n2) ANALISI ESPLORATIVA DIP E TP SU OBIETTIVO MENSILE...")
    dip_grid = build_dip_grid(closes, highs)
    tp_grid = build_tp_grid(closes)

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

    if n_samples < STABILITY_MIN_SAMPLES:
        suggestion = "In raccolta dati (pochi cicli per suggerire modifiche)."
    elif (mode_dip != CURRENT_DIP_PERCENT or mode_tp != CURRENT_TP_PERCENT) and (freq_dip >= 60 and freq_tp >= 60):
        suggestion = f"CONSIGLIATO CAMBIO -> DIP {mode_dip:.1f}% / TP {mode_tp:.1f}% (stabili al {freq_dip:.0f}%)."
    elif mode_dip == CURRENT_DIP_PERCENT and mode_tp == CURRENT_TP_PERCENT:
        suggestion = "Mantenere parametri attuali (coincidono con la moda storica)."
    else:
        suggestion = "Parametri variabili tra i cicli: consiglia di mantenere gli attuali per stabilità."

    # ============================================================
    # 3) MESSAGGIO TELEGRAM SINTETICO
    # ============================================================
    cap_curr_txt = f"${cap_for_30_curr:.0f}" if (reach_curr and cap_for_30_curr) else "N/D (limite bot)"
    cap_best_txt = f"${cap_for_30_best:.0f}" if (reach_best and cap_for_30_best) else "N/D (limite bot)"

    tg_msg = (
        f"Backtest {num_months} mesi | capitale ${args.capital:.2f}\n\n"
        f"Attuale: DIP {CURRENT_DIP_PERCENT:.1f}%/TP {CURRENT_TP_PERCENT:.1f}% -> profitto {BACKTEST_DAYS}gg: ${current_result['realized']:+.2f}\n"
        f"Capitale x ${TARGET_MONTHLY_PROFIT:.0f}/mese: {cap_curr_txt} (req. reale: ${current_required:.2f})\n\n"
        f"Più frequente: DIP {mode_dip:.1f}%/TP {mode_tp:.1f}% (ottimale ciclo: DIP {best_dip:.1f}%/TP {best_tp:.1f}%)\n"
        f"Capitale x ${TARGET_MONTHLY_PROFIT:.0f}/mese (con target ottimali): {cap_best_txt}\n\n"
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
