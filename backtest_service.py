"""
Servizio backtest per Railway con ricerca del DIP % per FREQUENZA, non per
massimo rendimento sull'intero periodo.

Il massimo rendimento su un unico periodo di 200 giorni puo' premiare un
singolo caso isolato (un DIP che ha funzionato bene una volta per caso).
Per questo i 200 giorni vengono divisi in finestre piu' corte e sovrapposte
(WINDOW_DAYS, passo STEP_DAYS): per ogni finestra si trova il DIP col
rendimento migliore, e si conta quante volte ciascun DIP vince. Il DIP
scelto per il report e' quello che vince piu' spesso, non quello col
rendimento piu' alto sul periodo intero (che resta mostrato solo come
informazione, non come criterio di scelta).

TP resta fisso (default 6%, vedi nota sotto) e non entra nella ricerca.
Include il calcolo del capitale reale (USDC + coin gestite + coin extra) e
la gestione dei BUY saltati per fondi insufficienti.

Nota sul TP fisso al 6%: con BUY_USD basso e SELL_PERCENT=95, il 95% di un
lotto vale meno del minimo d'ordine finche' il prezzo non e' salito di
circa il 100/SELL_PERCENT*100 - 100 % (~5.3% con SELL_PERCENT=95, BUY_USD
vicino a MIN_ORDER_USD). Un TAKE_PROFIT_PERCENT sotto quella soglia non ha
alcun effetto, perche' il lotto resta scartato dal controllo sul minimo
d'ordine indipendentemente dal target. 6% e' sopra quella soglia.
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


def print_report(r, a, closes, days):
    print("=" * 60)
    print(f"PERFORMANCE DEL BOT NEGLI ULTIMI {days:.0f} GIORNI")
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


# ============================================================
# SERVIZIO E RICERCA DIP ROBUSTA
# ============================================================

load_dotenv()

sys.stdout.reconfigure(line_buffering=True)

COINS = [c.strip().upper() for c in os.getenv("COINS", "HYPE,ZEC,ETH,SOL").split(",") if c.strip()]
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "14400"))
BUY_USD = float(os.getenv("BUY_USD", "10"))

# Target Take Profit fisso (vedi nota sul minimo d'ordine in testa al file)
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "6.0"))

# L'API restituisce al massimo ~5000 candele 1h, circa 208 giorni
BACKTEST_DAYS = min(int(os.getenv("BACKTEST_DAYS", "200")), 208)

# Ricerca del DIP per frequenza di vittoria su finestre sovrapposte, non per
# rendimento massimo sull'intero periodo (che puo' premiare un caso isolato).
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "30"))
STEP_DAYS = int(os.getenv("STEP_DAYS", "10"))

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
        log(f"CAPITALE REALE | coin extra configurate: {CAPITAL_EXTRA_COINS}")

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

    log(f"CAPITALE REALE dettaglio | USDC ${usdc_balance:.2f} | coin gestite ${coins_value:.2f} | coin extra ${extra_value:.2f}")

    return usdc_balance + coins_value + extra_value


def find_robust_dip(times, closes, highs, dip_candidates, interval, days, results):
    # Finestre di WINDOW_DAYS giorni, che scorrono di STEP_DAYS, dentro il
    # periodo scaricato. Ogni finestra riparte dallo stesso capitale (sono
    # periodi indipendenti tra loro, non un'unica simulazione incatenata):
    # l'obiettivo e' vedere quale DIP vince piu' spesso, non accumulare PnL.
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

    log(f"RICERCA DIP ROBUSTA | {windows_tested} finestre da {WINDOW_DAYS}gg (passo {STEP_DAYS}gg) dentro gli ultimi {days:.0f}gg")

    print("\n" + "=" * 60)
    print(f"FREQUENZA VITTORIE PER DIP % (su {windows_tested} finestre da {WINDOW_DAYS}gg)")
    print("=" * 60)
    print(f"{'DIP %':<7} {'Vittorie':>9} {'% finestre':>12}")

    for d in dip_candidates:
        pct = wins[d] / windows_tested * 100 if windows_tested else 0
        print(f"{d:<7.1f}% {wins[d]:>9} {pct:>11.1f}%")

    print("=" * 60)

    if windows_tested == 0:
        log(f"RICERCA DIP ROBUSTA | periodo troppo corto per finestre da {WINDOW_DAYS}gg, uso il primo DIP della lista")
        return dip_candidates[0], wins

    max_wins = max(wins.values())
    tied = [d for d in dip_candidates if wins[d] == max_wins]
    tied_pct = max_wins / windows_tested * 100

    if len(tied) == 1:
        robust_dip = tied[0]
        print(f"-> DIP PIU' FREQUENTE: {robust_dip}% (vince nel {tied_pct:.0f}% delle finestre)")
    else:
        # Pareggio: nessun DIP e' davvero piu' frequente degli altri. Lo
        # segnaliamo invece di sceglierne uno in modo silenzioso. Come
        # criterio SECONDARIO (debole) si usa il rendimento sull'intero
        # periodo tra i soli valori in pareggio, ma il pareggio stesso e'
        # un segnale che il periodo analizzato non ha un DIP chiaramente
        # migliore: valori molto diversi tra loro (es. l'estremo piu'
        # basso e l'estremo piu' alto della griglia) possono indicare
        # regimi di mercato diversi dentro lo stesso periodo, non rumore.
        by_ret = {r["dip"]: r["ret"] for r in results}
        robust_dip = max(tied, key=lambda d: by_ret.get(d, float("-inf")))

        tied_str = ", ".join(f"{d:.1f}%" for d in tied)
        print(f"-> PAREGGIO tra {len(tied)} valori (tutti nel {tied_pct:.0f}% delle finestre): {tied_str}")
        print(f"   Nessun DIP e' chiaramente piu' frequente degli altri in questo periodo.")
        print(f"   Scelto {robust_dip:.1f}% come criterio secondario (rendimento piu' alto sull'intero periodo tra i valori in pareggio).")

    print("=" * 60 + "\n", flush=True)

    return robust_dip, wins


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
                log(f"CAPITALE REALE | ${args.capital:.2f} (USDC + coin gestite + extra usati come liquidita' iniziale)")
            else:
                log(f"CAPITALE REALE nullo | uso BACKTEST_CAPITAL=${args.capital:.2f}")
        except Exception as e:
            log(f"CAPITALE REALE ERRORE | {e} | uso BACKTEST_CAPITAL=${args.capital:.2f}")
    else:
        log(f"HYPERLIQUID_ACCOUNT_ADDRESS non impostata | uso BACKTEST_CAPITAL=${args.capital:.2f}")

    log(f"AVVIO RICERCA DIP (TP FISSO = {TAKE_PROFIT_PERCENT}%) su {days:.0f} giorni")

    dip_candidates = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0]

    # Rendimento sull'intero periodo: utile da vedere, ma NON e' il criterio
    # di scelta (premia facilmente un singolo caso isolato, non un pattern
    # che si ripete).
    results = [simulate(times, closes, highs, BUY_USD, dip, TAKE_PROFIT_PERCENT, interval, args) for dip in dip_candidates]

    print("\n" + "=" * 90)
    print(f"RENDIMENTO SULL'INTERO PERIODO PER DIP % (solo informativo, TP fisso {TAKE_PROFIT_PERCENT}%)")
    print("=" * 90)
    print(f"{'DIP %':<7} {'Rendimento':>12} {'Valore Fin.':>12} {'Realizzato':>12} {'Buys/Sells':>12} {'Saltati':>8} {'Open':>6} {'Max DD':>9}")
    print("-" * 90)

    for r in results:
        print(f"{r['dip']:<7.1f}% {r['ret']:>11.2f}% ${r['final']:>11.2f} ${r['realized']:>11.2f} {r['buys']:>5}/{r['sells']:<5} {r['missed_buys']:>8} {r['open']:>6} {r['dd']:>8.1f}%")

    print("=" * 90 + "\n", flush=True)

    # Scelta del DIP: quello che vince piu' spesso su finestre piu' corte
    # dentro il periodo, non quello col rendimento massimo sul periodo intero.
    robust_dip, _wins = find_robust_dip(times, closes, highs, dip_candidates, interval, days, results)

    best_res = next(r for r in results if r["dip"] == robust_dip)

    print_report(best_res, args, closes, days)

    # Se la scelta robusta ha saltato dei BUY per mancanza di liquidita', simula con capitale sufficiente
    if best_res["missed_buys"] > 0:
        unlimited = simulate(times, closes, highs, BUY_USD, robust_dip, TAKE_PROFIT_PERCENT, interval, args, unlimited_cash=True)

        required_extra = max(0.0, -unlimited["min_cash"])
        required_capital = args.capital + required_extra

        log(
            f"CAPITALE NECESSARIO | per non saltare nessun BUY con DIP {robust_dip}% negli ultimi {days:.0f} giorni "
            f"servirebbero almeno ${required_capital:.2f} (attuale ${args.capital:.2f}, mancano ${required_extra:.2f})"
        )

        args_sufficient = SimpleNamespace(**vars(args))
        args_sufficient.capital = required_capital

        result_sufficient = simulate(times, closes, highs, BUY_USD, robust_dip, TAKE_PROFIT_PERCENT, interval, args_sufficient)

        print(f"\nPROFITTABILITA' CON CAPITALE SUFFICIENTE (${required_capital:.2f}, nessun BUY saltato su DIP {robust_dip}%)")
        print_report(result_sufficient, args_sufficient, closes, days)


if __name__ == "__main__":
    while True:
        try:
            run_optimization()
        except Exception as e:
            log(f"ERRORE OPTIMIZATION | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
