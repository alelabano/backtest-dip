"""
Servizio backtest per Railway. Ogni LOOP_INTERVAL_SECONDS (default 4 ore) fa
tre cose distinte:

1) CAPITALE NECESSARIO PER I PARAMETRI ATTUALI DEL BOT: simula il bot con i
   parametri realmente in uso (DIP_PERCENT e TAKE_PROFIT_PERCENT letti
   dall'ambiente, gli stessi del bot vero) sugli ultimi BACKTEST_DAYS giorni,
   e calcola quanto capitale servirebbe per non saltare nessun BUY.

2) ANALISI ESPLORATIVA DI DIP E TP SU OBIETTIVO MENSILE: SOLO INFORMATIVA,
   non cambia il bot. Il periodo viene diviso in MESI DI CALENDARIO (non
   finestre arbitrarie), e per ciascun DIP/TP candidato si calcola la
   MEDIANA del profitto REALIZZATO per mese di calendario, su un'UNICA
   simulazione continua sull'intero periodo (i lotti aperti in un mese
   possono chiudersi nel mese successivo, come accade davvero al bot: non
   si riparte da zero a ogni mese). Si sceglie il valore la cui mediana
   mensile e' piu' vicina a TARGET_MONTHLY_PROFIT (default $30/mese), con
   la deviazione standard tra i mesi come criterio secondario (un valore
   costante mese per mese e' preferibile a uno che rende bene solo in un
   mese fortunato). Prima si cerca il DIP migliore (TP fisso a quello
   attuale), poi SU QUEL DIP si cerca il TP migliore: i due parametri
   vengono incrociati in cascata, non cercati in isolamento. Il risultato
   trovato viene confrontato con i parametri attuali con un giudizio
   testuale esplicito (quattro casi: piu' vicino e piu' costante, piu'
   vicino ma meno costante, piu' costante ma piu' lontano, nessun
   vantaggio), che include anche BUY saltati e capitale necessario come
   fattori di rischio secondari.

3) RIEPILOGO SU TELEGRAM: se TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID sono
   impostate, invia un messaggio breve con i numeri principali e il
   giudizio. Il log completo resta solo nei log di Railway.

Include il calcolo del capitale reale (USDC + coin gestite + coin extra),
la gestione dei BUY saltati per fondi insufficienti, cache dei dati e dei
metadata per tutta la vita del processo (solo le ultime ore vengono
riscaricate a ogni ciclo, non l'intero periodo), e retry con backoff sulle
chiamate API in caso di rate limit (429).

Nota sul TP: con BUY_USD basso e SELL_PERCENT=95, il 95% di un lotto vale
meno del minimo d'ordine finche' il prezzo non e' salito di circa
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
    # Un 429 e' quasi sempre transitorio (rate limit dell'API pubblica di
    # Hyperliquid, non un errore nei dati o nel codice). Senza retry, un
    # singolo 429 fa fallire l'intero ciclo e si aspetta LOOP_INTERVAL_SECONDS
    # (di default 4 ore) prima di riprovare: con un backoff di pochi secondi
    # si risolve quasi sempre subito.
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
    """
    Primo avvio:
        scarica l'intero periodo di BACKTEST_DAYS.

    Cicli successivi:
        scarica solo le ultime UPDATE_HOURS ore e fa merge per timestamp,
        sostituendo anche la candela ancora in formazione.
    """
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

    # Aggiorniamo una finestra sufficientemente ampia da comprendere
    # la candela corrente e quelle eventualmente mancanti dall'ultimo ciclo.
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
            print(
                f"ATTENZIONE: nessun mercato spot {coin}/USDC: coin ignorata"
            )
            continue

        # Se abbiamo già dati locali, aggiorniamo solo le ultime ore.
        # Al primo avvio scarichiamo tutto il periodo.
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

        # Merge per timestamp.
        # Le nuove candele sostituiscono quelle già presenti,
        # indispensabile per aggiornare la candela 1h ancora aperta.
        merged = {c["t"]: c for c in data[coin]}

        for candle in new_candles:
            merged[candle["t"]] = candle

        # Mantieni sempre soltanto la finestra mobile di BACKTEST_DAYS.
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
    monthly_realized = {}  # "YYYY-MM" -> profitto realizzato in quel mese di calendario

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

            month_key = datetime.fromtimestamp(times[i] / 1000, timezone.utc).strftime("%Y-%m")
            monthly_realized[month_key] = monthly_realized.get(month_key, 0.0) + pnl

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
        "ret": (final - a.capital) / a.capital * 100,
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
        move = (closes[c][-1] / closes[c][23] - 1) * 100
        print(f"{c:<6} {v['buys']:>5} {v['sells']:>5} {v['realized']:>9.2f} {v['unrealized']:>10.2f} {v['open']:>7} {move:>+16.1f}%")
    print("=" * 60, flush=True)


def capital_needed(times, closes, highs, buy_usd, dip, tp, interval, a):
    # Quanto capitale servirebbe per non saltare nessun BUY: rifa la
    # simulazione permettendo al saldo USDC di andare virtualmente sotto
    # zero, e misura il punto peggiore raggiunto.
    unlimited = simulate(times, closes, highs, buy_usd, dip, tp, interval, a, unlimited_cash=True)
    required_extra = max(0.0, -unlimited["min_cash"])
    return a.capital + required_extra


def find_capital_for_monthly_target(times, closes, highs, buy_usd, dip, tp, interval, a, target, months):
    # Cerca, provando capitali crescenti (raddoppiando), quello che porta la
    # mediana mensile al target. Riusa monthly_median_at_capital (stessa
    # funzione del confronto "capitale sufficiente" qui sopra) invece di
    # duplicarla. Se la mediana smette di crescere prima di arrivare al
    # target, il capitale non e' (piu') il vincolo: lo e' qualcos'altro
    # (MAX_WEEKLY_BUYS o MAX_POSITION_USD), e nessuna quantita' di capitale
    # aggiuntivo risolverebbe da sola il problema — un'informazione
    # altrettanto utile del numero stesso.
    lo = a.capital
    lo_median, _, _ = monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, lo, months)

    if lo_median >= target:
        return lo, lo_median, True

    hi = max(lo * 2, 50.0)
    prev_median = lo_median
    hi_median = lo_median

    for _ in range(12):  # fino a 12 raddoppi: lo*4096, ampio margine
        hi_median, _, _ = monthly_median_at_capital(times, closes, highs, buy_usd, dip, tp, interval, a, hi, months)

        if hi_median >= target:
            break

        if hi_median <= prev_median * 1.02:  # non cresce quasi piu': plateau
            return None, hi_median, False

        prev_median = hi_median
        hi *= 2
    else:
        return None, hi_median, False  # mai arrivato al target in 12 raddoppi

    # bisezione tra lo e hi per restringere il capitale esatto
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
# SERVIZIO
# ============================================================

load_dotenv()

sys.stdout.reconfigure(line_buffering=True)

COINS = [c.strip().upper() for c in os.getenv("COINS", "HYPE,ZEC,ETH,SOL").split(",") if c.strip()]
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "14400"))
BUY_USD = float(os.getenv("BUY_USD", "10"))

# Parametri REALMENTE in uso sul bot: per il confronto "capitale necessario
# per i parametri attuali", tienili identici a quelli del servizio bot.
CURRENT_DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2.0"))
CURRENT_TP_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "4.0"))

SELL_PERCENT = float(os.getenv("SELL_PERCENT", "95"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "10"))

# L'API restituisce al massimo ~5000 candele 1h, circa 208 giorni
BACKTEST_DAYS = min(int(os.getenv("BACKTEST_DAYS", "200")), 208)

# Analisi esplorativa (solo informativa, non applicata al bot): cerca DIP e
# TP la cui mediana di profitto REALIZZATO per mese di calendario sia piu'
# vicina a questo obiettivo, non il valore che "vince piu' spesso" in
# astratto.
TARGET_MONTHLY_PROFIT = float(os.getenv("TARGET_MONTHLY_PROFIT", "30"))
GRID_SIZE = int(os.getenv("GRID_SIZE", "8"))

# Capitale iniziale del backtest = saldo reale del conto (USDC + coin gestite
# + coin extra), letto in sola lettura: NON serve la chiave privata, basta
# l'indirizzo pubblico. Accetta uno qualsiasi di questi tre nomi di variabile.
ACCOUNT_ADDRESS = (
    os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
    or os.getenv("HL_ACCOUNT_ADDRESS")
    or os.getenv("ACCOUNT_ADDRESS")
)

# Coin presenti sul conto ma NON gestite da questo bot (es. BTC dell'altro
# bot): il loro valore entra nel capitale iniziale del backtest, ma non
# nella simulazione di acquisto/vendita.
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
    # Saldo reale sul conto: USDC + valore delle coin gestite dal bot, al prezzo
    # di chiusura piu' recente (stesso dato del backtest, nessuna chiamata extra
    # all'orderbook), + valore delle coin extra (CAPITAL_EXTRA_COINS) al prezzo
    # spot corrente, per coin sul conto ma non gestite da questo bot (es. BTC).
    # Riusa l'Info e i metadata gia' scaricati in run() (INFO_CACHE/META_CACHE):
    # nessuna chiamata spot_meta() aggiuntiva a ogni ciclo.
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
    # Griglia adattiva: percentili della distribuzione di valori osservati
    # (ribassi per il DIP, rialzi per il TP), non una lista fissa. Solo la
    # "coda" alta della distribuzione rappresenta movimenti degni di nota
    # (la maggior parte del tempo il prezzo e' vicino al suo estremo
    # recente, quindi i percentili bassi darebbero soglie vicine a 0).
    if not values:
        return [max(floor, x) for x in [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]][:GRID_SIZE]

    values = sorted(values)
    n = len(values)

    percentiles = [round(40 + i * (98.5 - 40) / (GRID_SIZE - 1), 1) for i in range(GRID_SIZE)]

    grid = []

    for p in percentiles:
        idx = min(int(n * p / 100), n - 1)
        grid.append(round(values[idx] * 2) / 2)  # arrotonda a 0.5

    seen = set()
    out = []

    for v in grid:
        v = max(floor, v)

        if v not in seen:
            seen.add(v)
            out.append(v)

    return sorted(out)


def build_dip_grid(closes, highs):
    # Ribassi osservati rispetto al massimo mobile a 24h (stesso calcolo
    # usato dal bot per il riferimento del primo lotto di ogni coin).
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
    # Rialzi osservati rispetto al minimo mobile a 24h: stessa idea del
    # calcolo del DIP, ma capovolta (quanto il prezzo e' salito dal suo
    # minimo recente), per stimare l'ampiezza tipica dei movimenti verso
    # l'alto in questi dati. La soglia minima e' vincolata dal minimo
    # d'ordine: con SELL_PERCENT < 100, un TP sotto circa
    # (100/SELL_PERCENT*100 - 100)% non ha alcun effetto (vedi nota in testa
    # al file), quindi la griglia non propone mai valori sotto quella soglia.
    rises = []

    for c, cl in closes.items():
        for i in range(23, len(cl)):
            low24 = min(cl[i - 23:i + 1])

            if low24 > 0:
                r = (cl[i] - low24) / low24 * 100

                if r > 0:
                    rises.append(r)

    effective_floor = (100 / SELL_PERCENT * 100 - 100) if SELL_PERCENT < 100 else 0.0
    floor = max(0.5, round((effective_floor + 0.3) * 2) / 2)  # un po' sopra la soglia, non esattamente sul confine

    return _percentile_grid(rises, floor=floor)


def calendar_months_in_range(times):
    # Elenco ordinato di tutti i mesi di calendario (UTC) coperti dai dati,
    # compresi quelli senza nessuna vendita (contano come $0 per quel mese,
    # non vengono ignorati: un parametro che non vende per mesi deve
    # risultare peggiore, non uscire dal conteggio).
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
    # Non basta sapere QUANTO capitale servirebbe per non saltare BUY: serve
    # anche sapere cosa produrrebbe davvero quel capitale al mese. Rifa' la
    # simulazione con quel capitale come punto di partenza (non quello reale
    # attuale), e calcola la stessa mediana mensile usata per il confronto
    # con l'obiettivo. Con piu' capitale il bot apre piu' posizioni in
    # parallelo, quindi il profitto in dollari sale anche se il rendimento
    # percentuale puo' restare simile o scendere leggermente.
    a2 = SimpleNamespace(**vars(a))
    a2.capital = capital

    r = simulate(times, closes, highs, buy_usd, dip, tp, interval, a2)
    pnls = monthly_pnls(r, months)

    median = statistics.median(pnls)
    stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0

    return median, stdev, r


def monthly_pnls(result, months):
    # Profitto realizzato per ciascun mese di calendario nel periodo, nello
    # stesso ordine di 'months'. Un mese senza vendite vale 0.0 (non viene
    # saltato): un parametro che resta fermo per mesi deve pesare come tale.
    mr = result["monthly_realized"]
    return [mr.get(m, 0.0) for m in months]


def find_target_param(label, times, closes, highs, candidates, months, sim_fn, target, results):
    # Generico: usato sia per il DIP che per il TP. Al posto di "quale valore
    # vince piu' finestre" (astratto, slegato da un obiettivo concreto),
    # sceglie il valore la cui MEDIANA di profitto realizzato per mese di
    # calendario e' piu' vicina a TARGET_MONTHLY_PROFIT, con una singola
    # simulazione continua sull'intero periodo (non finestre indipendenti:
    # i lotti apribili in un mese possono chiudersi nel mese successivo,
    # come accade davvero al bot). La deviazione standard tra i mesi serve
    # da criterio secondario: tra due valori ugualmente vicini al target,
    # vince quello piu' COSTANTE mese per mese, non quello con un singolo
    # mese fortunato che alza la media.
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

    scored.sort(key=lambda x: (x[0], x[1]))  # scarto dal target, poi costanza

    best_diff, best_stdev, best_v, best_r, best_median = scored[0]

    print(f"\n-> {label} PIU' VICINO ALL'OBIETTIVO: {best_v:.1f}% (mediana ${best_median:.2f}/mese, scarto ${best_diff:.2f}, dev.std ${best_stdev:.2f})\n")

    return best_v, best_r, best_median, best_stdev


def monthly_verdict_text(target, current_median, current_stdev, combined_median, combined_stdev,
                          current, combined, current_required, combined_required):
    # Giudizio esplicito, non solo numeri: l'obiettivo e' avvicinare la
    # mediana di profitto REALIZZATO per mese di calendario a
    # TARGET_MONTHLY_PROFIT, in modo COSTANTE (dev.std bassa), non ottenere
    # un rendimento totale piu' alto che magari dipende da un solo mese
    # fortunato. Buy saltati e capitale necessario restano criteri di
    # rischio secondari, menzionati ma non decisivi da soli.
    current_diff = abs(current_median - target)
    combined_diff = abs(combined_median - target)

    closer_to_target = combined_diff < current_diff
    more_consistent = combined_stdev < current_stdev
    not_worse_missed = combined["missed_buys"] <= current["missed_buys"]
    not_worse_capital = combined_required <= current_required

    risk_notes = []

    if not not_worse_missed:
        risk_notes.append(f"salta piu' BUY ({combined['missed_buys']} contro {current['missed_buys']})")

    if not not_worse_capital:
        risk_notes.append(f"richiede piu' capitale (${combined_required:.2f} contro ${current_required:.2f})")

    risk_suffix = f" Attenzione: {' e '.join(risk_notes)}." if risk_notes else ""

    if closer_to_target and more_consistent:
        return (
            f"PIU' VICINO E PIU' COSTANTE: mediana ${combined_median:.2f}/mese (obiettivo ${target:.0f}) "
            f"contro ${current_median:.2f}/mese attuale, con meno variabilita' tra un mese e l'altro "
            f"(dev.std ${combined_stdev:.2f} contro ${current_stdev:.2f})." + risk_suffix
        )

    if closer_to_target and not more_consistent:
        return (
            f"PIU' VICINO ALL'OBIETTIVO ma meno costante: mediana ${combined_median:.2f}/mese "
            f"(obiettivo ${target:.0f}) contro ${current_median:.2f}/mese attuale, ma la variabilita' "
            f"tra i mesi e' maggiore (dev.std ${combined_stdev:.2f} contro ${current_stdev:.2f}): "
            "alcuni mesi potrebbero rendere molto piu' o molto meno del target." + risk_suffix
        )

    if not closer_to_target and more_consistent:
        return (
            f"PIU' COSTANTE ma piu' lontano dall'obiettivo: mediana ${combined_median:.2f}/mese contro "
            f"${current_median:.2f}/mese attuale (obiettivo ${target:.0f}), con meno variabilita' tra i "
            f"mesi (dev.std ${combined_stdev:.2f} contro ${current_stdev:.2f}). Puo' valere la pena se "
            "preferisci un risultato piu' prevedibile a uno piu' vicino al target ma irregolare." + risk_suffix
        )

    return (
        f"NESSUN VANTAGGIO chiaro rispetto ai parametri attuali verso l'obiettivo di ${target:.0f}/mese: "
        f"mediana ${combined_median:.2f}/mese contro ${current_median:.2f}/mese attuale, ne' piu' vicina "
        "ne' piu' costante. I parametri attuali restano la scelta piu' difendibile per ora." + risk_suffix
    )




# Cache in memoria del servizio Railway.
# Il primo ciclo scarica 200 giorni; i successivi aggiornano solo le ultime ore.
DATA_CACHE = None
INFO_CACHE = None
META_CACHE = None

# Storico dei valori "piu' frequenti" trovati a ogni ciclo, per DIP e TP.
# Vive solo in memoria per la vita del processo: si azzera a ogni redeploy
# o riavvio di Railway (il servizio non ha un Volume). Serve a distinguere
# un valore che emerge in modo ricorrente da uno che cambia a ogni ciclo.
DIP_HISTORY = []
TP_HISTORY = []

# Quante storie tenere al massimo (evita crescita illimitata in memoria).
# Con LOOP_INTERVAL_SECONDS=4h, 200 cicli sono circa 33 giorni di storico.
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "200"))

# Soglie per il giudizio di stabilita'
STABILITY_MIN_SAMPLES = int(os.getenv("STABILITY_MIN_SAMPLES", "5"))
STABILITY_BAND = float(os.getenv("STABILITY_BAND", "1.0"))  # punti percentuali


def stability_report(label, history):
    # Non basta guardare se un valore si ripete IDENTICO: con una griglia
    # continua, 1.5% e 2.0% sono "vicini" anche se non coincidono mai
    # esattamente. Quindi oltre alla moda (valore esatto piu' frequente) si
    # conta anche quante osservazioni cadono entro STABILITY_BAND punti
    # percentuali dalla moda, e si calcola la deviazione standard
    # dell'intero storico come misura di dispersione complessiva.
    n = len(history)

    print(f"\nSTORICO {label} ({n} cicli in memoria, azzerato a ogni redeploy)")

    if n < STABILITY_MIN_SAMPLES:
        print(f"-> Servono almeno {STABILITY_MIN_SAMPLES} cicli per un giudizio di stabilita' (ne servono ancora {STABILITY_MIN_SAMPLES - n}).\n")
        return f"{label}: solo {n}/{STABILITY_MIN_SAMPLES} cicli, ancora presto per giudicare"

    mean = statistics.mean(history)
    stdev = statistics.pstdev(history)

    counts = Counter(history)
    mode_value, mode_count = counts.most_common(1)[0]

    near_mode = sum(1 for v in history if abs(v - mode_value) <= STABILITY_BAND)
    near_pct = near_mode / n * 100

    print(f"Media {mean:.2f}% | dev. standard {stdev:.2f}% | moda {mode_value:.1f}% (esatta in {mode_count}/{n} cicli)")
    print(f"Entro ±{STABILITY_BAND:.1f}% dalla moda: {near_mode}/{n} cicli ({near_pct:.0f}%)")

    if near_pct >= 60:
        verdict = f"STABILE: {mode_value:.1f}% (o un valore entro ±{STABILITY_BAND:.1f}%) e' il risultato ricorrente nel {near_pct:.0f}% dei cicli osservati."
    elif near_pct >= 40:
        verdict = f"PARZIALMENTE STABILE: {mode_value:.1f}% ricorre nel {near_pct:.0f}% dei cicli, ma non e' ancora un pattern netto."
    else:
        verdict = f"INSTABILE: il valore trovato cambia spesso da un ciclo all'altro (dev. standard {stdev:.2f}%). Probabile rumore, non un pattern."

    print(f"-> {verdict}\n", flush=True)

    return f"{label} storico: {verdict}"


def run():
    global DATA_CACHE, INFO_CACHE, META_CACHE

    # Riutilizza la stessa connessione HTTP/client e gli stessi metadata
    # per tutta la vita del processo: spot_meta() viene chiamata una sola
    # volta in assoluto, non a ogni ciclo. L'import resta qui dentro, non
    # in testa al file, cosi' avviene una sola volta (al primo ciclo) e
    # non e' necessario quando la cache e' gia' popolata.
    if INFO_CACHE is None:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        INFO_CACHE = Info(constants.MAINNET_API_URL, skip_ws=True)

    if META_CACHE is None:
        META_CACHE = _with_retry(INFO_CACHE.spot_meta)

    DATA_CACHE = fetch_candles(
        COINS,
        BACKTEST_DAYS,
        existing_data=DATA_CACHE,
        info=INFO_CACHE,
        meta=META_CACHE,
    )

    data = DATA_CACHE

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
            real_capital = get_real_capital(closes, last_prices, INFO_CACHE, META_CACHE)

            if real_capital > 0:
                args.capital = real_capital
            else:
                log(f"CAPITALE REALE nullo | uso BACKTEST_CAPITAL=${args.capital:.2f}")
        except Exception as e:
            log(f"CAPITALE REALE ERRORE | {e} | uso BACKTEST_CAPITAL=${args.capital:.2f}")
    else:
        log(f"HYPERLIQUID_ACCOUNT_ADDRESS non impostata | uso BACKTEST_CAPITAL=${args.capital:.2f}")

    log(f"BACKTEST | coin {','.join(closes)} | {days:.0f} giorni | {len(times)} candele 1h | capitale ${args.capital:.2f}")

    # ========================================================
    # 0) PARAMETRI ATTUALI DEL BOT (baseline di confronto)
    # ========================================================
    current_result = simulate(times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, interval, args)
    current_required = capital_needed(times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, interval, args)

    print(f"\n{'#' * 60}")
    print(f"# PARAMETRI ATTUALI DEL BOT: DIP {CURRENT_DIP_PERCENT:.1f}% | TP {CURRENT_TP_PERCENT:.1f}%")
    print(f"{'#' * 60}")
    total_profit_current = args.capital * current_result["ret"] / 100
    months_in_period = days / 30.44  # mese medio, solo per la stima lineare qui sotto

    print(f"Capitale attuale:    ${args.capital:>10.2f}")
    print(f"Profitto totale sui {days:.0f}gg (~{months_in_period:.1f} mesi), sul capitale reale: ${total_profit_current:+.2f}", end="")
    print(f"  (~${total_profit_current / months_in_period:+.2f}/mese se diviso in parti uguali — NON e' il modo corretto di stimarlo, vedi la mediana calendario qui sotto, che e' piu' affidabile)")
    print(f"BUY saltati (200gg): {current_result['missed_buys']:>10}")
    print(f"Capitale necessario per non saltarne nessuno: ${current_required:.2f}", end="")
    print(f"  (mancano ${max(0.0, current_required - args.capital):.2f})" if current_required > args.capital else "  (sufficiente)")
    print("ATTENZIONE: il capitale necessario qui sopra e' un'altra simulazione (quanto servirebbe per non saltare BUY), NON la base su cui e' calcolato il rendimento sopra, che resta sempre il capitale reale.")
    print(f"{'#' * 60}\n", flush=True)

    print_report(f"PERFORMANCE CON I PARAMETRI ATTUALI (DIP {CURRENT_DIP_PERCENT:.1f}%, TP {CURRENT_TP_PERCENT:.1f}%)", current_result, args, closes, days)

    # ========================================================
    # MESI DI CALENDARIO COPERTI DAI DATI (non finestre arbitrarie)
    # ========================================================
    months = calendar_months_in_range(times)
    current_monthly = monthly_pnls(current_result, months)
    current_median = statistics.median(current_monthly)
    current_mstdev = statistics.pstdev(current_monthly) if len(current_monthly) > 1 else 0.0

    print(f"\nMESI DI CALENDARIO NEL PERIODO: {months[0]} -> {months[-1]} ({len(months)} mesi)")
    print(f"Parametri attuali, sul capitale REALE (${args.capital:.2f}): mediana ${current_median:.2f}/mese (dev.std ${current_mstdev:.2f}), obiettivo ${TARGET_MONTHLY_PROFIT:.0f}/mese")

    # Cosa produrrebbe lo STESSO DIP/TP attuale se il capitale fosse quello
    # necessario a non saltare BUY (non il massimo teorico, lo standard
    # raggiungibile con fondi adeguati a questi parametri).
    current_suff_median, current_suff_stdev, current_suff_result = monthly_median_at_capital(
        times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, interval, args, current_required, months
    )
    print(f"Stessi parametri, con capitale sufficiente (${current_required:.2f}): mediana ${current_suff_median:.2f}/mese (dev.std ${current_suff_stdev:.2f})")

    # Non e' detto che il capitale sufficiente (solo fondi per non saltare
    # BUY) basti anche per l'obiettivo di TARGET_MONTHLY_PROFIT: con gli
    # stessi parametri attuali, cerca per davvero (non linearmente) quale
    # capitale ci vorrebbe per arrivarci, provando capitali crescenti.
    target_capital, target_capital_median, target_reachable = find_capital_for_monthly_target(
        times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, interval, args, TARGET_MONTHLY_PROFIT, months
    )

    if target_reachable:
        print(f"Capitale per arrivare a ${TARGET_MONTHLY_PROFIT:.0f}/mese con questi stessi parametri: ${target_capital:.2f} (mediana attesa ${target_capital_median:.2f}/mese)")
    else:
        print(
            f"ATTENZIONE: con questi parametri la mediana mensile si FERMA a circa ${target_capital_median:.2f}/mese "
            f"anche con molto piu' capitale di quello provato: il vincolo non e' (solo) il capitale, probabilmente "
            "e' MAX_WEEKLY_BUYS o MAX_POSITION_USD. Aumentare il capitale da solo non basterebbe a raggiungere "
            f"${TARGET_MONTHLY_PROFIT:.0f}/mese con questi parametri."
        )

    # ========================================================
    # 1) TROVA IL DIP CHE AVVICINA PIU' LA MEDIANA MENSILE ALL'OBIETTIVO
    #    (TP tenuto fisso a quello attuale)
    # ========================================================
    dip_candidates = build_dip_grid(closes, highs)
    sim_dip = lambda t, c, h, v: simulate(t, c, h, BUY_USD, v, CURRENT_TP_PERCENT, interval, args)

    robust_dip, _, _, _ = find_target_param("DIP", times, closes, highs, dip_candidates, months, sim_dip, TARGET_MONTHLY_PROFIT, None)

    DIP_HISTORY.append(robust_dip)
    del DIP_HISTORY[:-HISTORY_MAX]  # tiene solo gli ultimi HISTORY_MAX cicli
    dip_stability = stability_report("DIP", DIP_HISTORY)

    # ========================================================
    # 2) SUL DIP TROVATO, CERCA IL TP CHE AVVICINA PIU' LA MEDIANA MENSILE
    #    ALL'OBIETTIVO (non sul DIP attuale: i due parametri vengono
    #    incrociati in cascata, non cercati in isolamento)
    # ========================================================
    tp_candidates = build_tp_grid(closes)
    sim_tp = lambda t, c, h, v: simulate(t, c, h, BUY_USD, robust_dip, v, interval, args)

    robust_tp, combined_result, combined_median, combined_mstdev = find_target_param("TP", times, closes, highs, tp_candidates, months, sim_tp, TARGET_MONTHLY_PROFIT, None)

    TP_HISTORY.append(robust_tp)
    del TP_HISTORY[:-HISTORY_MAX]
    tp_stability = stability_report("TP", TP_HISTORY)

    # ========================================================
    # 3) SCENARIO TROVATO vs PARAMETRI ATTUALI, sull'obiettivo mensile
    # ========================================================
    combined_required = capital_needed(times, closes, highs, BUY_USD, robust_dip, robust_tp, interval, args)

    # Stesso ragionamento per i parametri trovati: cosa producono davvero
    # con il capitale che servirebbe a loro (non quello attuale, che e'
    # un vincolo di cassa, non una proprieta' dei parametri).
    combined_suff_median, combined_suff_stdev, combined_suff_result = monthly_median_at_capital(
        times, closes, highs, BUY_USD, robust_dip, robust_tp, interval, args, combined_required, months
    )

    print(f"\n{'=' * 60}")
    print(f"CONFRONTO SULL'OBIETTIVO DI ${TARGET_MONTHLY_PROFIT:.0f}/MESE: DIP {robust_dip:.1f}% + TP {robust_tp:.1f}% (trovati in cascata)")
    print(f"{'=' * 60}")
    print(f"{'':35} {'attuale':>15} {'trovato':>15}")
    print(f"{'DIP / TP':<35} {CURRENT_DIP_PERCENT:>6.1f}/{CURRENT_TP_PERCENT:<6.1f}% {robust_dip:>6.1f}/{robust_tp:<6.1f}%")
    print(f"{'Capitale necessario':<35} ${current_required:>14.2f} ${combined_required:>14.2f}")
    print(f"{'Mediana $/mese (capitale REALE)':<35} ${current_median:>14.2f} ${combined_median:>14.2f}")
    print(f"{'Mediana $/mese (capitale sufficiente)':<35} ${current_suff_median:>14.2f} ${combined_suff_median:>14.2f}")
    print(f"{'Dev.std $/mese (capitale sufficiente)':<35} ${current_suff_stdev:>14.2f} ${combined_suff_stdev:>14.2f}")
    print(f"{'BUY saltati (capitale REALE)':<35} {current_result['missed_buys']:>15} {combined_result['missed_buys']:>15}")
    print(f"{'Max drawdown (capitale REALE)':<35} {current_result['dd']:>14.2f}% {combined_result['dd']:>14.2f}%")
    print(f"{'=' * 60}")

    # Il confronto usa i numeri A CAPITALE SUFFICIENTE per ciascuno scenario,
    # non quelli sul capitale reale attuale: altrimenti si confonderebbe
    # "i parametri sono buoni" con "ho abbastanza soldi oggi", che sono due
    # domande diverse. Con capitale adeguato i BUY saltati tendono a zero
    # per entrambi gli scenari (e' l'effetto voluto, non un errore).
    verdict = monthly_verdict_text(
        TARGET_MONTHLY_PROFIT, current_suff_median, current_suff_stdev, combined_suff_median, combined_suff_stdev,
        current_suff_result, combined_suff_result, current_required, combined_required
    )

    print(f"\nINDICAZIONE SULLA STRATEGIA (a capitale sufficiente per ciascuno scenario):\n{verdict}\n", flush=True)

    # ========================================================
    # RIEPILOGO TELEGRAM
    # ========================================================
    lines = [
        f"\U0001F4CA Backtest {len(months)} mesi | capitale ${args.capital:.2f} | obiettivo ${TARGET_MONTHLY_PROFIT:.0f}/mese",
        f"Attuale: DIP {CURRENT_DIP_PERCENT:.1f}%/TP {CURRENT_TP_PERCENT:.1f}% -> profitto {days:.0f}gg: ${total_profit_current:+.2f} | mediana ${current_median:+.2f}/mese (dev.std ${current_mstdev:.2f})",
        f"Trovato: DIP {robust_dip:.1f}%/TP {robust_tp:.1f}% -> mediana ${combined_median:+.2f}/mese (dev.std ${combined_mstdev:.2f})",
        f"\U0001F449 {verdict}",
        dip_stability,
        tp_stability,
    ]

    send_telegram("\n".join(lines))


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            log(f"ERRORE BACKTEST | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
