"""
BACKTEST / MARKET REGIME ANALYZER - Hyperliquid Spot

Logica:
1) Simula i parametri attuali del bot.
2) Analizza gli ultimi 200 giorni con candele 4H:
   BULLISH / BEARISH / NEUTRAL.
3) Misura DIP e TP fisiologici osservati nel periodo.
4) Confronta il mese corrente con i mesi storici dello stesso regime.
5) Cerca target standard sulle oscillazioni fisiologiche del training.
6) Verifica i target sul 30% finale, non usato nella selezione; se non superano
   il test rispetto ai parametri attuali, non propone alcun cambio.
7) Conserva uno storico JSON per mese/regime.
8) Mantiene il calcolo del capitale necessario e del target mensile.
9) Invia un riepilogo compatto a Telegram.

NOTA:
- HISTORY_FILE deve stare su un Railway Volume se si vuole persistenza
  anche dopo il redeploy/restart del servizio.
"""

import json
import math
import os
import statistics
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import requests
from dotenv import load_dotenv

HOUR_MS = 3600 * 1000
CANDLE_INTERVAL = "4h"
CANDLES_PER_DAY = 6
ANALYSIS_DAYS = 200
ANALYSIS_CANDLES = ANALYSIS_DAYS * CANDLES_PER_DAY

# Event detection on 4H candles.
MIN_SWING_PCT = 1.0
PIVOT_LEFT_RIGHT = 2
BIN_SIZE = 0.5


# ============================================================
# RETRY API
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
                log(
                    f"API RATE LIMIT (429) | tentativo "
                    f"{attempt}/{attempts}, riprovo in {delay}s"
                )
                time.sleep(delay)
                continue
            raise


# ============================================================
# DATI
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
    incremental_start_ms = end_ms - 24 * HOUR_MS

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
            log(f"ATTENZIONE: nessun mercato spot {coin}/USDC")
            continue

        start_ms = (
            incremental_start_ms
            if coin in data and data[coin]
            else full_start_ms
        )

        candles = _with_retry(
            info.candles_snapshot,
            market,
            CANDLE_INTERVAL,
            start_ms,
            end_ms,
        )

        new_candles = [
            {
                "t": c["t"],
                "h": float(c["h"]),
                "l": float(c.get("l", c["c"])),
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
    valid = {c: v for c, v in data.items() if v}

    if not valid:
        raise RuntimeError("Nessun dato disponibile")

    common = sorted(
        set.intersection(
            *[{c["t"] for c in v} for v in valid.values()]
        )
    )

    closes = {}
    highs = {}
    lows = {}

    for coin, values in valid.items():
        by_t = {c["t"]: c for c in values}
        closes[coin] = [by_t[t]["c"] for t in common]
        highs[coin] = [by_t[t]["h"] for t in common]
        lows[coin] = [by_t[t]["l"] for t in common]

    return common, closes, highs, lows


# ============================================================
# SIMULAZIONE BOT
# ============================================================

def simulate(
    times,
    closes,
    highs,
    buy_usd,
    dip,
    tp,
    interval,
    a,
    unlimited_cash=False,
):
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
    missed_buys = 0
    min_cash = a.capital

    per = {
        c: {
            "buys": 0,
            "sells": 0,
            "realized": 0.0,
        }
        for c in coins
    }

    monthly_realized = {}

    warmup = max(1, CANDLES_PER_DAY - 1)

    for i in range(warmup, len(times), interval):
        px = {c: closes[c][i] for c in coins}

        equity = usdc + sum(
            l["qty"] * px[l["coin"]]
            for l in lots
        )

        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(
                max_dd,
                (peak - equity) / peak * 100,
            )

        max_deployed = max(
            max_deployed,
            sum(l["qty"] * l["cost"] for l in lots),
        )

        # SELL
        best = None

        for lot in lots:
            p = px[lot["coin"]]

            if (
                p >= lot["target"]
                and lot["qty"] * a.sell_percent / 100 * p
                >= a.min_order
            ):
                ret = p / lot["buy_price"]

                if best is None or ret > best[0]:
                    best = (ret, lot)

        if best:
            lot = best[1]
            qty = lot["qty"] * a.sell_percent / 100

            proceeds = (
                qty
                * px[lot["coin"]]
                * (1 - a.slippage)
                * (1 - a.fee)
            )

            usdc += proceeds

            pnl = proceeds - qty * lot["cost"]
            realized += pnl

            per[lot["coin"]]["realized"] += pnl

            month_key = datetime.fromtimestamp(
                times[i] / 1000,
                timezone.utc,
            ).strftime("%Y-%m")

            monthly_realized[month_key] = (
                monthly_realized.get(month_key, 0.0)
                + pnl
            )

            per[lot["coin"]]["sells"] += 1
            lot["qty"] -= qty
            sells += 1
            continue

        # BUY
        week = datetime.fromtimestamp(
            times[i] / 1000,
            timezone.utc,
        ).strftime("%G-W%V")

        drops = []

        for c in coins:
            open_lots = [
                l for l in lots
                if l["coin"] == c
            ]

            if open_lots:
                reference = min(
                    l["buy_price"]
                    for l in open_lots
                )
            else:
                reference = max(
                    highs[c][max(0, i - CANDLES_PER_DAY + 1):i + 1]
                )

            if reference > 0:
                drop = (
                    (reference - px[c])
                    / reference
                    * 100
                )
            else:
                drop = 0.0

            drops.append((drop, c))

        drops.sort(reverse=True)

        cash_blocked = False

        for drop, c in drops:
            if drop < dip:
                break

            held = (
                sum(
                    l["qty"]
                    for l in lots
                    if l["coin"] == c
                )
                * px[c]
            )

            if (
                held + buy_usd > a.max_position
                or weekly.get(week, 0) >= a.weekly_buys
            ):
                continue

            if not unlimited_cash and usdc < buy_usd:
                cash_blocked = True
                continue

            fill = px[c] * (1 + a.slippage)
            qty = buy_usd / fill * (1 - a.fee)

            usdc -= buy_usd
            min_cash = min(min_cash, usdc)

            lots.append(
                {
                    "coin": c,
                    "qty": qty,
                    "buy_price": fill,
                    "target": fill * (1 + tp / 100),
                    "cost": buy_usd / qty,
                }
            )

            weekly[week] = weekly.get(week, 0) + 1
            per[c]["buys"] += 1
            buys += 1
            cash_blocked = False
            break

        if cash_blocked:
            missed_buys += 1

    last = {
        c: closes[c][-1]
        for c in coins
    }

    open_value = sum(
        l["qty"] * last[l["coin"]]
        for l in lots
    )

    final = usdc + open_value

    for c in coins:
        c_lots = [
            l for l in lots
            if l["coin"] == c
        ]

        per[c]["unrealized"] = sum(
            l["qty"] * (last[c] - l["cost"])
            for l in c_lots
        )

        per[c]["open"] = sum(
            1
            for l in c_lots
            if l["qty"] * last[c] >= a.min_order
        )

    return {
        "buy": buy_usd,
        "dip": dip,
        "tp": tp,
        "int": interval,
        "ret": (
            (final - a.capital)
            / a.capital
            * 100
            if a.capital
            else 0.0
        ),
        "final": final,
        "realized": realized,
        "unrealized": (
            open_value
            - sum(l["qty"] * l["cost"] for l in lots)
        ),
        "buys": buys,
        "sells": sells,
        "open": sum(
            1
            for l in lots
            if l["qty"] * last[l["coin"]] >= a.min_order
        ),
        "dd": max_dd,
        "deployed": max_deployed,
        "per": per,
        "missed_buys": missed_buys,
        "min_cash": min_cash,
        "monthly_realized": monthly_realized,
    }


def capital_needed(
    times,
    closes,
    highs,
    buy_usd,
    dip,
    tp,
    interval,
    a,
):
    unlimited = simulate(
        times,
        closes,
        highs,
        buy_usd,
        dip,
        tp,
        interval,
        a,
        unlimited_cash=True,
    )

    required_extra = max(
        0.0,
        -unlimited["min_cash"],
    )

    return a.capital + required_extra


# ============================================================
# ANALISI MERCATO 200 GIORNI
# ============================================================

def mean_coin_return(closes, start_idx, end_idx):
    values = []

    for prices in closes.values():
        if (
            len(prices) > end_idx
            and prices[start_idx] > 0
        ):
            values.append(
                (
                    prices[end_idx]
                    / prices[start_idx]
                    - 1
                )
                * 100
            )

    return (
        statistics.mean(values)
        if values
        else 0.0
    )


def market_regime(
    times,
    closes,
    highs,
    lows,
    start_idx=None,
    end_idx=None,
):
    if end_idx is None:
        end_idx = len(times) - 1

    if start_idx is None:
        start_idx = max(
            0,
            end_idx - ANALYSIS_CANDLES,
        )

    returns = []
    range_positions = []
    trend_scores = []

    for c in closes:
        p = closes[c]

        if (
            start_idx >= len(p)
            or end_idx >= len(p)
            or p[start_idx] <= 0
        ):
            continue

        ret = (
            p[end_idx]
            / p[start_idx]
            - 1
        ) * 100

        period_high = max(
            highs[c][start_idx:end_idx + 1]
        )
        period_low = min(
            lows[c][start_idx:end_idx + 1]
        )

        if period_high > period_low:
            position = (
                p[end_idx] - period_low
            ) / (
                period_high - period_low
            ) * 100
        else:
            position = 50.0

        # Confronto seconda metà / prima metà.
        mid = start_idx + (
            end_idx - start_idx
        ) // 2

        first = p[start_idx:mid + 1]
        second = p[mid:end_idx + 1]

        first_avg = (
            statistics.mean(first)
            if first
            else p[start_idx]
        )
        second_avg = (
            statistics.mean(second)
            if second
            else p[end_idx]
        )

        trend = (
            (second_avg / first_avg - 1) * 100
            if first_avg
            else 0.0
        )

        returns.append(ret)
        range_positions.append(position)
        trend_scores.append(trend)

    avg_return = (
        statistics.mean(returns)
        if returns
        else 0.0
    )

    avg_position = (
        statistics.mean(range_positions)
        if range_positions
        else 50.0
    )

    avg_trend = (
        statistics.mean(trend_scores)
        if trend_scores
        else 0.0
    )

    # Classificazione deliberatamente prudente:
    # il rendimento è il segnale principale;
    # posizione nel range e trend fanno da conferma.
    score = (
        avg_return
        + avg_trend * 0.75
        + (avg_position - 50.0) * 0.025
    )

    if (
        avg_return >= 4.0
        and avg_trend >= 1.0
    ) or score >= 5.0:
        regime = "BULLISH"
    elif (
        avg_return <= -4.0
        and avg_trend <= -1.0
    ) or score <= -5.0:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"

    return {
        "regime": regime,
        "return_pct": avg_return,
        "position_pct": avg_position,
        "trend_pct": avg_trend,
        "score": score,
        "start": datetime.fromtimestamp(
            times[start_idx] / 1000,
            timezone.utc,
        ).strftime("%Y-%m-%d"),
        "end": datetime.fromtimestamp(
            times[end_idx] / 1000,
            timezone.utc,
        ).strftime("%Y-%m-%d"),
    }


# ============================================================
# DIP / TP FISIOLOGICI
# ============================================================

def _swing_events_for_coin(prices, highs, lows, start_idx, end_idx):
    """
    Estrae eventi swing su candele 4H.

    Non conta ogni candela.
    Cerca pivot locali e conserva solo movimenti abbastanza ampi da
    rappresentare una correzione/recupero operativo.
    """
    if end_idx - start_idx < (PIVOT_LEFT_RIGHT * 2 + 2):
        return [], []

    left = max(start_idx, PIVOT_LEFT_RIGHT)
    right = min(end_idx, len(prices) - PIVOT_LEFT_RIGHT - 1)

    if right <= left:
        return [], []

    pivots = []

    for i in range(left, right + 1):
        window_h = highs[i - PIVOT_LEFT_RIGHT:i + PIVOT_LEFT_RIGHT + 1]
        window_l = lows[i - PIVOT_LEFT_RIGHT:i + PIVOT_LEFT_RIGHT + 1]

        is_high = highs[i] >= max(window_h)
        is_low = lows[i] <= min(window_l)

        if is_high and not is_low:
            pivots.append(("H", i, highs[i]))
        elif is_low and not is_high:
            pivots.append(("L", i, lows[i]))

    clean = []
    for kind, idx, price in pivots:
        if not clean:
            clean.append((kind, idx, price))
            continue

        last_kind, last_idx, last_price = clean[-1]

        if kind == last_kind:
            if kind == "H" and price > last_price:
                clean[-1] = (kind, idx, price)
            elif kind == "L" and price < last_price:
                clean[-1] = (kind, idx, price)
            continue

        move = abs(price / last_price - 1) * 100 if last_price > 0 else 0.0
        if move >= MIN_SWING_PCT:
            clean.append((kind, idx, price))

    dips = []
    rebounds = []

    for j in range(len(clean) - 1):
        k1, i1, p1 = clean[j]
        k2, i2, p2 = clean[j + 1]

        if k1 == "H" and k2 == "L" and p1 > 0:
            dip = (p1 - p2) / p1 * 100

            if dip >= MIN_SWING_PCT:
                dips.append({
                    "high_idx": i1,
                    "low_idx": i2,
                    "dip_pct": dip,
                })

                if j + 2 < len(clean):
                    k3, i3, p3 = clean[j + 2]

                    if k3 == "H" and p2 > 0:
                        rebound = (p3 - p2) / p2 * 100

                        if rebound >= MIN_SWING_PCT:
                            rebounds.append({
                                "dip_idx": i2,
                                "rebound_idx": i3,
                                "tp_pct": rebound,
                            })
                else:
                    future_high = max(highs[i2:end_idx + 1])

                    if future_high > p2:
                        rebound = (future_high - p2) / p2 * 100

                        if rebound >= MIN_SWING_PCT:
                            rebounds.append({
                                "dip_idx": i2,
                                "rebound_idx": end_idx,
                                "tp_pct": rebound,
                            })

    return dips, rebounds


def physiological_levels(
    closes,
    highs,
    lows,
    start_idx,
    end_idx,
    sell_percent,
):
    """
    Statistica dei DIP/TP fisiologici su candele 4H.

    DIP = correzione H -> L tra swing significativi.
    TP  = recupero L -> H successivo a un DIP.
    """
    dip_values = []
    tp_values = []

    for c in closes:
        p = closes[c]

        if end_idx >= len(p):
            continue

        dips, rebounds = _swing_events_for_coin(
            p,
            highs[c],
            lows[c],
            start_idx,
            end_idx,
        )

        dip_values.extend(e["dip_pct"] for e in dips)
        tp_values.extend(e["tp_pct"] for e in rebounds)

    def summary(values):
        if not values:
            return {
                "count": 0,
                "median": None,
                "p25": None,
                "p75": None,
                "mode": None,
                "frequency": {},
                "mode_frequency": 0.0,
            }

        bins = [
            round(math.floor(v / BIN_SIZE) * BIN_SIZE, 1)
            for v in values
        ]

        counter = Counter(bins)
        mode_value, mode_count = counter.most_common(1)[0]
        total = len(values)

        return {
            "count": total,
            "median": round(statistics.median(values), 2),
            "p25": round(percentile(values, 25), 2),
            "p75": round(percentile(values, 75), 2),
            "mode": mode_value,
            "frequency": {
                f"{k:.1f}": round(v / total * 100, 1)
                for k, v in counter.most_common()
            },
            "mode_frequency": round(mode_count / total * 100, 1),
        }

    dips = [x for x in dip_values if MIN_SWING_PCT <= x <= 20.0]
    tps = [x for x in tp_values if MIN_SWING_PCT <= x <= 25.0]

    return {
        "dip": summary(dips),
        "tp": summary(tps),
        "method": "4H_SWING_EVENTS",
        "min_swing_pct": MIN_SWING_PCT,
        "coins_analyzed": len(closes),
    }


def percentile(values, p):
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    k = (len(values) - 1) * p / 100
    f = math.floor(k)
    c = math.ceil(k)

    if f == c:
        return values[int(k)]

    return (
        values[f]
        + (values[c] - values[f])
        * (k - f)
    )


def calendar_months_in_range(times):
    first = datetime.fromtimestamp(
        times[0] / 1000,
        timezone.utc,
    )
    last = datetime.fromtimestamp(
        times[-1] / 1000,
        timezone.utc,
    )

    months = []

    y, m = first.year, first.month

    while (y, m) <= (last.year, last.month):
        months.append(f"{y:04d}-{m:02d}")

        m += 1
        if m > 12:
            m = 1
            y += 1

    return months


def month_indices(times, month):
    start = None
    end = None

    for i, ts in enumerate(times):
        key = datetime.fromtimestamp(
            ts / 1000,
            timezone.utc,
        ).strftime("%Y-%m")

        if key == month:
            if start is None:
                start = i
            end = i

    return start, end


# ============================================================
# STORICO PERSISTENTE
# ============================================================

def load_history(path):
    try:
        if not path.exists():
            return {}

        with path.open(
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception as e:
        log(f"STORICO | errore lettura {path}: {e}")
        return {}


def save_history(path, history):
    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        tmp = path.with_suffix(
            path.suffix + ".tmp"
        )

        with tmp.open(
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                history,
                f,
                indent=2,
                ensure_ascii=False,
            )

        tmp.replace(path)

    except Exception as e:
        log(f"STORICO | errore salvataggio {path}: {e}")


def analyze_historical_months(
    times,
    closes,
    highs,
    lows,
    months,
    sell_percent,
):
    records = {}

    for month in months:
        start, end = month_indices(
            times,
            month,
        )

        # Con candele 4H, 18 intervalli equivalgono a 3 giorni.
        # Evita di scartare mesi parziali con dati sufficienti per le statistiche.
        if (
            start is None
            or end is None
            or end - start < 18
        ):
            continue

        regime = market_regime(
            times,
            closes,
            highs,
            lows,
            start,
            end,
        )

        levels = physiological_levels(
            closes,
            highs,
            lows,
            start,
            end,
            sell_percent,
        )

        records[month] = {
            "regime": regime["regime"],
            "return_pct": round(
                regime["return_pct"],
                2,
            ),
            "trend_pct": round(
                regime["trend_pct"],
                2,
            ),
            "position_pct": round(
                regime["position_pct"],
                2,
            ),
            "score": round(
                regime["score"],
                2,
            ),
            "dip": levels["dip"],
            "tp": levels["tp"],
        }

    return records


def regime_statistics(
    history,
    regime,
    max_months,
):
    rows = [
        (month, record)
        for month, record in history.items()
        if record.get("regime") == regime
    ]

    rows.sort(key=lambda x: x[0])

    if max_months > 0:
        rows = rows[-max_months:]

    if not rows:
        return None

    dip_modes = [
        r["dip"]["mode"]
        for _, r in rows
        if r.get("dip", {}).get("mode") is not None
    ]

    tp_modes = [
        r["tp"]["mode"]
        for _, r in rows
        if r.get("tp", {}).get("mode") is not None
    ]

    dip_medians = [
        r["dip"]["median"]
        for _, r in rows
        if r.get("dip", {}).get("median") is not None
    ]

    tp_medians = [
        r["tp"]["median"]
        for _, r in rows
        if r.get("tp", {}).get("median") is not None
    ]

    def mode_info(values):
        if not values:
            return None, 0.0

        rounded = [
            round(float(v) * 2) / 2
            for v in values
        ]

        counter = Counter(rounded)
        value, count = counter.most_common(1)[0]

        return (
            value,
            count / len(rounded) * 100,
        )

    dip_mode, dip_freq = mode_info(dip_modes)
    tp_mode, tp_freq = mode_info(tp_modes)

    return {
        "months": len(rows),
        "from": rows[0][0],
        "to": rows[-1][0],
        "dip_mode": dip_mode,
        "dip_frequency": round(dip_freq, 1),
        "tp_mode": tp_mode,
        "tp_frequency": round(tp_freq, 1),
        "dip_median": (
            round(statistics.median(dip_medians), 2)
            if dip_medians
            else None
        ),
        "tp_median": (
            round(statistics.median(tp_medians), 2)
            if tp_medians
            else None
        ),
    }


# ============================================================
# TARGET CONSIGLIATO
# ============================================================

def suggested_targets(current_regime, current_levels, historical):
    """Placeholder statistic targets; operational suggestions come from backtest optimization."""
    def central(level):
        if not level:
            return None
        median, mode = level.get("median"), level.get("mode")
        if median is None:
            return float(mode) if mode is not None else None
        if mode is None:
            return float(median)
        return 0.60 * float(median) + 0.40 * float(mode)

    dip = central(current_levels.get("dip", {}))
    tp = central(current_levels.get("tp", {}))
    if dip is None:
        dip = 2.0
    if tp is None:
        tp = 4.0
    return {
        "dip": round(max(1.0, min(dip, 12.0)) / BIN_SIZE) * BIN_SIZE,
        "tp": round(max(1.0, min(tp, 15.0)) / BIN_SIZE) * BIN_SIZE,
    }


def optimize_targets(times, closes, highs, buy_usd, interval, args,
                     start_idx=0, end_idx=None, physiological=None,
                     train_ratio=0.70):
    """Seleziona target sul periodo di addestramento e li verifica su dati successivi.

    Il 70% iniziale è usato per scegliere i target; il 30% finale resta escluso
    dalla selezione ed è usato una sola volta come test fuori campione.
    La scelta privilegia rendimento mediano, continuità dei risultati e drawdown,
    non il profitto massimo ottenuto sull'intero storico.
    """
    if end_idx is None:
        end_idx = len(times) - 1
    if not 0.60 <= train_ratio <= 0.80:
        raise ValueError("train_ratio deve essere tra 0.60 e 0.80")

    times_all = times[start_idx:end_idx + 1]
    closes_all = {c: v[start_idx:end_idx + 1] for c, v in closes.items()}
    highs_all = {c: v[start_idx:end_idx + 1] for c, v in highs.items()}
    n = len(times_all)
    split = int(n * train_ratio)
    if split < 180 or n - split < 60:
        raise ValueError("Dati insufficienti per selezione e test fuori campione")

    train_times = times_all[:split]
    train_closes = {c: v[:split] for c, v in closes_all.items()}
    train_highs = {c: v[:split] for c, v in highs_all.items()}
    test_times = times_all[split:]
    test_closes = {c: v[split:] for c, v in closes_all.items()}
    test_highs = {c: v[split:] for c, v in highs_all.items()}

    physiological = physiological or {}

    def bounds(level, default_low, default_high, hard_low, hard_high):
        p25 = level.get("p25") if level else None
        p75 = level.get("p75") if level else None
        if p25 is None or p75 is None:
            lo, hi = default_low, default_high
        else:
            # Margine contenuto attorno alla fascia centrale degli swing osservati.
            lo = max(hard_low, math.floor((p25 - 0.5) * 2) / 2)
            hi = min(hard_high, math.ceil((p75 + 0.5) * 2) / 2)
        if hi < lo:
            lo, hi = default_low, default_high
        return lo, hi

    dip_lo, dip_hi = bounds(physiological.get("dip"), 1.5, 4.0, 1.0, 6.0)
    tp_lo, tp_hi = bounds(physiological.get("tp"), 2.0, 6.0, 1.5, 8.0)
    dips = [round(i * 0.5, 1) for i in range(round(dip_lo * 2), round(dip_hi * 2) + 1)]
    tps = [round(i * 0.5, 1) for i in range(round(tp_lo * 2), round(tp_hi * 2) + 1)]

    # Selezione solo nel training: tre finestre cronologiche interne.
    folds = []
    for k in range(3):
        a_idx = k * len(train_times) // 3
        b_idx = (k + 1) * len(train_times) // 3
        if b_idx - a_idx >= 60:
            folds.append((a_idx, b_idx))
    if len(folds) < 3:
        raise ValueError("Dati insufficienti per tre finestre di training")

    candidates = []
    phys_dip = physiological.get("dip", {}).get("median")
    phys_tp = physiological.get("tp", {}).get("median")
    for dip in dips:
        for tp in tps:
            fold_results = []
            for a_idx, b_idx in folds:
                sub_times = train_times[a_idx:b_idx]
                sub_closes = {c: v[a_idx:b_idx] for c, v in train_closes.items()}
                sub_highs = {c: v[a_idx:b_idx] for c, v in train_highs.items()}
                fold_results.append(simulate(
                    sub_times, sub_closes, sub_highs, buy_usd,
                    dip, tp, interval, args,
                ))
            returns = [r["ret"] for r in fold_results]
            median_ret = statistics.median(returns)
            positive_fraction = sum(x > 0 for x in returns) / len(returns)
            worst_dd = max(r["dd"] for r in fold_results)
            avg_missed = statistics.mean(r["missed_buys"] for r in fold_results)
            total_sells = sum(r["sells"] for r in fold_results)
            # Penalizza oscillazioni del risultato, drawdown e target lontani dai livelli centrali.
            spread = max(returns) - min(returns)
            distance = (abs(dip - phys_dip) if phys_dip is not None else 0.0) + \
                       (abs(tp - phys_tp) if phys_tp is not None else 0.0)
            score = median_ret - 0.15 * worst_dd - 0.10 * spread
            candidates.append({
                "score": score,
                "median_ret": median_ret,
                "positive_fraction": positive_fraction,
                "worst_dd": worst_dd,
                "avg_missed": avg_missed,
                "distance": distance,
                "dip": dip,
                "tp": tp,
                "sells": total_sells,
            })

    # Prima rendimento robusto, poi continuità, drawdown, ordini saltati e vicinanza
    # alle oscillazioni centrali. Esclude coppie che non hanno generato vendite.
    viable = [c for c in candidates if c["sells"] >= 3]
    if not viable:
        viable = candidates
    best = max(viable, key=lambda c: (
        c["score"], c["positive_fraction"], -c["worst_dd"],
        -c["avg_missed"], -c["distance"],
    ))

    candidate_test = simulate(
        test_times, test_closes, test_highs, buy_usd,
        best["dip"], best["tp"], interval, args,
    )
    baseline_test = simulate(
        test_times, test_closes, test_highs, buy_usd,
        CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, interval, args,
    )
    # Il target è convalidato solo se il test escluso dalla selezione è positivo,
    # supera il baseline di almeno 0,5 punti percentuali, ha vendite reali e
    # non peggiora il drawdown di oltre 2 punti.
    improvement = candidate_test["ret"] - baseline_test["ret"]
    validated = (
        candidate_test["ret"] > 0
        and improvement >= 0.5
        and candidate_test["sells"] >= 3
        and candidate_test["dd"] <= baseline_test["dd"] + 2.0
    )

    full_candidate = simulate(
        times_all, closes_all, highs_all, buy_usd,
        best["dip"], best["tp"], interval, args,
    )
    full_baseline = simulate(
        times_all, closes_all, highs_all, buy_usd,
        CURRENT_DIP_PERCENT, CURRENT_TP_PERCENT, interval, args,
    )

    train_end_ts = train_times[-1]
    test_start_ts = test_times[0]
    date_fmt = lambda ts: datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
    return {
        "dip": best["dip"],
        "tp": best["tp"],
        "validated": validated,
        "result": full_candidate,
        "baseline_full": full_baseline,
        "profit": full_candidate["final"] - args.capital,
        "ret": full_candidate["ret"],
        "missed_buys": full_candidate["missed_buys"],
        "train_median_ret": best["median_ret"],
        "train_positive_fraction": best["positive_fraction"],
        "train_worst_dd": best["worst_dd"],
        "test_result": candidate_test,
        "baseline_test": baseline_test,
        "test_improvement": improvement,
        "test_start": date_fmt(test_start_ts),
        "train_end": date_fmt(train_end_ts),
        "candidate_count": len(candidates),
        "dip_range": (dip_lo, dip_hi),
        "tp_range": (tp_lo, tp_hi),
        "train_windows": len(folds),
    }


# ============================================================
# CAPITALE TARGET MENSILE
# ============================================================

def monthly_median_at_capital(
    times,
    closes,
    highs,
    buy_usd,
    dip,
    tp,
    interval,
    a,
    capital,
    months,
):
    a2 = SimpleNamespace(**vars(a))
    a2.capital = capital

    r = simulate(
        times,
        closes,
        highs,
        buy_usd,
        dip,
        tp,
        interval,
        a2,
    )

    pnls = [
        r["monthly_realized"].get(
            m,
            0.0,
        )
        for m in months
    ]

    median = (
        statistics.median(pnls)
        if pnls
        else 0.0
    )

    stdev = (
        statistics.pstdev(pnls)
        if len(pnls) > 1
        else 0.0
    )

    return median, stdev, r


def find_capital_for_monthly_target(
    times,
    closes,
    highs,
    buy_usd,
    dip,
    tp,
    interval,
    a,
    target,
    months,
):
    lo = a.capital

    lo_median, _, _ = monthly_median_at_capital(
        times,
        closes,
        highs,
        buy_usd,
        dip,
        tp,
        interval,
        a,
        lo,
        months,
    )

    if lo_median >= target:
        return lo, lo_median, True

    hi = max(
        lo * 2,
        50.0,
    )

    prev_median = lo_median
    hi_median = lo_median

    for _ in range(12):
        hi_median, _, _ = monthly_median_at_capital(
            times,
            closes,
            highs,
            buy_usd,
            dip,
            tp,
            interval,
            a,
            hi,
            months,
        )

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

        mid_median, _, _ = monthly_median_at_capital(
            times,
            closes,
            highs,
            buy_usd,
            dip,
            tp,
            interval,
            a,
            mid,
            months,
        )

        if mid_median >= target:
            hi = mid
        else:
            lo = mid

    final_median, _, _ = monthly_median_at_capital(
        times,
        closes,
        highs,
        buy_usd,
        dip,
        tp,
        interval,
        a,
        hi,
        months,
    )

    return hi, final_median, True


# ============================================================
# CAPITALE REALE
# ============================================================

def get_real_capital(
    coins,
    last_prices,
    info,
    meta,
):
    user_state = _with_retry(
        info.spot_user_state,
        ACCOUNT_ADDRESS,
    )

    extra_prices = {}

    if CAPITAL_EXTRA_COINS:
        usdc_idx = next(
            i
            for i, t in enumerate(meta["tokens"])
            if t["name"] == "USDC"
        )

        for coin in CAPITAL_EXTRA_COINS:
            found_token = False

            for idx, token in enumerate(
                meta["tokens"]
            ):
                if token["name"] in (
                    coin,
                    "U" + coin,
                ):
                    found_token = True

                    market = next(
                        (
                            m["name"]
                            for m in meta["universe"]
                            if m["tokens"]
                            == [idx, usdc_idx]
                        ),
                        None,
                    )

                    if not market:
                        log(
                            f"CAPITALE REALE | {coin}: "
                            f"nessun mercato spot"
                        )
                        break

                    book = _with_retry(
                        info.l2_snapshot,
                        market,
                    )

                    levels = book.get(
                        "levels",
                        [],
                    )

                    if (
                        len(levels) == 2
                        and levels[0]
                        and levels[1]
                    ):
                        extra_prices[coin] = (
                            float(levels[0][0]["px"])
                            + float(levels[1][0]["px"])
                        ) / 2

                    break

            if not found_token:
                log(
                    f"CAPITALE REALE | {coin}: "
                    f"token non trovato"
                )

    usdc_balance = 0.0
    coins_value = 0.0
    extra_value = 0.0

    spot_balances = user_state.get("balances", [])
    if not isinstance(spot_balances, list):
        raise ValueError(
            "Risposta spot_user_state inattesa: 'balances' non è una lista"
        )

    # Diagnostica esplicita: permette di vedere quale saldo restituisce
    # davvero l'API e quanto è libero (total - hold).
    log(
        "CAPITALE REALE | indirizzo letto: "
        f"{ACCOUNT_ADDRESS[:6]}...{ACCOUNT_ADDRESS[-4:]}"
        if ACCOUNT_ADDRESS and len(ACCOUNT_ADDRESS) > 12
        else f"CAPITALE REALE | indirizzo letto: {ACCOUNT_ADDRESS or 'NON CONFIGURATO'}"
    )
    for balance in spot_balances:
        coin_label = str(balance.get("coin", ""))
        try:
            total_debug = float(balance.get("total", 0) or 0)
            hold_debug = float(balance.get("hold", 0) or 0)
        except (TypeError, ValueError):
            total_debug, hold_debug = 0.0, 0.0
        log(
            f"SALDO SPOT API | coin={coin_label!r} "
            f"total=${total_debug:.8f} hold=${hold_debug:.8f} "
            f"libero=${max(0.0, total_debug-hold_debug):.8f}"
        )

    for balance in spot_balances:
        name = str(balance.get("coin", "")).strip().upper()
        total = float(balance.get("total", 0) or 0)

        if name in ("USDC", "USDC(0)"):
            usdc_balance += total
            continue

        handled = False

        for coin in coins:
            if (
                name in (
                    coin,
                    "U" + coin,
                )
                and coin in last_prices
            ):
                coins_value += (
                    total * last_prices[coin]
                )
                handled = True
                break

        if handled:
            continue

        for coin in CAPITAL_EXTRA_COINS:
            if (
                name in (
                    coin,
                    "U" + coin,
                )
                and coin in extra_prices
            ):
                extra_value += (
                    total * extra_prices[coin]
                )
                break

    total_capital = (
        usdc_balance
        + coins_value
        + extra_value
    )

    if not spot_balances:
        log(
            "ERRORE CAPITALE | l'API non ha restituito saldi Spot; "
            "verificare ACCOUNT_ADDRESS e il wallet configurato"
        )
    elif usdc_balance == 0:
        log(
            "ATTENZIONE CAPITALE | nessuna riga USDC riconosciuta nella "
            "risposta Spot; consultare le righe SALDO SPOT API sopra"
        )

    log(
        f"CAPITALE REALE | USDC "
        f"${usdc_balance:.2f} + coin gestite "
        f"${coins_value:.2f} + coin extra "
        f"${extra_value:.2f} = "
        f"${total_capital:.2f}"
    )

    return total_capital


# ============================================================
# REPORT / TELEGRAM
# ============================================================

def print_report(
    title,
    r,
    a,
    closes,
):
    print("=" * 68)
    print(title)
    print("=" * 68)

    print(
        f"BUY ${r['buy']:.0f} | "
        f"DIP {r['dip']:.1f}% | "
        f"TP {r['tp']:.1f}% | "
        f"frequenza simulazione ogni {r['int']} candela/e da {CANDLE_INTERVAL}"
    )

    print(
        f"Capitale iniziale     ${a.capital:>10.2f}"
    )
    print(
        f"Valore finale         ${r['final']:>10.2f} "
        f"({r['ret']:+.2f}%)"
    )
    print(
        f"Profitto realizzato   ${r['realized']:>10.2f}"
    )
    print(
        f"Non realizzato        ${r['unrealized']:>10.2f}"
    )
    print(
        f"BUY / SELL             "
        f"{r['buys']:>5} / {r['sells']:<5}"
    )
    print(
        f"BUY saltati            "
        f"{r['missed_buys']:>5}"
    )
    print(
        f"Max capitale investito "
        f"${r['deployed']:>9.2f}"
    )
    print(
        f"Max drawdown            "
        f"{r['dd']:>8.2f}%"
    )

    print("-" * 68)

    for c, v in r["per"].items():
        move = (
            (
                closes[c][-1]
                / closes[c][0]
                - 1
            ) * 100
            if closes[c][0]
            else 0
        )

        print(
            f"{c:<7} "
            f"BUY {v['buys']:>4} "
            f"SELL {v['sells']:>4} "
            f"real. ${v['realized']:>8.2f} "
            f"move {move:+7.2f}%"
        )

    print("=" * 68)


def send_telegram(message):
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return

    try:
        requests.post(
            "https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=10,
        )
    except Exception as e:
        log(
            f"TELEGRAM ERRORE | {e}"
        )


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

sys.stdout.reconfigure(
    line_buffering=True
)

COINS = [
    c.strip().upper()
    for c in os.getenv(
        "COINS",
        "BTC",
    ).split(",")
    if c.strip()
]

LOOP_INTERVAL_SECONDS = int(
    os.getenv(
        "LOOP_INTERVAL_SECONDS",
        "14400",
    )
)

BUY_USD = float(
    os.getenv(
        "BUY_USD",
        "10",
    )
)

CURRENT_DIP_PERCENT = float(
    os.getenv(
        "DIP_PERCENT",
        "2.0",
    )
)

CURRENT_TP_PERCENT = float(
    os.getenv(
        "TAKE_PROFIT_PERCENT",
        "4.0",
    )
)

SELL_PERCENT = float(
    os.getenv(
        "SELL_PERCENT",
        "95",
    )
)

MIN_ORDER_USD = float(
    os.getenv(
        "MIN_ORDER_USD",
        "10",
    )
)

BACKTEST_DAYS = min(
    int(
        os.getenv(
            "BACKTEST_DAYS",
            "200",
        )
    ),
    208,
)

TARGET_MONTHLY_PROFIT = float(
    os.getenv(
        "TARGET_MONTHLY_PROFIT",
        "30",
    )
)

HISTORY_FILE = Path(
    os.getenv(
        "HISTORY_FILE",
        "/data/market_history.json",
    )
)

HISTORY_MONTHS = int(
    os.getenv(
        "HISTORY_MONTHS",
        "24",
    )
)

ACCOUNT_ADDRESS = (
    os.getenv(
        "HYPERLIQUID_ACCOUNT_ADDRESS"
    )
    or os.getenv(
        "HL_ACCOUNT_ADDRESS"
    )
    or os.getenv(
        "ACCOUNT_ADDRESS"
    )
)

CAPITAL_EXTRA_COINS = [
    c.strip().upper()
    for c in os.getenv(
        "CAPITAL_EXTRA_COINS",
        "",
    ).split(",")
    if c.strip()
]

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)

args = SimpleNamespace(
    capital=float(
        os.getenv(
            "BACKTEST_CAPITAL",
            "1000",
        )
    ),
    sell_percent=SELL_PERCENT,
    max_position=float(
        os.getenv(
            "MAX_POSITION_USD",
            "200",
        )
    ),
    weekly_buys=int(
        os.getenv(
            "MAX_WEEKLY_BUYS",
            "10",
        )
    ),
    min_order=MIN_ORDER_USD,
    fee=float(
        os.getenv(
            "BACKTEST_FEE",
            "0.0007",
        )
    ),
    slippage=float(
        os.getenv(
            "BACKTEST_SLIPPAGE",
            "0.0005",
        )
    ),
)

DATA_CACHE = None
INFO_CACHE = None
META_CACHE = None


def log(message):
    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    print(
        f"[{now}] {message}",
        flush=True,
    )


# ============================================================
# RUN
# ============================================================

def run():
    global DATA_CACHE
    global INFO_CACHE
    global META_CACHE

    log("=" * 68)
    log("AVVIO NUOVO CICLO")
    log(f"ASSET CONFIGURATI: {', '.join(COINS)} | intervallo candele {CANDLE_INTERVAL} | giorni {BACKTEST_DAYS}")
    log("=" * 68)

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    if INFO_CACHE is None:
        INFO_CACHE = Info(
            constants.MAINNET_API_URL,
            skip_ws=True,
        )

    if META_CACHE is None:
        META_CACHE = _with_retry(
            INFO_CACHE.spot_meta
        )

    DATA_CACHE = fetch_candles(
        COINS,
        BACKTEST_DAYS,
        existing_data=DATA_CACHE,
        info=INFO_CACHE,
        meta=META_CACHE,
    )

    times, closes, highs, lows = align(
        DATA_CACHE
    )

    months = calendar_months_in_range(
        times
    )

    last_prices = {
        c: closes[c][-1]
        for c in closes
    }

    if ACCOUNT_ADDRESS:
        try:
            args.capital = get_real_capital(
                COINS,
                last_prices,
                INFO_CACHE,
                META_CACHE,
            )
        except Exception as e:
            log(
                f"ATTENZIONE capitale reale: "
                f"{e}"
            )

    # --------------------------------------------------------
    # A. PARAMETRI ATTUALI
    # --------------------------------------------------------

    current_result = simulate(
        times,
        closes,
        highs,
        BUY_USD,
        CURRENT_DIP_PERCENT,
        CURRENT_TP_PERCENT,
        1,
        args,
    )

    current_required = capital_needed(
        times,
        closes,
        highs,
        BUY_USD,
        CURRENT_DIP_PERCENT,
        CURRENT_TP_PERCENT,
        1,
        args,
    )

    required_capital = max(args.capital, current_required)
    funded_args = SimpleNamespace(**vars(args))
    funded_args.capital = required_capital
    funded_result = simulate(
        times, closes, highs, BUY_USD, CURRENT_DIP_PERCENT,
        CURRENT_TP_PERCENT, 1, funded_args,
    )

    # --------------------------------------------------------
    # B. ANALISI COMPLESSIVA 200 GIORNI + MESE CORRENTE
    # --------------------------------------------------------

    analysis_start = max(0, len(times) - ANALYSIS_CANDLES)
    analysis_regime = market_regime(
        times, closes, highs, lows,
        analysis_start, len(times) - 1,
    )
    analysis_levels = physiological_levels(
        closes, highs, lows,
        analysis_start, len(times) - 1, SELL_PERCENT,
    )

    current_month = datetime.fromtimestamp(
        times[-1] / 1000, timezone.utc
    ).strftime("%Y-%m")
    current_start, current_end = month_indices(times, current_month)
    if current_start is None:
        current_start = analysis_start
    if current_end is None:
        current_end = len(times) - 1

    current_regime = market_regime(
        times, closes, highs, lows, current_start, current_end,
    )
    current_levels = physiological_levels(
        closes, highs, lows, current_start, current_end, SELL_PERCENT,
    )

    log(
        f"REGIME 200G | "
        f"{current_regime['regime']} | "
        f"rendimento {current_regime['return_pct']:+.2f}% | "
        f"trend {current_regime['trend_pct']:+.2f}% | "
        f"range-pos {current_regime['position_pct']:.1f}%"
    )

    log(
        f"200G FISIOLOGICO | "
        f"DIP mode {analysis_levels['dip']['mode']}% "
        f"freq {analysis_levels['dip']['mode_frequency']}% | "
        f"TP mode {analysis_levels['tp']['mode']}% "
        f"freq {analysis_levels['tp']['mode_frequency']}%"
    )

    # --------------------------------------------------------
    # C. ANALISI STORICA MESE PER MESE
    # --------------------------------------------------------

    new_month_records = analyze_historical_months(
        times,
        closes,
        highs,
        lows,
        months,
        SELL_PERCENT,
    )

    history = load_history(
        HISTORY_FILE
    )

    # Aggiorna i mesi già esistenti.
    for month, record in new_month_records.items():
        history[month] = record

    # Limite storico.
    all_months = sorted(history.keys())
    if HISTORY_MONTHS > 0:
        for old_month in all_months[:-HISTORY_MONTHS]:
            del history[old_month]

    save_history(
        HISTORY_FILE,
        history,
    )

    hist_stats = regime_statistics(
        history,
        current_regime["regime"],
        HISTORY_MONTHS,
    )

    if hist_stats is None:
        hist_stats = {
            "months": 0,
            "from": None,
            "to": None,
            "dip_mode": None,
            "dip_frequency": 0.0,
            "tp_mode": None,
            "tp_frequency": 0.0,
            "dip_median": None,
            "tp_median": None,
        }

    # --------------------------------------------------------
    # D. TARGET CONSIGLIATI
    # --------------------------------------------------------

    # I livelli fisiologici usati per costruire i candidati sono calcolati
    # esclusivamente sul 70% iniziale, mai sul periodo di test.
    split_idx = analysis_start + int((len(times) - analysis_start) * 0.70)
    train_levels = physiological_levels(
        closes, highs, lows, analysis_start, max(analysis_start, split_idx - 1), SELL_PERCENT,
    )
    optimized = optimize_targets(
        times, closes, highs, BUY_USD, 1, args,
        analysis_start, len(times) - 1, train_levels,
    )
    # Se i target non superano il test indipendente, non cambiare i parametri correnti.
    suggested = (
        {"dip": optimized["dip"], "tp": optimized["tp"]}
        if optimized["validated"]
        else {"dip": CURRENT_DIP_PERCENT, "tp": CURRENT_TP_PERCENT}
    )
    log(
        f"TARGET CANDIDATO (training) | DIP {optimized['dip']:.1f}% / TP {optimized['tp']:.1f}% | "
        f"fasce fisiologiche DIP {optimized['dip_range'][0]:.1f}-{optimized['dip_range'][1]:.1f}% "
        f"TP {optimized['tp_range'][0]:.1f}-{optimized['tp_range'][1]:.1f}% | "
        f"training mediano {optimized['train_median_ret']:+.2f}% "
        f"finestre positive {optimized['train_positive_fraction']:.0%}"
    )
    log(
        f"TEST FUORI CAMPIONE {optimized['test_start']} -> {datetime.fromtimestamp(times[-1] / 1000, timezone.utc).strftime('%Y-%m-%d')} | "
        f"candidato {optimized['test_result']['ret']:+.2f}% "
        f"(${optimized['test_result']['final'] - args.capital:+.2f}), "
        f"SELL {optimized['test_result']['sells']}, DD {optimized['test_result']['dd']:.2f}% | "
        f"attuale {optimized['baseline_test']['ret']:+.2f}% "
        f"(${optimized['baseline_test']['final'] - args.capital:+.2f}), "
        f"SELL {optimized['baseline_test']['sells']}, DD {optimized['baseline_test']['dd']:.2f}% | "
        f"validato={'SI' if optimized['validated'] else 'NO'}"
    )

    target_changed = (
        abs(suggested["dip"] - CURRENT_DIP_PERCENT) >= 0.5
        or abs(suggested["tp"] - CURRENT_TP_PERCENT) >= 0.5
    )
    if not optimized["validated"]:
        suggestion = "NESSUN CAMBIO: target candidato non supera il test indipendente"
    elif not target_changed:
        suggestion = f"MANTENERE DIP {CURRENT_DIP_PERCENT:.1f}% / TP {CURRENT_TP_PERCENT:.1f}%"
    else:
        suggestion = f"CAMBIO VALIDATO -> DIP {suggested['dip']:.1f}% / TP {suggested['tp']:.1f}%"

    # --------------------------------------------------------
    # E. CAPITALE CON TARGET SUGGERITI
    # --------------------------------------------------------

    cap_curr, _, reach_curr = (
        find_capital_for_monthly_target(
            times,
            closes,
            highs,
            BUY_USD,
            CURRENT_DIP_PERCENT,
            CURRENT_TP_PERCENT,
            1,
            args,
            TARGET_MONTHLY_PROFIT,
            months,
        )
    )

    cap_suggested, _, reach_suggested = (
        find_capital_for_monthly_target(
            times,
            closes,
            highs,
            BUY_USD,
            suggested["dip"],
            suggested["tp"],
            1,
            args,
            TARGET_MONTHLY_PROFIT,
            months,
        )
    )

    # --------------------------------------------------------
    # F. REPORT LOG
    # --------------------------------------------------------

    print_report(
        "PARAMETRI ATTUALI",
        current_result,
        args,
        closes,
    )

    print("\n" + "=" * 68)
    print("SIMULAZIONE CON CAPITALE NECESSARIO | PARAMETRI ATTUALI")
    print("=" * 68)
    print(f"Capitale attuale: ${args.capital:.2f}")
    print(f"Buy saltati per fondi insufficienti: {current_result['missed_buys']}")
    print(f"Capitale stimato per non saltare buy: ${required_capital:.2f}")
    print(f"Capitale aggiuntivo necessario: ${max(0, required_capital - args.capital):.2f}")
    print_report("CAPITALE NECESSARIO - BUY NON SALTATI", funded_result, funded_args, closes)

    print("\n" + "=" * 68)
    print("ANALISI MERCATO 200 GIORNI | CANDELE 4H")
    print("=" * 68)

    print(
        f"Periodo: "
        f"{current_regime['start']} -> "
        f"{current_regime['end']}"
    )

    for coin in closes:
        p_start = closes[coin][analysis_start]
        p_end = closes[coin][-1]
        move = (p_end / p_start - 1) * 100 if p_start else 0.0
        print(f"MERCATO 200G {coin}: {analysis_regime['regime']} | prezzo iniziale {p_start:.6g} -> finale {p_end:.6g} | variazione reale {move:+.2f}%")
    print(f"REGIME MESE CORRENTE ({current_month}): {current_regime['regime']} | rendimento {current_regime['return_pct']:+.2f}% | trend {current_regime['trend_pct']:+.2f}%")
    print("\nREGIME E DIP/TP PER MESE (periodo disponibile):")
    for month in months:
        start, end = month_indices(times, month)
        if start is None or end is None or end - start < 6:
            continue
        mr = market_regime(times, closes, highs, lows, start, end)
        ml = physiological_levels(closes, highs, lows, start, end, SELL_PERCENT)
        print(f"{month} | {mr['regime']:<8} | rendimento {mr['return_pct']:+6.2f}% | DIP {ml['dip']['mode']}% (n={ml['dip']['count']}) | TP {ml['tp']['mode']}% (n={ml['tp']['count']})")
    print(
        f"REGIME: {current_regime['regime']}"
    )

    print(
        f"Rendimento medio: "
        f"{current_regime['return_pct']:+.2f}%"
    )

    print(
        f"Trend seconda metà: "
        f"{current_regime['trend_pct']:+.2f}%"
    )

    print(
        f"Posizione nel range: "
        f"{current_regime['position_pct']:.1f}%"
    )

    print(
        f"Score: "
        f"{current_regime['score']:+.2f}"
    )

    print(
        f"DIP PIU FREQUENTE 200G: mode {analysis_levels['dip']['mode']}% "
        f"(frequenza {analysis_levels['dip']['mode_frequency']}%, n={analysis_levels['dip']['count']}) | "
        f"mediana {analysis_levels['dip']['median']}% | "
        f"P25/P75 {analysis_levels['dip']['p25']}/{analysis_levels['dip']['p75']}%"
    )
    print(
        f"TP PIU FREQUENTE 200G: mode {analysis_levels['tp']['mode']}% "
        f"(frequenza {analysis_levels['tp']['mode_frequency']}%, n={analysis_levels['tp']['count']}) | "
        f"mediana {analysis_levels['tp']['median']}% | "
        f"P25/P75 {analysis_levels['tp']['p25']}/{analysis_levels['tp']['p75']}%"
    )
    print(f"TARGET FISIOLOGICI DEL MESE: DIP {current_levels['dip']['mode']}% / TP {current_levels['tp']['mode']}%")

    print("\n" + "=" * 68)
    print(
        f"STORICO {current_regime['regime']}"
    )
    print("=" * 68)

    print(
        f"Mesi considerati: "
        f"{hist_stats['months']}"
    )

    if hist_stats["months"]:
        print(
            f"DIP storico più frequente: "
            f"{hist_stats['dip_mode']:.1f}% "
            f"({hist_stats['dip_frequency']:.0f}% dei mesi)"
        )

        print(
            f"TP storico più frequente: "
            f"{hist_stats['tp_mode']:.1f}% "
            f"({hist_stats['tp_frequency']:.0f}% dei mesi)"
        )

        print(
            f"DIP mediano dei mesi: "
            f"{hist_stats['dip_median']:.2f}%"
        )

        print(
            f"TP mediano dei mesi: "
            f"{hist_stats['tp_median']:.2f}%"
        )

    print(
        f"\nTARGET ADOTTABILI (solo se validati): "
        f"DIP {suggested['dip']:.1f}% / "
        f"TP {suggested['tp']:.1f}%"
    )

    print(
        f"SUGGERIMENTO: {suggestion}"
    )

    # --------------------------------------------------------
    # G. TELEGRAM
    # --------------------------------------------------------

    cap_curr_txt = (
        f"${cap_curr:.0f}"
        if reach_curr and cap_curr
        else "N/D (storico insufficiente o target non raggiunto)"
    )

    cap_suggested_txt = (
        f"${cap_suggested:.0f}"
        if reach_suggested and cap_suggested
        else "N/D (storico insufficiente o target non raggiunto)"
    )

    asset_moves_text = "; ".join(
        f"{coin} {(closes[coin][-1] / closes[coin][analysis_start] - 1) * 100:+.1f}%"
        for coin in closes
        if closes[coin][analysis_start] > 0
    )
    tg = (
        f"ASSET: {', '.join(closes.keys())}\n"
        f"VARIAZIONE PREZZI 200G: {asset_moves_text}\n"
        f"MEDIA SEMPLICE RENDIMENTI ASSET 200G: {analysis_regime['regime']} {analysis_regime['return_pct']:+.1f}%\n"
        f"MESE {current_month}: {current_regime['regime']} "
        f"{current_regime['return_pct']:+.1f}%\n"
        f"DIP/TP mese: {current_levels['dip']['mode']}% / "
        f"{current_levels['tp']['mode']}%\n"
        f"DIP/TP 200g: {analysis_levels['dip']['mode']}% / "
        f"{analysis_levels['tp']['mode']}%\n\n"
        f"Storico {current_regime['regime']}: "
        f"{hist_stats['months']} mesi\n"
        f"DIP: "
        f"{hist_stats['dip_mode'] if hist_stats['dip_mode'] is not None else 'N/D'}% | "
        f"TP: "
        f"{hist_stats['tp_mode'] if hist_stats['tp_mode'] is not None else 'N/D'}%\n\n"
        f"Attuale: "
        f"DIP {CURRENT_DIP_PERCENT:.1f}% / "
        f"TP {CURRENT_TP_PERCENT:.1f}%\n"
        f"TARGET CANDIDATO: DIP {optimized['dip']:.1f}% / TP {optimized['tp']:.1f}%\n"
        f"TEST INDIPENDENTE ({optimized['test_start']} -> {datetime.fromtimestamp(times[-1] / 1000, timezone.utc).strftime('%Y-%m-%d')}):\n"
        f"candidato {optimized['test_result']['ret']:+.1f}% (${optimized['test_result']['final'] - args.capital:+.2f}), "
        f"SELL {optimized['test_result']['sells']}, DD {optimized['test_result']['dd']:.1f}%\n"
        f"attuale {optimized['baseline_test']['ret']:+.1f}% (${optimized['baseline_test']['final'] - args.capital:+.2f}), "
        f"SELL {optimized['baseline_test']['sells']}, DD {optimized['baseline_test']['dd']:.1f}%\n"
        f"Esito test: {'VALIDATO' if optimized['validated'] else 'NON VALIDATO'}\n"
        f"Backtest 200g candidato: {optimized['ret']:+.1f}% (${optimized['profit']:+.2f}); "
        f"buy saltati {optimized['missed_buys']}\n\n"
        f"BOT capitale attuale ${args.capital:.2f}: "
        f"{current_result['ret']:+.1f}% (${current_result['final'] - args.capital:+.2f})\n"
        f"Buy saltati: {current_result['missed_buys']}\n"
        f"Capitale per zero buy saltati: ${required_capital:.2f}\n"
        f"BOT con capitale necessario: {funded_result['ret']:+.1f}% "
        f"(${funded_result['final'] - required_capital:+.2f})\n\n"
        f"Capitale target ${TARGET_MONTHLY_PROFIT:.0f}/mese:\n"
        f"attuale {cap_curr_txt} | "
        f"suggerito {cap_suggested_txt}\n\n"
        f"Oscillazioni standard osservate: DIP {optimized['dip_range'][0]:.1f}-{optimized['dip_range'][1]:.1f}% / "
        f"TP {optimized['tp_range'][0]:.1f}-{optimized['tp_range'][1]:.1f}%\n"
        f"{suggestion}"
    )

    send_telegram(tg)

    log(
        f"\n[TELEGRAM]\n{tg}"
    )


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":
    log(
        "Servizio Backtest/Market Analyzer avviato..."
    )

    while True:
        try:
            run()

        except Exception as e:
            log(
                f"ERRORE NEL CICLO: {e}"
            )
            traceback.print_exc()

        log(
            f"Prossimo ciclo tra "
            f"{LOOP_INTERVAL_SECONDS} secondi..."
        )

        time.sleep(
            LOOP_INTERVAL_SECONDS
        )
