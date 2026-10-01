"""
Servizio backtest per Railway. Ogni LOOP_INTERVAL_SECONDS (default 4 ore) fa
due cose distinte:

1) CAPITALE NECESSARIO PER I PARAMETRI ATTUALI DEL BOT: simula il bot con i
   parametri realmente in uso (DIP_PERCENT e TAKE_PROFIT_PERCENT letti
   dall'ambiente, gli stessi del bot vero) sugli ultimi BACKTEST_DAYS giorni,
   e calcola quanto capitale servirebbe per non saltare nessun BUY.

2) ANALISI ESPLORATIVA DEL DIP: SOLO INFORMATIVA, non cambia il bot. Prova
   una griglia di valori di DIP_PERCENT costruita sui ribassi realmente
   osservati nei dati (percentili della distribuzione, non una lista
   fissa), e sceglie quello che vince piu' spesso su finestre piu' corte
   dentro il periodo (non quello col rendimento massimo sul periodo intero,
   che premia facilmente un singolo caso isolato). Un pareggio tra valori
   lontani tra loro (es. l'estremo piu' basso e quello piu' alto) e' un
   segnale di periodi con regimi diversi, non va risolto in silenzio: viene
   segnalato esplicitamente.

Include il calcolo del capitale reale (USDC + coin gestite + coin extra) e
la gestione dei BUY saltati per fondi insufficienti.

Nota sul TP: con BUY_USD basso e SELL_PERCENT=95, il 95% di un lotto vale
meno del minimo d'ordine finche' il prezzo non e' salito di circa
100/SELL_PERCENT*100 - 100 % (~5.3% con SELL_PERCENT=95). Un
TAKE_PROFIT_PERCENT sotto quella soglia non ha alcun effetto: il lotto resta
scartato dal controllo sul minimo d'ordine indipendentemente dal target.
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
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "4.0"))

# L'API restituisce al massimo ~5000 candele 1h, circa 208 giorni
BACKTEST_DAYS = min(int(os.getenv("BACKTEST_DAYS", "200")), 208)

# Analisi esplorativa del DIP per frequenza di vittoria su finestre
# sovrapposte dentro il periodo (solo informativa, non applicata al bot)
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "30"))
STEP_DAYS = int(os.getenv("STEP_DAYS", "10"))
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
    # Saldo reale sul conto: USDC + valore delle coin gestite dal bot, al prezzo
    # di chiusura piu' recente (stesso dato del backtest, nessuna chiamata extra
    # all'orderbook), + valore delle coin extra (CAPITAL_EXTRA_COINS) al prezzo
    # spot corrente, per coin sul conto ma non gestite da questo bot (es. BTC).
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    user_state = info.spot_user_state(ACCOUNT_ADDRESS)

    extra_prices = {}

    if CAPITAL_EXTRA_COINS:
        meta = info.spot_meta()
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

                    book = info.l2_snapshot(market)
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


def build_dip_grid(closes, highs):
    # Griglia adattiva: percentili della distribuzione dei ribassi REALMENTE
    # osservati nei dati (rispetto al massimo mobile a 24h), non una lista
    # fissa. Cosi' la griglia si adatta alla volativa' effettiva delle coin
    # configurate in questo periodo, invece di provare valori arbitrari che
    # potrebbero essere tutti troppo bassi (coin poco volatili) o tutti
    # troppo alti (coin molto volatili).
    drops = []

    for c, cl in closes.items():
        hi = highs[c]

        for i in range(23, len(cl)):
            h24 = max(hi[i - 23:i + 1])

            if h24 > 0:
                d = (h24 - cl[i]) / h24 * 100

                if d > 0:
                    drops.append(d)

    if not drops:
        return [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

    drops.sort()

    n = len(drops)
    # Percentili alti: solo la "coda" della distribuzione rappresenta ribassi
    # degni di nota: la maggior parte del tempo il prezzo e' vicino al suo
    # massimo a 24h, quindi i percentili bassi darebbero soglie vicine a 0.
    percentiles = [40, 52, 64, 74, 83, 90, 95, 98.5][:GRID_SIZE] if GRID_SIZE < 8 else \
        [round(40 + i * (98.5 - 40) / (GRID_SIZE - 1), 1) for i in range(GRID_SIZE)]

    grid = []

    for p in percentiles:
        idx = min(int(n * p / 100), n - 1)
        grid.append(round(drops[idx] * 2) / 2)  # arrotonda a 0.5

    # dedup mantenendo l'ordine, e garantisce almeno 0.5% come minimo
    seen = set()
    out = []

    for v in grid:
        v = max(0.5, v)

        if v not in seen:
            seen.add(v)
            out.append(v)

    return sorted(out)


def find_robust_dip(times, closes, highs, dip_candidates, interval, days, results):
    window_candles = int(WINDOW_DAYS * 24)
    step_candles = max(1, int(STEP_DAYS * 24))

    wins = {d: 0 for d in dip_candidates}
    windows_tested = 0

    start = 23

    while start + window_candles <= len(times):
        end = start + window_candles

        w_times = times[start:end]
        w_closes = {c: v[start:end] for c, v in closes.items()}
        w_highs = {c: v[start:end] for c, v in highs.items()}

        best_dip, best_ret = None, None

        for dip in dip_candidates:
            r = simulate(w_times, w_closes, w_highs, BUY_USD, dip, TAKE_PROFIT_PERCENT, interval, args)

            if best_ret is None or r["ret"] > best_ret:
                best_dip, best_ret = dip, r["ret"]

        wins[best_dip] += 1
        windows_tested += 1
        start += step_candles

    print(f"\nFREQUENZA VITTORIE SU {windows_tested} FINESTRE DA {WINDOW_DAYS}gg (passo {STEP_DAYS}gg)")
    print("-" * 40)

    for d in dip_candidates:
        pct = wins[d] / windows_tested * 100 if windows_tested else 0
        bar = "#" * round(pct / 5)
        print(f"DIP {d:>5.1f}% | {wins[d]:>2} vittorie ({pct:>4.0f}%) {bar}")

    if windows_tested == 0:
        log(f"RICERCA DIP | periodo troppo corto per finestre da {WINDOW_DAYS}gg")
        return dip_candidates[0], wins

    max_wins = max(wins.values())
    tied = [d for d in dip_candidates if wins[d] == max_wins]
    tied_pct = max_wins / windows_tested * 100

    if len(tied) == 1:
        robust_dip = tied[0]
        print(f"\n-> DIP PIU' FREQUENTE: {robust_dip:.1f}% (vince nel {tied_pct:.0f}% delle finestre)\n")
    else:
        by_ret = {r["dip"]: r["ret"] for r in results}
        robust_dip = max(tied, key=lambda d: by_ret.get(d, float("-inf")))
        tied_str = ", ".join(f"{d:.1f}%" for d in tied)

        print(f"\n-> PAREGGIO nel {tied_pct:.0f}% delle finestre tra: {tied_str}")
        print("   Nessun DIP e' chiaramente piu' frequente: questo periodo ha probabilmente regimi diversi.")
        print(f"   Scelto {robust_dip:.1f}% come criterio secondario (rendimento piu' alto sul periodo intero tra i pareggiati).\n")

    return robust_dip, wins


def run():
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
            else:
                log(f"CAPITALE REALE nullo | uso BACKTEST_CAPITAL=${args.capital:.2f}")
        except Exception as e:
            log(f"CAPITALE REALE ERRORE | {e} | uso BACKTEST_CAPITAL=${args.capital:.2f}")
    else:
        log(f"HYPERLIQUID_ACCOUNT_ADDRESS non impostata | uso BACKTEST_CAPITAL=${args.capital:.2f}")

    log(f"BACKTEST | coin {','.join(closes)} | {days:.0f} giorni | {len(times)} candele 1h | capitale ${args.capital:.2f}")

    # ========================================================
    # 1) CAPITALE NECESSARIO PER I PARAMETRI ATTUALI DEL BOT
    # ========================================================
    current_result = simulate(times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, TAKE_PROFIT_PERCENT, interval, args)
    current_required = capital_needed(times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT, TAKE_PROFIT_PERCENT, interval, args)

    print(f"\n{'#' * 60}")
    print(f"# PARAMETRI ATTUALI DEL BOT: DIP {CURRENT_DIP_PERCENT:.1f}% | TP {TAKE_PROFIT_PERCENT:.1f}%")
    print(f"{'#' * 60}")
    print(f"Capitale attuale:    ${args.capital:>10.2f}")
    print(f"BUY saltati (200gg): {current_result['missed_buys']:>10}")
    print(f"Capitale necessario per non saltarne nessuno: ${current_required:.2f}", end="")
    print(f"  (mancano ${max(0.0, current_required - args.capital):.2f})" if current_required > args.capital else "  (sufficiente)")
    print(f"{'#' * 60}\n", flush=True)

    print_report(f"PERFORMANCE CON I PARAMETRI ATTUALI (DIP {CURRENT_DIP_PERCENT:.1f}%, TP {TAKE_PROFIT_PERCENT:.1f}%)", current_result, args, closes, days)

    # ========================================================
    # 2) ANALISI ESPLORATIVA DEL DIP (solo informativa)
    # ========================================================
    dip_candidates = build_dip_grid(closes, highs)

    print(f"\nANALISI ESPLORATIVA DEL DIP (non applicata al bot) | griglia adattata ai dati: {', '.join(f'{d:.1f}%' for d in dip_candidates)}")

    results = [simulate(times, closes, highs, BUY_USD, dip, TAKE_PROFIT_PERCENT, interval, args) for dip in dip_candidates]

    robust_dip, _wins = find_robust_dip(times, closes, highs, dip_candidates, interval, days, results)

    if robust_dip != CURRENT_DIP_PERCENT:
        robust_result = next(r for r in results if r["dip"] == robust_dip)
        robust_required = capital_needed(times, closes, highs, BUY_USD, robust_dip, TAKE_PROFIT_PERCENT, interval, args)

        print(f"{'-' * 60}")
        print(f"CONFRONTO: parametri attuali (DIP {CURRENT_DIP_PERCENT:.1f}%) vs DIP piu' frequente (DIP {robust_dip:.1f}%)")
        print(f"{'-' * 60}")
        print(f"{'':25} {'attuale':>15} {'piu'' frequente':>18}")
        print(f"{'DIP':<25} {CURRENT_DIP_PERCENT:>14.1f}% {robust_dip:>17.1f}%")
        print(f"{'Rendimento 200gg':<25} {current_result['ret']:>14.2f}% {robust_result['ret']:>17.2f}%")
        print(f"{'BUY saltati':<25} {current_result['missed_buys']:>15} {robust_result['missed_buys']:>18}")
        print(f"{'Capitale necessario':<25} ${current_required:>14.2f} ${robust_required:>17.2f}")
        print(f"{'-' * 60}\n", flush=True)
    else:
        log(f"Il DIP attuale ({CURRENT_DIP_PERCENT:.1f}%) coincide con quello piu' frequente trovato nell'analisi esplorativa.")


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            log(f"ERRORE BACKTEST | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
