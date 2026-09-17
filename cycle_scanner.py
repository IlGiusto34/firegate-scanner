#!/usr/bin/env python3
"""
FireGate Cycle / Seasonality Scanner
------------------------------------
Finds up to five liquid US stocks that combine:
- historical cyclic/seasonal tendency to rise in the current setup
- similar-pullback rebound statistics
- current intraday confirmation
- volume / VWAP confirmation
- analyst estimates
- recent news sentiment

Output:
    AddFiregate TICKER

This is a probabilistic rules-based scanner, not a guarantee of future gains.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

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
PREFIX = os.getenv("TELEGRAM_PREFIX", "AddFiregate")

STATE_FILE = Path("cycle_alerts_state.json")
META_FILE = Path("cycle_meta_cache.json")

TOP_ACTIVE = int(os.getenv("TOP_ACTIVE", "80"))
MAX_ANALYZE = int(os.getenv("MAX_ANALYZE", "60"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS_PER_RUN", "5"))

MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "2000000000"))
MIN_AVG_DOLLAR_VOLUME = float(os.getenv("MIN_AVG_DOLLAR_VOLUME", "20000000"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "5"))
MIN_FINAL_SCORE = float(os.getenv("MIN_FINAL_SCORE", "10.0"))
MIN_CYCLE_SCORE = float(os.getenv("MIN_CYCLE_SCORE", "4.0"))

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

POS_WORDS = {
    "upgrade","upgraded","outperform","overweight","buy rating","raises target",
    "raised target","beats","beat estimates","guidance raised","raises guidance",
    "contract","award","partnership","approval","approved","record","growth",
    "expansion","buyback","repurchase","launch","strong demand","demand exceeds",
    "orders","backlog","deal","wins","won",
}
NEG_WORDS = {
    "downgrade","underperform","sell rating","misses","missed estimates",
    "guidance cut","cuts guidance","warning","weak demand","probe","investigation",
    "lawsuit","offering","dilution","short report","fraud","restatement",
}

def sf(x, default=None):
    try:
        v = float(str(x).replace(",","").replace("%","").strip())
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

def mean(xs):
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else 0.0

def positive_rate(xs):
    vals = [float(x) for x in xs if x is not None]
    return sum(v > 0 for v in vals) / len(vals) if vals else 0.0

def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
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
        print(f"[WARN] clock: {e}")
        return False

# ---------------- Universe ----------------

def most_active():
    d = req(
        f"{DATA_BASE}/v1beta1/screener/stocks/most-actives",
        params={"by":"volume","top":TOP_ACTIVE},
    )
    rows = d.get("most_actives", []) if isinstance(d, dict) else []
    if not rows:
        # Some responses use "mostActives".
        rows = d.get("mostActives", []) if isinstance(d, dict) else []
    return rows

# ---------------- Historical bars ----------------

def daily_bars(symbol):
    now = datetime.now(NY)
    start = (now.date() - timedelta(days=1200)).isoformat()
    end = (now.date() - timedelta(days=1)).isoformat()
    d = req(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        params={
            "timeframe":"1Day",
            "start":start,
            "end":end,
            "limit":10000,
            "adjustment":"split",
            "feed":"sip",
            "sort":"asc",
        },
    )
    return d.get("bars", [])

def intraday_bars(symbol):
    now = datetime.now(NY)
    start = datetime.combine(now.date(), dtime(9,30), tzinfo=NY).astimezone(UTC)
    d = req(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        params={
            "timeframe":"5Min",
            "start":start.isoformat(),
            "end":now.astimezone(UTC).isoformat(),
            "limit":1000,
            "adjustment":"split",
            "feed":"iex",
            "sort":"asc",
        },
    )
    return d.get("bars", [])

def snapshot(symbol):
    try:
        return req(
            f"{DATA_BASE}/v2/stocks/{symbol}/snapshot",
            params={"feed":"iex"},
        )
    except Exception:
        return {}

# ---------------- Cyclicity / seasonality ----------------

def daily_returns(bars):
    out=[]
    for i in range(1,len(bars)):
        prev=sf(bars[i-1].get("c"))
        cur=sf(bars[i].get("c"))
        if prev and cur:
            out.append({
                "ret":pct(cur,prev),
                "date":datetime.fromtimestamp(int(bars[i]["t"])/1e9 if int(bars[i]["t"])>10**12 else int(bars[i]["t"]), tz=UTC).astimezone(NY)
                    if isinstance(bars[i].get("t"), (int,float))
                    else None
            })
    return out

def bar_date(bar):
    t=bar.get("t")
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t.replace("Z","+00:00")).astimezone(NY)
        except Exception:
            return None
    try:
        tv=float(t)
        if tv>10**15: tv/=1e9
        elif tv>10**12: tv/=1e3
        return datetime.fromtimestamp(tv,tz=UTC).astimezone(NY)
    except Exception:
        return None

def cycle_metrics(bars):
    if len(bars) < 180:
        return None

    now=datetime.now(NY)
    closes=[float(b["c"]) for b in bars]
    rets=[pct(closes[i],closes[i-1]) for i in range(1,len(closes))]

    # Today's calendar seasonality based on historical completed days.
    weekday_samples=[]
    month_samples=[]
    for i in range(1,len(bars)):
        dt=bar_date(bars[i])
        if not dt:
            continue
        r=pct(closes[i],closes[i-1])
        if dt.weekday()==now.weekday():
            weekday_samples.append(r)
        if dt.month==now.month:
            month_samples.append(r)

    # Current pullback state from completed daily bars.
    r1=rets[-1]
    r3=pct(closes[-1],closes[-4]) if len(closes)>=4 else 0.0
    r5=pct(closes[-1],closes[-6]) if len(closes)>=6 else 0.0

    # Historical next-day returns after similar 1d / 3d / 5d pullbacks.
    next_after_1=[]
    next_after_3=[]
    next_after_5=[]

    for i in range(6,len(closes)-1):
        past1=pct(closes[i],closes[i-1])
        past3=pct(closes[i],closes[i-3])
        past5=pct(closes[i],closes[i-5])
        nxt=pct(closes[i+1],closes[i])

        if abs(past1-r1) <= 1.25:
            next_after_1.append(nxt)
        if abs(past3-r3) <= 2.0:
            next_after_3.append(nxt)
        if abs(past5-r5) <= 2.5:
            next_after_5.append(nxt)

    # Mean-reversion after red streaks.
    red_streak=0
    for r in reversed(rets):
        if r < 0:
            red_streak += 1
        else:
            break

    streak_next=[]
    if red_streak>0:
        for i in range(3,len(closes)-1):
            streak=0
            j=i
            while j>0 and pct(closes[j],closes[j-1])<0 and streak<5:
                streak+=1
                j-=1
            if streak==min(red_streak,5):
                streak_next.append(pct(closes[i+1],closes[i]))

    metrics={
        "weekday_avg":mean(weekday_samples[-160:]),
        "weekday_pos":positive_rate(weekday_samples[-160:]),
        "month_avg":mean(month_samples[-180:]),
        "month_pos":positive_rate(month_samples[-180:]),
        "sim1_avg":mean(next_after_1[-120:]),
        "sim1_pos":positive_rate(next_after_1[-120:]),
        "sim3_avg":mean(next_after_3[-120:]),
        "sim3_pos":positive_rate(next_after_3[-120:]),
        "sim5_avg":mean(next_after_5[-120:]),
        "sim5_pos":positive_rate(next_after_5[-120:]),
        "streak_avg":mean(streak_next[-80:]),
        "streak_pos":positive_rate(streak_next[-80:]),
        "red_streak":red_streak,
        "r1":r1,
        "r3":r3,
        "r5":r5,
    }

    score=0.0
    # Day-of-week seasonality.
    if metrics["weekday_pos"]>=0.56 and metrics["weekday_avg"]>0:
        score+=1.0
    if metrics["weekday_pos"]>=0.60 and metrics["weekday_avg"]>=0.20:
        score+=0.5

    # Month seasonality.
    if metrics["month_pos"]>=0.54 and metrics["month_avg"]>0:
        score+=0.75
    if metrics["month_pos"]>=0.58 and metrics["month_avg"]>=0.18:
        score+=0.5

    # Similar-pattern next-day rebound stats.
    for avg_key,pos_key in [
        ("sim1_avg","sim1_pos"),
        ("sim3_avg","sim3_pos"),
        ("sim5_avg","sim5_pos"),
    ]:
        if metrics[pos_key]>=0.56 and metrics[avg_key]>0:
            score+=0.75
        if metrics[pos_key]>=0.62 and metrics[avg_key]>=0.35:
            score+=0.5

    # Red-streak mean reversion.
    if red_streak>=1 and metrics["streak_pos"]>=0.58 and metrics["streak_avg"]>0:
        score+=0.75
    if red_streak>=2 and metrics["streak_pos"]>=0.62:
        score+=0.5

    metrics["cycle_score"]=score
    return metrics

# ---------------- Analyst / market cap ----------------

def get_meta(symbol, price, cache):
    today=datetime.now(NY).date()
    c=cache.get(symbol)
    if c:
        try:
            if datetime.strptime(c["updated"],"%Y-%m-%d").date()==today:
                return c
        except Exception:
            pass

    out={
        "market_cap":None,
        "recommendation":"",
        "target":None,
        "target_upside":None,
        "analyst_count":0,
        "analyst_score":0.0,
        "updated":today.isoformat(),
    }

    try:
        t=yf.Ticker(symbol)
        try:
            out["market_cap"]=sf(t.fast_info["market_cap"])
        except Exception:
            pass
        info=t.get_info()
        if not out["market_cap"]:
            out["market_cap"]=sf(info.get("marketCap"))
        rec=str(info.get("recommendationKey") or "").lower()
        target=sf(info.get("targetMeanPrice"))
        count=int(sf(info.get("numberOfAnalystOpinions"),0) or 0)
        upside=pct(target,price) if target and price else None
        score=0.0
        if rec in {"strong_buy","strong buy"}: score+=1.5
        elif rec=="buy": score+=1.0
        elif rec=="hold": score+=0.2
        elif rec in {"underperform","sell","strong_sell","strong sell"}: score-=1.5
        if upside is not None:
            if upside>=25: score+=1.0
            elif upside>=12: score+=0.5
            elif upside<=-10: score-=1.0
        if count>=5: score+=0.25
        out.update({
            "recommendation":rec,
            "target":target,
            "target_upside":upside,
            "analyst_count":count,
            "analyst_score":score,
        })
    except Exception as e:
        print(f"[WARN] {symbol}: meta {e}")

    cache[symbol]=out
    return out

# ---------------- News sentiment ----------------

def alpaca_news(symbol):
    start=(datetime.now(UTC)-timedelta(hours=18)).isoformat()
    try:
        d=req(
            f"{DATA_BASE}/v1beta1/news",
            params={"symbols":symbol,"start":start,"limit":20,"sort":"desc"},
        )
        return [
            " ".join(str(x.get("headline") or "").split())
            for x in d.get("news",[]) if x.get("headline")
        ]
    except Exception:
        return []

def gdelt_news(symbol):
    try:
        d=req(
            GDELT,
            params={
                "query":f'"{symbol}" stock',
                "mode":"ArtList","maxrecords":12,"format":"json",
                "timespan":"12h","sort":"HybridRel",
            },
        )
        return [
            " ".join(str(x.get("title") or "").split())
            for x in d.get("articles",[]) if x.get("title")
        ]
    except Exception:
        return []

def google_news(symbol):
    try:
        txt=req(
            GNEWS,
            params={"q":f'"{symbol}" stock when:1d',"hl":"en-US","gl":"US","ceid":"US:en"},
            json_out=False,
        )
        root=ET.fromstring(txt)
        return [
            " ".join((x.findtext("title") or "").split())
            for x in root.findall(".//item")[:12]
            if x.findtext("title")
        ]
    except Exception:
        return []

def news_sentiment(symbol):
    titles=alpaca_news(symbol)+gdelt_news(symbol)+google_news(symbol)
    seen=set()
    score=0.0
    pos=neg=0
    for title in titles:
        key=title.lower()
        if key in seen: continue
        seen.add(key)
        p=sum(w in key for w in POS_WORDS)
        n=sum(w in key for w in NEG_WORDS)
        if p:
            pos+=1
            score+=min(0.8,0.3*p)
        if n:
            neg+=1
            score-=min(1.0,0.4*n)
    return max(-2.0,min(2.0,score)),pos,neg

# ---------------- Intraday confirmation ----------------

def intraday_metrics(bars, price):
    if len(bars)<7:
        return None

    cs=[float(b["c"]) for b in bars]
    hs=[float(b["h"]) for b in bars]
    ls=[float(b["l"]) for b in bars]
    vs=[float(b.get("v",0) or 0) for b in bars]

    ret5=pct(cs[-1],cs[-2])
    ret15=pct(cs[-1],cs[-4])
    ret30=pct(cs[-1],cs[-7])
    low=min(ls)
    high=max(hs)
    range_pos=(price-low)/(high-low) if high>low else 0.0

    pv=vv=0.0
    for b in bars:
        typ=(float(b["h"])+float(b["l"])+float(b["c"]))/3
        v=float(b.get("v",0) or 0)
        pv+=typ*v
        vv+=v
    vwap=pv/vv if vv else None

    vbase=median(vs[-13:-1]) if len(vs)>=13 else median(vs[:-1])
    last_vr=vs[-1]/vbase if vbase else 0.0

    higher_low=False
    if len(bars)>=6:
        l1=min(float(b["l"]) for b in bars[-6:-3])
        l2=min(float(b["l"]) for b in bars[-3:])
        higher_low=l2>l1*1.0005

    return {
        "ret5":ret5,
        "ret15":ret15,
        "ret30":ret30,
        "range_pos":range_pos,
        "vwap":vwap,
        "above_vwap":bool(vwap and price>vwap),
        "last_vr":last_vr,
        "higher_low":higher_low,
    }

# ---------------- Full analysis ----------------

def analyze(active_row, meta_cache):
    symbol=str(active_row.get("symbol") or "").upper().strip()
    if not symbol:
        return None

    try:
        dbars=daily_bars(symbol)
        if len(dbars)<180:
            return None

        last_close=float(dbars[-1]["c"])
        snap=snapshot(symbol)
        latest=(snap.get("latestTrade") or {})
        minute=(snap.get("minuteBar") or {})
        price=sf(latest.get("p")) or sf(minute.get("c")) or last_close
        if price<MIN_PRICE:
            return None

        meta=get_meta(symbol,price,meta_cache)
        mc=sf(meta.get("market_cap"))
        if not mc or mc<MIN_MARKET_CAP:
            return None

        recent=dbars[-20:]
        avg_vol=sum(float(b.get("v",0) or 0) for b in recent)/len(recent)
        avg_dvol=avg_vol*price
        if avg_dvol<MIN_AVG_DOLLAR_VOLUME:
            return None

        cyc=cycle_metrics(dbars)
        if not cyc or cyc["cycle_score"]<MIN_CYCLE_SCORE:
            return None

        ibars=intraday_bars(symbol)
        intr=intraday_metrics(ibars,price)
        if not intr:
            return None

        # Current daily move vs previous completed close.
        day_change=pct(price,last_close)

        # Intraday volume pace.
        ivol=sum(float(b.get("v",0) or 0) for b in ibars)
        now=datetime.now(NY)
        open_dt=datetime.combine(now.date(),dtime(9,30),tzinfo=NY)
        close_dt=datetime.combine(now.date(),dtime(16,0),tzinfo=NY)
        frac=max(0.03,min(1.0,(now-open_dt).total_seconds()/(close_dt-open_dt).total_seconds()))
        rvol_pace=ivol/(avg_vol*frac) if avg_vol else 0.0

        # News is used only after the statistically cyclical filter.
        nscore,npos,nneg=news_sentiment(symbol)

        score=cyc["cycle_score"] + sf(meta.get("analyst_score"),0) + nscore

        # Current confirmation.
        if day_change>0: score+=0.75
        if 0.2<=day_change<=5.5: score+=0.5
        if intr["ret15"]>=0.25: score+=1.0
        if intr["ret30"]>=0.5: score+=1.0
        if intr["above_vwap"]: score+=1.0
        if intr["higher_low"]: score+=0.75
        if intr["range_pos"]>=0.55: score+=0.5
        if intr["last_vr"]>=1.4: score+=0.5
        if rvol_pace>=1.1: score+=0.5
        if rvol_pace>=1.6: score+=0.5

        # Penalize if today's expected rise is not materializing.
        if intr["ret15"]<0 and intr["ret30"]<0:
            score-=1.5
        if day_change<=-2:
            score-=1.0
        if intr["range_pos"]<0.25:
            score-=1.0
        if nneg>=2 and nscore<0:
            score-=1.0

        # Do not chase already vertical moves.
        if day_change>=8:
            score-=1.0
        if day_change>=12:
            score-=2.0

        confirmation=(
            intr["ret15"]>0
            and (intr["above_vwap"] or intr["higher_low"])
            and intr["range_pos"]>=0.35
        )

        signal=score>=MIN_FINAL_SCORE and confirmation

        return {
            "symbol":symbol,
            "price":price,
            "day_change":day_change,
            "score":score,
            "cycle_score":cyc["cycle_score"],
            "weekday_pos":cyc["weekday_pos"],
            "weekday_avg":cyc["weekday_avg"],
            "month_pos":cyc["month_pos"],
            "month_avg":cyc["month_avg"],
            "sim1_pos":cyc["sim1_pos"],
            "sim1_avg":cyc["sim1_avg"],
            "sim3_pos":cyc["sim3_pos"],
            "sim3_avg":cyc["sim3_avg"],
            "sim5_pos":cyc["sim5_pos"],
            "sim5_avg":cyc["sim5_avg"],
            "red_streak":cyc["red_streak"],
            "week_change":cyc["r5"],
            "ret15":intr["ret15"],
            "ret30":intr["ret30"],
            "above_vwap":intr["above_vwap"],
            "higher_low":intr["higher_low"],
            "range_pos":intr["range_pos"],
            "rvol_pace":rvol_pace,
            "market_cap":mc,
            "avg_dvol":avg_dvol,
            "analyst":meta.get("recommendation"),
            "target_upside":meta.get("target_upside"),
            "news_score":nscore,
            "signal":signal,
        }

    except Exception as e:
        print(f"[WARN] {symbol}: {e}")
        return None

# ---------------- Anti-spam ----------------

def prune_state(state):
    cutoff=datetime.now(UTC)-timedelta(days=3)
    out={}
    for k,v in state.items():
        try:
            t=datetime.fromisoformat(v["time"])
            if t.tzinfo is None: t=t.replace(tzinfo=UTC)
            if t>=cutoff: out[k]=v
        except Exception:
            pass
    return out

def can_alert(a,state):
    old=state.get(a["symbol"])
    if not old:
        return True
    try:
        t=datetime.fromisoformat(old["time"])
        if t.tzinfo is None: t=t.replace(tzinfo=UTC)
    except Exception:
        return True
    if (datetime.now(UTC)-t).total_seconds()/3600<COOLDOWN_HOURS:
        return False
    return a["score"] >= (sf(old.get("score"),0) or 0)+REALERT_SCORE_GAIN

# ---------------- Main ----------------

def main():
    if TEST_TELEGRAM:
        send_telegram(f"{PREFIX} AMD")
        print(f"[OK] {PREFIX} AMD")
        return 0

    if not market_open():
        print("[INFO] US market closed.")
        return 0

    now=datetime.now(NY)
    if now.time()<dtime(9,45) or now.time()>dtime(15,45):
        print("[INFO] Outside 09:45-15:45 ET scan window.")
        return 0

    state=prune_state(load_json(STATE_FILE,{}))
    meta_cache=load_json(META_FILE,{})

    active=most_active()
    print(f"[INFO] Most-active received: {len(active)}")
    active=active[:MAX_ANALYZE]

    results=[]
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs=[ex.submit(analyze,row,meta_cache) for row in active]
        for fut in cf.as_completed(futs):
            x=fut.result()
            if x:
                results.append(x)

    results.sort(key=lambda x:x["score"],reverse=True)

    for a in results:
        print(
            f"[CHECK] {a['symbol']:6} score={a['score']:.2f} "
            f"cycle={a['cycle_score']:.2f} day={a['day_change']:+.2f}% "
            f"15m={a['ret15']:+.2f}% 30m={a['ret30']:+.2f}% "
            f"week={a['week_change']:+.2f}% RVOL={a['rvol_pace']:.2f}x "
            f"weekday={a['weekday_pos']:.0%}/{a['weekday_avg']:+.2f}% "
            f"month={a['month_pos']:.0%}/{a['month_avg']:+.2f}% "
            f"sim3={a['sim3_pos']:.0%}/{a['sim3_avg']:+.2f}% "
            f"VWAP={a['above_vwap']} HL={a['higher_low']} "
            f"analyst={a['analyst']} news={a['news_score']:+.2f} "
            f"signal={a['signal']}"
        )

    signals=[a for a in results if a["signal"] and can_alert(a,state)][:MAX_ALERTS]

    if not signals:
        print("[INFO] No qualified cyclical-upside setup.")
        save_json(META_FILE,meta_cache)
        save_json(STATE_FILE,state)
        return 0

    for a in signals:
        msg=f"{PREFIX} {a['symbol']}"
        if DRY_RUN:
            print(f"[DRY RUN] {msg}")
            continue
        send_telegram(msg)
        print(f"[ALERT] {msg}")
        state[a["symbol"]]={
            "time":datetime.now(UTC).isoformat(),
            "price":a["price"],
            "score":a["score"],
        }

    save_json(META_FILE,meta_cache)
    save_json(STATE_FILE,state)
    return 0

if __name__=="__main__":
    sys.exit(main())
