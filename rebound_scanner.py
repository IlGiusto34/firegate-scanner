#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import os
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

DATA_BASE = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS = "https://news.google.com/rss/search"
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = Path("rebound_alerts_state.json")
META_CACHE = Path("rebound_meta_cache.json")

TOP_LOSERS = int(os.getenv("TOP_LOSERS", "50"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS_PER_RUN", "5"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "2000000000"))
MIN_AVG_DOLLAR_VOLUME = float(os.getenv("MIN_AVG_DOLLAR_VOLUME", "20000000"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "5"))
MIN_DROP_PCT = float(os.getenv("MIN_DROP_PCT", "3.0"))
MAX_DROP_PCT = float(os.getenv("MAX_DROP_PCT", "15.0"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "9.0"))
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "6"))
REALERT_SCORE_GAIN = float(os.getenv("REALERT_SCORE_GAIN", "2.0"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
TEST_TELEGRAM = os.getenv("TEST_TELEGRAM", "false").lower() == "true"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}
http = requests.Session()
http.headers.update(HEADERS)

POSITIVE_WORDS = {
    "upgrade", "upgraded", "buy", "outperform", "overweight", "reiterates",
    "raises target", "raised target", "contract", "award", "partnership",
    "approval", "approved", "beats", "beat estimates", "record", "growth",
    "guidance raised", "raises guidance", "buyback", "repurchase", "wins",
}
NEGATIVE_WORDS = {
    "downgrade", "underperform", "sell rating", "misses", "missed estimates",
    "warning", "slowing", "weak demand", "margin pressure", "probe",
    "investigation", "lawsuit",
}
HARD_VETO_WORDS = {
    "bankruptcy", "chapter 11", "fraud", "accounting concerns",
    "accounting practices", "short seller", "short-seller", "short report",
    "sec investigation", "subpoena", "guidance cut", "cuts guidance",
    "lowered guidance", "revenue forecast cut", "secondary offering",
    "public offering", "dilution", "dilutive", "going concern", "default",
    "restatement", "material weakness", "delisting", "fda rejection",
    "clinical hold",
}


def sf(x, default=None):
    try:
        v = float(str(x).replace(",", "").replace("%", "").strip())
        return v if math.isfinite(v) else default
    except Exception:
        return default


def req(url, params=None, retries=2, json_out=True):
    last = None
    for n in range(retries + 1):
        try:
            r = http.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.2 * (n + 1))
                continue
            r.raise_for_status()
            return r.json() if json_out else r.text
        except Exception as e:
            last = e
            if n < retries:
                time.sleep(0.7 * (n + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0


def median(xs):
    vals = [float(x) for x in xs if x is not None]
    return statistics.median(vals) if vals else 0.0


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": text},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:400]}")


def market_open():
    try:
        return bool(req(f"{PAPER_BASE}/v2/clock").get("is_open"))
    except Exception as e:
        print(f"[WARN] market clock: {e}")
        return False


def get_losers():
    data = req(
        f"{DATA_BASE}/v1beta1/screener/stocks/movers",
        params={"top": TOP_LOSERS},
    )
    return data.get("losers", []) if isinstance(data, dict) else []


def analyst_points(info, current_price):
    score = 0.0
    rec = str(info.get("recommendationKey") or "").lower()
    target = sf(info.get("targetMeanPrice"))
    analysts = sf(info.get("numberOfAnalystOpinions"), 0) or 0
    upside = pct(target, current_price) if target and current_price else None
    if rec in {"strong_buy", "strong buy"}:
        score += 1.75
    elif rec == "buy":
        score += 1.25
    elif rec == "hold":
        score += 0.25
    elif rec in {"underperform", "sell", "strong_sell", "strong sell"}:
        score -= 1.5
    if upside is not None:
        if upside >= 30:
            score += 1.25
        elif upside >= 15:
            score += 0.75
        elif upside <= -10:
            score -= 1.0
    if analysts >= 5:
        score += 0.25
    return score, rec, target, upside, int(analysts)


def get_meta(symbol, price, cache):
    today = datetime.now(NY).date()
    cached = cache.get(symbol)
    if cached:
        try:
            d = datetime.strptime(cached["updated"], "%Y-%m-%d").date()
            if (today - d).days <= 1:
                return cached
        except Exception:
            pass
    result = {
        "market_cap": None,
        "target": None,
        "target_upside": None,
        "recommendation": "",
        "analyst_count": 0,
        "analyst_score": 0.0,
        "updated": today.isoformat(),
    }
    try:
        t = yf.Ticker(symbol)
        try:
            mc = sf(t.fast_info["market_cap"])
            if mc:
                result["market_cap"] = mc
        except Exception:
            pass
        info = t.get_info()
        if result["market_cap"] is None:
            result["market_cap"] = sf(info.get("marketCap"))
        aps, rec, target, upside, n = analyst_points(info, price)
        result.update({
            "target": target,
            "target_upside": upside,
            "recommendation": rec,
            "analyst_count": n,
            "analyst_score": aps,
        })
    except Exception as e:
        print(f"[WARN] {symbol}: yfinance meta: {e}")
    cache[symbol] = result
    return result


def daily_bars(symbol):
    now = datetime.now(NY)
    return req(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        params={
            "timeframe": "1Day",
            "start": (now.date() - timedelta(days=40)).isoformat(),
            "end": (now.date() - timedelta(days=1)).isoformat(),
            "limit": 100,
            "adjustment": "split",
            "feed": "sip",
            "sort": "asc",
        },
    ).get("bars", [])


def intraday_bars(symbol):
    now = datetime.now(NY)
    start = datetime.combine(now.date(), dtime(9, 30), tzinfo=NY).astimezone(UTC)
    return req(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        params={
            "timeframe": "5Min",
            "start": start.isoformat(),
            "end": now.astimezone(UTC).isoformat(),
            "limit": 1000,
            "adjustment": "split",
            "feed": "iex",
            "sort": "asc",
        },
    ).get("bars", [])


def alpaca_news(symbol):
    start = (datetime.now(UTC) - timedelta(hours=18)).isoformat()
    try:
        d = req(
            f"{DATA_BASE}/v1beta1/news",
            params={"symbols": symbol, "start": start, "limit": 20, "sort": "desc"},
        )
        return [
            " ".join(str(x.get("headline") or "").split())
            for x in d.get("news", []) if x.get("headline")
        ]
    except Exception:
        return []


def gdelt_news(symbol):
    try:
        d = req(
            GDELT,
            params={
                "query": f'"{symbol}" stock',
                "mode": "ArtList",
                "maxrecords": 15,
                "format": "json",
                "timespan": "12h",
                "sort": "HybridRel",
            },
        )
        return [
            " ".join(str(x.get("title") or "").split())
            for x in d.get("articles", []) if x.get("title")
        ]
    except Exception:
        return []


def google_news(symbol):
    try:
        txt = req(
            GNEWS,
            params={"q": f'"{symbol}" stock when:1d', "hl": "en-US", "gl": "US", "ceid": "US:en"},
            json_out=False,
        )
        root = ET.fromstring(txt)
        return [
            " ".join((item.findtext("title") or "").split())
            for item in root.findall(".//item")[:15]
            if item.findtext("title")
        ]
    except Exception:
        return []


def news_score(titles):
    seen = set()
    score = 0.0
    hard_veto = False
    hits = []
    for title in titles:
        t = title.lower()
        if t in seen:
            continue
        seen.add(t)
        veto_terms = [w for w in HARD_VETO_WORDS if w in t]
        if veto_terms:
            hard_veto = True
            hits.append(("VETO", title))
            continue
        p = sum(1 for w in POSITIVE_WORDS if w in t)
        n = sum(1 for w in NEGATIVE_WORDS if w in t)
        if p:
            score += min(1.2, 0.45 * p)
            hits.append(("POS", title))
        if n:
            score -= min(1.2, 0.55 * n)
            hits.append(("NEG", title))
    return max(-2.5, min(2.5, score)), hard_veto, hits[:8]


def analyze(mover, meta_cache):
    symbol = str(mover.get("symbol") or "").upper().strip()
    price = sf(mover.get("price"))
    move = sf(mover.get("percent_change"))
    if not symbol or not price or move is None:
        return None
    drop = abs(move) if move < 0 else 0
    if drop < MIN_DROP_PCT or drop > MAX_DROP_PCT or price < MIN_PRICE:
        return None

    meta = get_meta(symbol, price, meta_cache)
    mc = sf(meta.get("market_cap"))
    if not mc or mc < MIN_MARKET_CAP:
        return None

    dbars = daily_bars(symbol)
    ibars = intraday_bars(symbol)
    if len(dbars) < 8 or len(ibars) < 4:
        return None

    recent = dbars[-20:]
    avg_vol20 = sum(float(b.get("v", 0) or 0) for b in recent) / len(recent)
    avg_dvol = avg_vol20 * price
    if avg_dvol < MIN_AVG_DOLLAR_VOLUME:
        return None

    prev_close = float(dbars[-1]["c"])
    close5 = float(dbars[-6]["c"]) if len(dbars) >= 6 else float(dbars[0]["c"])
    week_change = pct(prev_close, close5)

    lows = [float(b["l"]) for b in ibars]
    highs = [float(b["h"]) for b in ibars]
    closes = [float(b["c"]) for b in ibars]
    vols = [float(b["v"]) for b in ibars]

    day_low = min(lows)
    day_high = max(highs)
    recovery = pct(price, day_low)
    range_pos = (price - day_low) / (day_high - day_low) if day_high > day_low else 0.0
    ret5 = pct(closes[-1], closes[-2]) if len(closes) >= 2 else 0.0
    ret15 = pct(closes[-1], closes[-4]) if len(closes) >= 4 else 0.0
    ret30 = pct(closes[-1], closes[-7]) if len(closes) >= 7 else 0.0

    vbase = median(vols[-13:-1]) if len(vols) >= 13 else median(vols[:-1])
    last_vol_ratio = vols[-1] / vbase if vbase else 0.0

    pv = 0.0
    vv = 0.0
    for b in ibars:
        typical = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        v = float(b.get("v", 0) or 0)
        pv += typical * v
        vv += v
    vwap = pv / vv if vv else None
    above_vwap = bool(vwap and price > vwap)

    higher_low = False
    if len(ibars) >= 6:
        first = min(float(b["l"]) for b in ibars[-6:-3])
        second = min(float(b["l"]) for b in ibars[-3:])
        higher_low = second > first * 1.001

    intraday_volume = sum(vols)
    now = datetime.now(NY)
    open_dt = datetime.combine(now.date(), dtime(9, 30), tzinfo=NY)
    close_dt = datetime.combine(now.date(), dtime(16, 0), tzinfo=NY)
    elapsed = max(0.03, min(1.0, (now-open_dt).total_seconds()/(close_dt-open_dt).total_seconds()))
    rvol_pace = intraday_volume / (avg_vol20 * elapsed) if avg_vol20 else 0.0

    titles = alpaca_news(symbol) + gdelt_news(symbol) + google_news(symbol)
    nscore, veto, news_hits = news_score(titles)

    score = 0.0
    if 3 <= drop < 5:
        score += 0.5
    elif 5 <= drop <= 9:
        score += 1.25
    elif 9 < drop <= 12:
        score += 0.75
    elif drop > 12:
        score -= 1.0

    if recovery >= 0.8: score += 1.0
    if recovery >= 1.8: score += 1.0
    if range_pos >= 0.45: score += 0.75
    if range_pos >= 0.65: score += 0.75
    if ret5 > 0: score += 0.5
    if ret15 >= 0.4: score += 1.0
    if ret30 >= 0.8: score += 1.0
    if higher_low: score += 1.0
    if above_vwap: score += 1.0
    if last_vol_ratio >= 1.3: score += 0.5
    if last_vol_ratio >= 2.0: score += 0.75
    if rvol_pace >= 1.2: score += 0.5
    if rvol_pace >= 1.8: score += 0.75
    if week_change >= 2: score += 1.0
    elif week_change >= -2: score += 0.5
    elif week_change <= -10: score -= 1.0

    score += sf(meta.get("analyst_score"), 0) or 0
    score += nscore
    if veto: score -= 6.0

    no_turn = recovery < 0.6 and ret15 <= 0 and not higher_low
    if no_turn: score -= 3.0

    technical_turn = (
        recovery >= 1.0
        and (ret15 > 0 or higher_low)
        and (range_pos >= 0.35 or above_vwap)
    )
    signal = score >= MIN_SCORE and technical_turn and not veto

    return {
        "symbol": symbol,
        "price": price,
        "move": move,
        "score": score,
        "market_cap": mc,
        "avg_dvol": avg_dvol,
        "week_change": week_change,
        "recovery": recovery,
        "range_pos": range_pos,
        "ret5": ret5,
        "ret15": ret15,
        "ret30": ret30,
        "rvol_pace": rvol_pace,
        "last_vol_ratio": last_vol_ratio,
        "above_vwap": above_vwap,
        "higher_low": higher_low,
        "recommendation": meta.get("recommendation"),
        "target": meta.get("target"),
        "target_upside": meta.get("target_upside"),
        "news_score": nscore,
        "veto": veto,
        "news_hits": news_hits,
        "signal": signal,
    }


def prune_state(state):
    cutoff = datetime.now(UTC) - timedelta(days=3)
    out = {}
    for s, v in state.items():
        try:
            ts = datetime.fromisoformat(v["time"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= cutoff:
                out[s] = v
        except Exception:
            pass
    return out


def can_alert(a, state):
    old = state.get(a["symbol"])
    if not old:
        return True
    try:
        ts = datetime.fromisoformat(old["time"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except Exception:
        return True
    hours = (datetime.now(UTC) - ts).total_seconds() / 3600.0
    if hours < COOLDOWN_HOURS:
        return False
    return a["score"] >= (sf(old.get("score"), 0) or 0) + REALERT_SCORE_GAIN


def main():
    if TEST_TELEGRAM:
        send_telegram("AddFiregate AAPL")
        print("[OK] Telegram test: AddFiregate AAPL")
        return 0

    if not market_open():
        print("[INFO] US market closed.")
        return 0

    now = datetime.now(NY)
    if now.time() < dtime(9, 45) or now.time() > dtime(15, 45):
        print("[INFO] Outside rebound scan window 09:45-15:45 ET.")
        return 0

    state = prune_state(load_json(STATE_FILE, {}))
    meta_cache = load_json(META_CACHE, {})
    losers = get_losers()
    print(f"[INFO] Alpaca losers received: {len(losers)}")

    candidates = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(analyze, m, meta_cache) for m in losers]
        for fut in cf.as_completed(futures):
            try:
                x = fut.result()
                if x:
                    candidates.append(x)
            except Exception as e:
                print(f"[WARN] analysis: {e}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    for a in candidates:
        print(
            f"[CHECK] {a['symbol']:6} {a['move']:+.2f}% price=${a['price']:.2f} "
            f"score={a['score']:.2f} recover={a['recovery']:.2f}% "
            f"15m={a['ret15']:+.2f}% 30m={a['ret30']:+.2f}% "
            f"week={a['week_change']:+.2f}% RVOLpace={a['rvol_pace']:.2f}x "
            f"VWAP={a['above_vwap']} HL={a['higher_low']} "
            f"analyst={a['recommendation']} news={a['news_score']:+.2f} "
            f"veto={a['veto']} signal={a['signal']}"
        )

    signals = [a for a in candidates if a["signal"] and can_alert(a, state)][:MAX_ALERTS]
    if not signals:
        print("[INFO] No qualified rebound setup.")
        save_json(META_CACHE, meta_cache)
        save_json(STATE_FILE, state)
        return 0

    for a in signals:
        msg = f"AddFiregate {a['symbol']}"
        if DRY_RUN:
            print(f"[DRY RUN] {msg}")
            continue
        send_telegram(msg)
        print(f"[ALERT] {msg}")
        state[a["symbol"]] = {
            "time": datetime.now(UTC).isoformat(),
            "price": a["price"],
            "score": a["score"],
        }

    save_json(META_CACHE, meta_cache)
    save_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
