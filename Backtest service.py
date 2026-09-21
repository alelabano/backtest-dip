"""
Servizio backtest per Railway: ogni LOOP_INTERVAL_SECONDS (default 4 ore, come il bot)
rifa il backtest sugli ultimi BACKTEST_DAYS giorni con gli stessi parametri del bot
(stesse variabili d'ambiente) e stampa il report nei log.
Usa solo dati pubblici: non servono chiavi private ne' Volume.
"""

import os
import sys
import time
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace

from dotenv import load_dotenv

from backtest_spot import HOUR_MS, align, fetch_candles, print_report, simulate

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
