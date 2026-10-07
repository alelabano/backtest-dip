"""
Servizio backtest per Railway. Ogni LOOP_INTERVAL_SECONDS (default 4 ore) fa
quattro cose:

1) ANALISI DEL MESE CORRENTE (ultimi ANALYSIS_WINDOW_DAYS giorni, default 30,
   su candele a 4h — la stessa granularita' del ciclo del bot, non 1h):
   calcola la variazione media di prezzo delle coin gestite e classifica il
   periodo come RIALZISTA o RIBASSISTA (soglia REGIME_THRESHOLD_PCT, default
   0: puro segno della variazione).

2) RICERCA DEL DIP E DEL TP "FISIOLOGICI" DI QUESTO MESE: dentro i 30 giorni,
   usa sotto-finestre piu' corte (SUBWINDOW_DAYS, default 7, passo
   SUBWINDOW_STEP_DAYS, default 2) per trovare quale DIP (TP fisso a quello
   attuale) e quale TP (sul DIP trovato) vincono piu' spesso — lo stesso
   principio di "frequenza su finestre" usato in precedenza, ora scalato
   dentro il singolo mese anziche' sull'intero storico. La griglia di
   candidati e' adattiva (percentili dei movimenti REALI osservati in
   questi 30 giorni), non una lista fissa.

3) STORICO PER REGIME: il DIP/TP trovato in questo ciclo viene salvato in
   due storici SEPARATI (uno per i mesi rialzisti, uno per i mesi
   ribassisti), non in un unico storico generico. Nel tempo, ciascun
   regime accumula la propria distribuzione di valori "fisiologici"
   osservati. Vive solo in memoria per la vita del processo (si azzera a
   ogni redeploy: il servizio non ha un Volume).

4) SUGGERIMENTO CONDIZIONATO AL REGIME: guarda il regime di QUESTO mese e
   consiglia il DIP/TP piu' frequente nello storico DI QUEL REGIME (non un
   valore unico assoluto per tutti i regimi), con una soglia minima di
   osservazioni prima di suggerire qualcosa.

RIEPILOGO SU TELEGRAM: se TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID sono
impostate, invia un messaggio breve. Il log completo resta nei log di
Railway.

NOTA IMPORTANTE SU COSA NON C'E' PIU': le versioni precedenti di questo
servizio calcolavano anche un "capitale necessario per raggiungere $X/mese"
(TARGET_MONTHLY_PROFIT) con una ricerca per raddoppi+bisezione sul
capitale. Quella logica e' stata rimossa in questa versione, perche' non e'
piu' coerente con l'approccio per regime: qui l'obiettivo non e' un importo
mensile fisso, ma trovare la soglia "fisiologica" del mercato corrente. Se
serve ancora anche l'altra analisi, va reintegrata come sezione separata.

Nota sul TP: con BUY_USD basso e SELL_PERCENT=95, il 95% di un lotto vale
meno del minimo d'ordine finche' il prezzo non e' salito di circa
100/SELL_PERCENT*100 - 100 % (~5.3% con SELL_PERCENT=95). Un
TAKE_PROFIT_PERCENT sotto quella soglia non ha alcun effetto. La griglia del
TP parte sempre da sopra questa soglia.
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
# CANDELE: granularita' a 4 ore, stessa cadenza del ciclo del bot
# ============================================================

CANDLE_HOURS = 4
CANDLE_INTERVAL_STR = "4h"
CANDLE_MS = CANDLE_HOURS * HOUR_MS

# Quante candele compongono una finestra di 24h a questa granularita'
# (6 candele da 4h = 24h). Sostituisce il vecchio "23" pensato per candele
# da 1h (24 candele da 1h = 24h).
DAY_LOOKBACK = 24 // CANDLE_HOURS


# ============================================================
# DATI
# ============================================================

def fetch_candles(coins, days, existing_data=None, info=None, meta=None):
    """
    Primo avvio: scarica l'intero periodo.
    Cicli successivi: aggiorna solo le ultime UPDATE_HOURS ore (merge per
    timestamp, sostituendo anche la candela ancora in formazione).
    """
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    if info is None:
        info = Info(constants.MAINNET_API_URL, skip_ws=True)

    if meta is None:
        meta = _with_retry(info.spot_meta)

    usdc = next(i for i, t in enumerate(meta["tokens"]) if t["name"] == "USDC")

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    full_start_ms = end_ms - days * 24 * HOUR_MS

    UPDATE_HOURS = max(12, CANDLE_HOURS * 3)  # un po' di margine sopra una candela
    incremental_start_ms = end_ms - UPDATE_HOURS * HOUR_MS

    data = {}
    if existing_data:
        data = {c: list(existing_data[c]) for c in coins if c in existing_data}

    for coin in coins:
        market = None

        for idx, token in enumerate(meta["tokens"]):
            if token["name"] in (coin, "U" + coin):
                market = next((m["name"] for m in meta["universe"] if m["tokens"] == [idx, usdc]), None)
                if market:
                    break

        if not market:
            log(f"ATTENZIONE: nessun mercato spot {coin}/USDC: coin ignorata")
            continue

        start_ms = incremental_start_ms if (coin in data and data[coin]) else full_start_ms

        candles = _with_retry(info.candles_snapshot, market, CANDLE_INTERVAL_STR, start_ms, end_ms)

        new_candles = [{"t": c["t"], "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])} for c in candles]

        if coin not in data:
            data[coin] = new_candles
            continue

        merged = {c["t"]: c for c in data[coin]}

        for candle in new_candles:
            merged[candle["t"]] = candle

        cutoff_ms = end_ms - days * 24 * HOUR_MS
        data[coin] = [c for c in sorted(merged.values(), key=lambda x: x["t"]) if c["t"] >= cutoff_ms]

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
# SIMULAZIONE (motore generico, usato per cercare DIP/TP nella finestra)
# ============================================================

def simulate(times, closes, highs, buy_usd, dip, tp, a):
    coins = list(closes)

    usdc = a.capital
    lots = []
    weekly = {}

    realized = 0.0
    buys = 0
    sells = 0

    for i in range(DAY_LOOKBACK - 1, len(times)):
        px = {c: closes[c][i] for c in coins}

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
            realized += proceeds - qty * lot["cost"]
            lot["qty"] -= qty
            sells += 1
            continue

        # ---- BUY: coin col ribasso piu' forte ----
        week = datetime.fromtimestamp(times[i] / 1000, timezone.utc).strftime("%G-W%V")

        drops = []

        for c in coins:
            open_lots = [l for l in lots if l["coin"] == c]

            if open_lots:
                reference = min(l["buy_price"] for l in open_lots)
            else:
                reference = max(highs[c][i - (DAY_LOOKBACK - 1):i + 1])

            if reference > 0:
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
            buys += 1
            break

    last = {c: closes[c][-1] for c in coins}
    open_value = sum(l["qty"] * last[l["coin"]] for l in lots)
    final = usdc + open_value

    return {
        "dip": dip, "tp": tp,
        "ret": (final - a.capital) / a.capital * 100 if a.capital else 0.0,
        "realized": realized,
        "buys": buys, "sells": sells,
    }


# ============================================================
# GRIGLIE ADATTIVE (percentili dei movimenti osservati nella finestra data)
# ============================================================

def _percentile_grid(values, floor, grid_size):
    if not values:
        return [max(floor, x) for x in [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]][:grid_size]

    values = sorted(values)
    n = len(values)
    percentiles = [round(40 + i * (98.5 - 40) / (grid_size - 1), 1) for i in range(grid_size)]

    grid = []
    for p in percentiles:
        idx = min(int(n * p / 100), n - 1)
        grid.append(round(values[idx] * 2) / 2)

    seen, out = set(), []
    for v in grid:
        v = max(floor, v)
        if v not in seen:
            seen.add(v)
            out.append(v)

    return sorted(out)


def build_dip_grid(closes, highs, grid_size):
    drops = []

    for c, cl in closes.items():
        hi = highs[c]

        for i in range(DAY_LOOKBACK - 1, len(cl)):
            h = max(hi[i - (DAY_LOOKBACK - 1):i + 1])

            if h > 0:
                d = (h - cl[i]) / h * 100

                if d > 0:
                    drops.append(d)

    return _percentile_grid(drops, 0.5, grid_size)


def build_tp_grid(closes, sell_percent, grid_size):
    rises = []

    for c, cl in closes.items():
        for i in range(DAY_LOOKBACK - 1, len(cl)):
            lo = min(cl[i - (DAY_LOOKBACK - 1):i + 1])

            if lo > 0:
                r = (cl[i] - lo) / lo * 100

                if r > 0:
                    rises.append(r)

    effective_floor = (100 / sell_percent * 100 - 100) if sell_percent < 100 else 0.0
    floor = max(0.5, round((effective_floor + 0.3) * 2) / 2)

    return _percentile_grid(rises, floor, grid_size)


# ============================================================
# CLASSIFICAZIONE DEL REGIME (rialzista / ribassista)
# ============================================================

def classify_regime(closes, threshold_pct):
    # Variazione media di prezzo tra inizio e fine finestra, sulle coin
    # gestite. threshold_pct=0 (default) -> puro segno: qualunque
    # variazione positiva e' "rialzista", qualunque negativa e' "ribassista".
    changes = []

    for c, cl in closes.items():
        if len(cl) >= 2 and cl[0] > 0:
            changes.append((cl[-1] / cl[0] - 1) * 100)

    avg_change = statistics.mean(changes) if changes else 0.0
    label = "rialzista" if avg_change >= threshold_pct else "ribassista"

    return label, avg_change, dict(zip(closes.keys(), changes))


# ============================================================
# RICERCA DEL DIP/TP FISIOLOGICO DI QUESTA FINESTRA
# (frequenza di vittoria su sotto-finestre piu' corte, dentro i 30gg)
# ============================================================

def find_window_param(label, times, closes, highs, candidates, sub_days, step_days, sim_fn):
    sub_candles = int(sub_days * 24 / CANDLE_HOURS)
    step_candles = max(1, int(step_days * 24 / CANDLE_HOURS))

    wins = {v: 0 for v in candidates}
    tested = 0

    start = DAY_LOOKBACK - 1

    while start + sub_candles <= len(times):
        end = start + sub_candles
        w_times = times[start:end]
        w_closes = {c: v[start:end] for c, v in closes.items()}
        w_highs = {c: v[start:end] for c, v in highs.items()}

        best_v, best_ret = None, None

        for v in candidates:
            r = sim_fn(w_times, w_closes, w_highs, v)

            if best_ret is None or r["ret"] > best_ret:
                best_v, best_ret = v, r["ret"]

        wins[best_v] += 1
        tested += 1
        start += step_candles

    if tested == 0:
        return candidates[0], wins, 0, 0.0

    max_wins = max(wins.values())
    tied = [v for v in candidates if wins[v] == max_wins]
    best_v = tied[0] if len(tied) == 1 else sorted(tied)[len(tied) // 2]  # tra pareggiati, il valore mediano

    return best_v, wins, tested, max_wins / tested * 100


# ============================================================
# SERVIZIO
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

# Finestra di analisi: l'ultimo "mese" (default 30gg), non piu' l'intero
# storico. Si scarica un piccolo margine extra per il lookback a 24h.
ANALYSIS_WINDOW_DAYS = int(os.getenv("ANALYSIS_WINDOW_DAYS", "30"))
FETCH_BUFFER_DAYS = 3
BACKTEST_DAYS = ANALYSIS_WINDOW_DAYS + FETCH_BUFFER_DAYS

# Sotto-finestre dentro il mese, per trovare il DIP/TP "fisiologico"
SUBWINDOW_DAYS = int(os.getenv("SUBWINDOW_DAYS", "7"))
SUBWINDOW_STEP_DAYS = int(os.getenv("SUBWINDOW_STEP_DAYS", "2"))
GRID_SIZE = int(os.getenv("GRID_SIZE", "8"))

# Soglia per la classificazione del regime (punti percentuali). 0 = puro
# segno della variazione media (rialzista se >=0, ribassista se <0).
REGIME_THRESHOLD_PCT = float(os.getenv("REGIME_THRESHOLD_PCT", "0.0"))

# Storico per regime: quante osservazioni tenere al massimo, e soglia
# minima prima di dare un giudizio di stabilita'.
HISTORY_MAX_PER_REGIME = int(os.getenv("HISTORY_MAX_PER_REGIME", "100"))
STABILITY_MIN_SAMPLES = int(os.getenv("STABILITY_MIN_SAMPLES", "5"))
STABILITY_BAND = float(os.getenv("STABILITY_BAND", "1.0"))

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
            for idx, token in enumerate(meta["tokens"]):
                if token["name"] in (coin, "U" + coin):
                    market = next((m["name"] for m in meta["universe"] if m["tokens"] == [idx, usdc_idx]), None)

                    if market:
                        book = _with_retry(info.l2_snapshot, market)
                        levels = book.get("levels", [])

                        if len(levels) == 2 and levels[0] and levels[1]:
                            extra_prices[coin] = (float(levels[0][0]["px"]) + float(levels[1][0]["px"])) / 2
                    break

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


def regime_consistency(label, values):
    # Stesso principio della stabilita' storica usata in precedenza, ma
    # applicato separatamente alla storia DI UN SOLO REGIME: moda, quante
    # osservazioni cadono entro STABILITY_BAND dalla moda, dev. standard.
    n = len(values)

    if n < STABILITY_MIN_SAMPLES:
        return None, None, None, n

    mode_value, _ = Counter(values).most_common(1)[0]
    near = sum(1 for v in values if abs(v - mode_value) <= STABILITY_BAND)
    near_pct = near / n * 100
    stdev = statistics.pstdev(values)

    return mode_value, near_pct, stdev, n


# Storico in memoria, separato per regime. Si azzera a ogni redeploy
# (nessun Volume): vive solo per la durata del processo.
REGIME_HISTORY = {
    "rialzista": {"dip": [], "tp": []},
    "ribassista": {"dip": [], "tp": []},
}

DATA_CACHE = None
INFO_CACHE = None
META_CACHE = None


def run():
    global DATA_CACHE, INFO_CACHE, META_CACHE

    if INFO_CACHE is None:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        INFO_CACHE = Info(constants.MAINNET_API_URL, skip_ws=True)

    if META_CACHE is None:
        META_CACHE = _with_retry(INFO_CACHE.spot_meta)

    DATA_CACHE = fetch_candles(COINS, BACKTEST_DAYS, existing_data=DATA_CACHE, info=INFO_CACHE, meta=META_CACHE)

    if not DATA_CACHE:
        raise RuntimeError("Nessun dato scaricato")

    times, closes, highs = align(DATA_CACHE)

    min_candles = DAY_LOOKBACK + int(ANALYSIS_WINDOW_DAYS * 24 / CANDLE_HOURS)

    if len(times) < min_candles:
        raise RuntimeError(f"Dati insufficienti: {len(times)} candele, servono almeno {min_candles}")

    # Finestra di analisi: solo l'ultimo ANALYSIS_WINDOW_DAYS (+ il margine
    # di lookback necessario per il massimo/minimo a 24h all'inizio della
    # finestra), non l'intero storico scaricato.
    window_candles = int(ANALYSIS_WINDOW_DAYS * 24 / CANDLE_HOURS) + (DAY_LOOKBACK - 1)
    w_times = times[-window_candles:]
    w_closes = {c: v[-window_candles:] for c, v in closes.items()}
    w_highs = {c: v[-window_candles:] for c, v in highs.items()}

    window_days_actual = (w_times[-1] - w_times[0]) / HOUR_MS / 24

    if ACCOUNT_ADDRESS:
        try:
            last_prices = {c: w_closes[c][-1] for c in w_closes}
            real_capital = get_real_capital(list(w_closes.keys()), last_prices, INFO_CACHE, META_CACHE)

            if real_capital > 0:
                args.capital = real_capital
        except Exception as e:
            log(f"CAPITALE REALE ERRORE | {e} | uso BACKTEST_CAPITAL=${args.capital:.2f}")
    else:
        log(f"HYPERLIQUID_ACCOUNT_ADDRESS non impostata | uso BACKTEST_CAPITAL=${args.capital:.2f}")

    log(f"ANALISI MESE | coin {','.join(w_closes)} | {window_days_actual:.0f}gg su candele {CANDLE_INTERVAL_STR} | capitale ${args.capital:.2f}")

    # ========================================================
    # 1) CLASSIFICAZIONE DEL REGIME
    # ========================================================
    regime, avg_change, per_coin_change = classify_regime(w_closes, REGIME_THRESHOLD_PCT)

    print(f"\n{'#' * 60}")
    print(f"# MESE {regime.upper()} | variazione media coin gestite: {avg_change:+.1f}%")
    print(f"{'#' * 60}")

    for c, chg in per_coin_change.items():
        print(f"  {c:<6} {chg:+.1f}%")

    # ========================================================
    # 2) PERFORMANCE DEI PARAMETRI ATTUALI IN QUESTO MESE
    # ========================================================
    current_result = simulate(w_times, w_closes, w_highs, BUY_USD, CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, args)

    print(f"\nParametri attuali (DIP {CURRENT_DIP_PERCENT:.1f}%/TP {CURRENT_TP_PERCENT:.1f}%) in questo mese {regime}:")
    print(f"  {current_result['buys']} buy, {current_result['sells']} sell, realizzato ${current_result['realized']:+.2f}, rendimento {current_result['ret']:+.2f}%")

    # ========================================================
    # 3) DIP E TP "FISIOLOGICI" DI QUESTO MESE (frequenza su sotto-finestre)
    # ========================================================
    dip_grid = build_dip_grid(w_closes, w_highs, GRID_SIZE)
    sim_dip = lambda t, c, h, v: simulate(t, c, h, BUY_USD, v, CURRENT_TP_PERCENT, args)

    window_dip, dip_wins, dip_tested, dip_win_pct = find_window_param(
        "DIP", w_times, w_closes, w_highs, dip_grid, SUBWINDOW_DAYS, SUBWINDOW_STEP_DAYS, sim_dip
    )

    tp_grid = build_tp_grid(w_closes, SELL_PERCENT, GRID_SIZE)
    sim_tp = lambda t, c, h, v: simulate(t, c, h, BUY_USD, window_dip, v, args)

    window_tp, tp_wins, tp_tested, tp_win_pct = find_window_param(
        "TP", w_times, w_closes, w_highs, tp_grid, SUBWINDOW_DAYS, SUBWINDOW_STEP_DAYS, sim_tp
    )

    print(f"\nDIP fisiologico di questo mese: {window_dip:.1f}% (vince {dip_win_pct:.0f}% di {dip_tested} sotto-finestre da {SUBWINDOW_DAYS}gg)")
    print(f"TP fisiologico di questo mese:  {window_tp:.1f}% (vince {tp_win_pct:.0f}% di {tp_tested} sotto-finestre da {SUBWINDOW_DAYS}gg)")

    # ========================================================
    # 4) STORICO PER REGIME + SUGGERIMENTO CONDIZIONATO
    # ========================================================
    REGIME_HISTORY[regime]["dip"].append(window_dip)
    REGIME_HISTORY[regime]["tp"].append(window_tp)
    REGIME_HISTORY[regime]["dip"] = REGIME_HISTORY[regime]["dip"][-HISTORY_MAX_PER_REGIME:]
    REGIME_HISTORY[regime]["tp"] = REGIME_HISTORY[regime]["tp"][-HISTORY_MAX_PER_REGIME:]

    dip_mode, dip_pct, dip_stdev, dip_n = regime_consistency(regime, REGIME_HISTORY[regime]["dip"])
    tp_mode, tp_pct, tp_stdev, tp_n = regime_consistency(regime, REGIME_HISTORY[regime]["tp"])

    print(f"\nSTORICO MESI {regime.upper()}: {dip_n} osservazioni in memoria (azzerato a ogni redeploy)")

    if dip_mode is None:
        suggestion = f"Storico mesi {regime} ancora insufficiente ({dip_n}/{STABILITY_MIN_SAMPLES}): nessun suggerimento per ora."
    else:
        print(f"  DIP tipico nei mesi {regime}: {dip_mode:.1f}% (entro ±{STABILITY_BAND:.1f}% nel {dip_pct:.0f}% dei mesi osservati, dev.std {dip_stdev:.2f})")
        print(f"  TP tipico nei mesi {regime}:  {tp_mode:.1f}% (entro ±{STABILITY_BAND:.1f}% nel {tp_pct:.0f}% dei mesi osservati, dev.std {tp_stdev:.2f})")

        if dip_pct < 60 or tp_pct < 60:
            suggestion = (
                f"Nei mesi {regime} il DIP/TP fisiologico varia parecchio ({dip_pct:.0f}%/{tp_pct:.0f}% di consistenza): "
                "nessun valore e' ancora abbastanza ricorrente da consigliare un cambio."
            )
        elif dip_mode == CURRENT_DIP_PERCENT and tp_mode == CURRENT_TP_PERCENT:
            suggestion = f"I parametri attuali coincidono gia' con lo standard tipico dei mesi {regime}: nessuna modifica suggerita."
        else:
            suggestion = (
                f"Nei mesi {regime} (come questo) DIP {dip_mode:.1f}%/TP {tp_mode:.1f}% e' il valore tipico, ricorrente nel "
                f"{min(dip_pct, tp_pct):.0f}% dei {dip_n} mesi {regime} osservati finora. Attuali: {CURRENT_DIP_PERCENT:.1f}%/{CURRENT_TP_PERCENT:.1f}%."
            )

    print(f"\nSUGGERIMENTO: {suggestion}\n", flush=True)

    # ========================================================
    # RIEPILOGO TELEGRAM
    # ========================================================
    lines = [
        f"\U0001F4CA Mese {regime} ({avg_change:+.1f}%) | {window_days_actual:.0f}gg su 4h | capitale ${args.capital:.2f}",
        f"Attuale: DIP {CURRENT_DIP_PERCENT:.1f}%/TP {CURRENT_TP_PERCENT:.1f}% -> {current_result['buys']} buy, realizzato ${current_result['realized']:+.2f}",
        f"Fisiologico questo mese: DIP {window_dip:.1f}% ({dip_win_pct:.0f}%) / TP {window_tp:.1f}% ({tp_win_pct:.0f}%)",
        f"\U0001F449 {suggestion}",
    ]

    send_telegram("\n".join(lines))


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            log(f"ERRORE BACKTEST | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
