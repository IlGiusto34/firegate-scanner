#!/usr/bin/env python3
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

DATA_BASE = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"
NY = ZoneInfo("America/New_York")

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MIN_CHANGE = float(os.getenv("MIN_CHANGE", "4.0"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "2000000000"))
MIN_AVG_DOLLAR_VOLUME = float(os.getenv("MIN_AVG_DOLLAR_VOLUME", "20000000"))
MIN_RVOL_PACE = float(os.getenv("MIN_RVOL_PACE", "1.8"))
TOP_MOVERS = int(os.getenv("TOP_MOVERS", "50"))

STATE_FILE = Path("alerts_state.json")
MCAP_CACHE_FILE = Path("market_caps.json")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}

session = requests.Session()
session.headers.update(HEADERS)


def request_json(url, params=None, timeout=20):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def market_is_open():
    try:
        data = request_json(f"{PAPER_BASE}/v2/clock")
        return bool(data.get("is_open"))
    except Exception as exc:
        print(f"[ERRORE] Impossibile verificare lo stato del mercato: {exc}")
        return False


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:500]}")
    return True


def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Errore lettura {path}: {exc}")
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def prune_alert_state(state, keep_days=7):
    cutoff = datetime.now(NY).date() - timedelta(days=keep_days)
    out = {}
    for day, tickers in state.items():
        try:
            if datetime.strptime(day, "%Y-%m-%d").date() >= cutoff:
                out[day] = tickers
        except ValueError:
            pass
    return out


def get_market_cap(symbol, cache):
    today = datetime.now(NY).date()
    item = cache.get(symbol)
    if item:
        try:
            updated = datetime.strptime(item["updated"], "%Y-%m-%d").date()
            if (today - updated).days <= 7 and float(item["market_cap"]) > 0:
                return float(item["market_cap"])
        except Exception:
            pass

    market_cap = None
    try:
        ticker = yf.Ticker(symbol)
        fi = ticker.fast_info
        try:
            market_cap = float(fi["market_cap"])
        except Exception:
            market_cap = None

        if not market_cap or not math.isfinite(market_cap):
            info = ticker.get_info()
            raw = info.get("marketCap")
            if raw:
                market_cap = float(raw)
    except Exception as exc:
        print(f"[WARN] Market cap non disponibile per {symbol}: {exc}")

    if market_cap and math.isfinite(market_cap):
        cache[symbol] = {
            "market_cap": market_cap,
            "updated": today.isoformat(),
        }
        return market_cap
    return None


def get_movers():
    data = request_json(
        f"{DATA_BASE}/v1beta1/screener/stocks/movers",
        params={"top": TOP_MOVERS},
    )
    return data.get("gainers", [])


def get_daily_history(symbol):
    now = datetime.now(NY)
    start = (now.date() - timedelta(days=400)).isoformat()
    # Solo sedute completate: così il SIP storico resta utilizzabile senza feed realtime SIP.
    end = (now.date() - timedelta(days=1)).isoformat()

    data = request_json(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        params={
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "limit": 10000,
            "adjustment": "split",
            "feed": "sip",
            "sort": "asc",
        },
    )
    return data.get("bars", [])


def get_delayed_snapshot(symbol):
    return request_json(
        f"{DATA_BASE}/v2/stocks/{symbol}/snapshot",
        params={"feed": "delayed_sip"},
    )


def recent_news_symbols(symbols):
    if not symbols:
        return set()

    start = (datetime.now(NY) - timedelta(hours=24)).astimezone(
        ZoneInfo("UTC")
    ).isoformat()

    try:
        data = request_json(
            f"{DATA_BASE}/v1beta1/news",
            params={
                "symbols": ",".join(symbols),
                "start": start,
                "limit": 50,
                "sort": "desc",
            },
        )
    except Exception as exc:
        print(f"[WARN] News non disponibili: {exc}")
        return set()

    found = set()
    for item in data.get("news", []):
        for s in item.get("symbols", []) or []:
            found.add(s.upper())
    return found


def elapsed_market_fraction():
    # Il volume delayed_sip è circa 15 minuti indietro.
    now = datetime.now(NY) - timedelta(minutes=15)
    open_dt = datetime.combine(now.date(), dtime(9, 30), tzinfo=NY)
    close_dt = datetime.combine(now.date(), dtime(16, 0), tzinfo=NY)
    if now <= open_dt:
        return 0.0
    if now >= close_dt:
        return 1.0
    return (now - open_dt).total_seconds() / (close_dt - open_dt).total_seconds()


def analyze_candidate(mover, mcap_cache, news_set):
    symbol = str(mover.get("symbol", "")).upper().strip()
    if not symbol:
        return None

    try:
        pct = float(mover.get("percent_change"))
        price = float(mover.get("price"))
    except (TypeError, ValueError):
        return None

    if pct < MIN_CHANGE or price <= 0:
        return None

    market_cap = get_market_cap(symbol, mcap_cache)
    if market_cap is None:
        print(f"[SKIP] {symbol}: market cap non verificabile")
        return None
    if market_cap < MIN_MARKET_CAP:
        print(f"[SKIP] {symbol}: market cap ${market_cap/1e9:.2f}B")
        return None

    try:
        bars = get_daily_history(symbol)
    except Exception as exc:
        print(f"[SKIP] {symbol}: storico non disponibile: {exc}")
        return None

    if len(bars) < 20:
        print(f"[SKIP] {symbol}: meno di 20 sedute storiche")
        return None

    last20 = bars[-20:]
    vols = [float(b.get("v", 0) or 0) for b in last20]
    avg_vol20 = sum(vols) / len(vols) if vols else 0
    avg_dollar_vol = avg_vol20 * price

    if avg_dollar_vol < MIN_AVG_DOLLAR_VOLUME:
        print(
            f"[SKIP] {symbol}: avg dollar volume "
            f"${avg_dollar_vol/1e6:.1f}M"
        )
        return None

    prev_high = float(bars[-1]["h"])
    high20 = max(float(b["h"]) for b in last20)
    last252 = bars[-252:] if len(bars) >= 252 else bars
    high52 = max(float(b["h"]) for b in last252)

    delayed_vol = 0.0
    try:
        snap = get_delayed_snapshot(symbol)
        delayed_vol = float((snap.get("dailyBar") or {}).get("v", 0) or 0)
    except Exception as exc:
        print(f"[WARN] {symbol}: snapshot delayed SIP non disponibile: {exc}")

    frac = elapsed_market_fraction()
    rvol_pace = None
    if avg_vol20 > 0 and delayed_vol > 0 and frac > 0.03:
        rvol_pace = delayed_vol / (avg_vol20 * frac)

    breakout_prev = price >= prev_high * 1.002
    breakout20 = price >= high20 * 1.001
    breakout52 = price >= high52 * 0.999

    strong_volume = rvol_pace is not None and rvol_pace >= MIN_RVOL_PACE
    strong_move = (
        pct >= 7.0 and rvol_pace is not None and rvol_pace >= 1.20
    )
    prev_breakout_confirmed = (
        breakout_prev
        and pct >= 4.5
        and rvol_pace is not None
        and rvol_pace >= 1.40
    )
    news_momentum = (
        symbol in news_set
        and pct >= 5.0
        and rvol_pace is not None
        and rvol_pace >= 1.30
    )

    signal = (
        strong_volume
        or breakout20
        or breakout52
        or strong_move
        or prev_breakout_confirmed
        or news_momentum
    )

    return {
        "symbol": symbol,
        "price": price,
        "pct": pct,
        "market_cap": market_cap,
        "avg_dollar_vol": avg_dollar_vol,
        "rvol_pace": rvol_pace,
        "prev_high": prev_high,
        "high20": high20,
        "high52": high52,
        "breakout_prev": breakout_prev,
        "breakout20": breakout20,
        "breakout52": breakout52,
        "has_news": symbol in news_set,
        "signal": signal,
    }


def main():
    if os.getenv("TEST_TELEGRAM", "").lower() == "true":
        send_telegram("AddFiregate TMO")
        print("[OK] Test Telegram inviato: AddFiregate TMO")
        return 0

    now = datetime.now(NY)

    if not market_is_open():
        print("[INFO] Mercato USA chiuso. Nessuna scansione.")
        return 0

    # Aspetta che il delayed SIP abbia almeno un minimo di dati della seduta.
    if now.time() < dtime(9, 45):
        print("[INFO] Prima delle 09:45 ET: attendo dati volume delayed SIP.")
        return 0

    state = prune_alert_state(load_json(STATE_FILE, {}))
    today_key = now.date().isoformat()
    already_alerted = set(state.get(today_key, []))
    mcap_cache = load_json(MCAP_CACHE_FILE, {})

    try:
        movers = get_movers()
    except Exception as exc:
        print(f"[ERRORE] Impossibile recuperare i movers Alpaca: {exc}")
        print(
            "[NOTA] Se ricevi 403, il tuo account potrebbe non avere accesso "
            "allo screener movers; in quel caso useremo un fallback gratuito."
        )
        return 1

    candidates = []
    for m in movers:
        try:
            if float(m.get("percent_change", 0) or 0) >= MIN_CHANGE:
                candidates.append(m)
        except Exception:
            continue

    symbols = [str(m.get("symbol", "")).upper() for m in candidates if m.get("symbol")]
    news_set = recent_news_symbols(symbols)

    print(f"[INFO] Movers ricevuti: {len(movers)}; candidati >= {MIN_CHANGE}%: {len(candidates)}")

    new_alerts = []
    for mover in candidates:
        result = analyze_candidate(mover, mcap_cache, news_set)
        if not result:
            continue

        rv = result["rvol_pace"]
        rv_txt = f"{rv:.2f}x" if rv is not None else "n/d"
        print(
            f"[CHECK] {result['symbol']} "
            f"${result['price']:.2f} "
            f"{result['pct']:+.2f}% "
            f"RVOL-pace={rv_txt} "
            f"cap=${result['market_cap']/1e9:.1f}B "
            f"ADV$=${result['avg_dollar_vol']/1e6:.1f}M "
            f"B20={result['breakout20']} "
            f"B52={result['breakout52']} "
            f"news={result['has_news']} "
            f"=> signal={result['signal']}"
        )

        symbol = result["symbol"]
        if not result["signal"]:
            continue
        if symbol in already_alerted:
            print(f"[DUP] {symbol}: già segnalato oggi")
            continue

        try:
            send_telegram(f"AddFiregate {symbol}")
            print(f"[ALERT] AddFiregate {symbol}")
            already_alerted.add(symbol)
            new_alerts.append(symbol)
        except Exception as exc:
            print(f"[ERRORE] Telegram per {symbol}: {exc}")

    state[today_key] = sorted(already_alerted)
    save_json(STATE_FILE, state)
    save_json(MCAP_CACHE_FILE, mcap_cache)

    if not new_alerts:
        print("[INFO] Nessun nuovo segnale forte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
