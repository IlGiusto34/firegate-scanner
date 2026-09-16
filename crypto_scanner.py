#!/usr/bin/env python3
# FireGate Crypto Scanner v1.1 - Coinbase pair compatibility fix
import concurrent.futures as cf
import json, math, os, statistics, sys, time, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CBX = "https://api.exchange.coinbase.com"
CBP = "https://api.coinbase.com/api/v3/brokerage/market"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS = "https://news.google.com/rss/search"
FNG_URL = "https://api.alternative.me/fng/"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

STATE = Path("crypto_alerts_state.json")
TOP_N = int(os.getenv("SCAN_TOP_LIQUID", "60"))
MIN_DVOL = float(os.getenv("MIN_DOLLAR_VOLUME_24H", "15000000"))
MIN_MCAP = float(os.getenv("MIN_MARKET_CAP_USD", "100000000"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD_PCT", "0.80"))
MIN_PRE = float(os.getenv("MIN_PRE_SCORE", "7.0"))
MIN_FINAL = float(os.getenv("MIN_FINAL_SCORE", "10.0"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS_PER_RUN", "3"))
COOLDOWN_H = float(os.getenv("COOLDOWN_HOURS", "6"))
REALERT_GAIN = float(os.getenv("REALERT_PRICE_GAIN_PCT", "3.0"))
REALERT_SCORE = float(os.getenv("REALERT_SCORE_IMPROVEMENT", "2.0"))
WORKERS = int(os.getenv("MAX_WORKERS", "5"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
TEST_TELEGRAM = os.getenv("TEST_TELEGRAM", "false").lower() == "true"

QUOTES = {"USD", "USDC", "USDT"}
STABLES = {
    "USD","USDC","USDT","DAI","USDS","PYUSD","EURC","EUR","GBP","GUSD",
    "TUSD","FDUSD","USDP","RLUSD","USDG"
}
LEV_SUFFIX = ("3L","3S","5L","5S","BULL","BEAR","UP","DOWN")

POS = {
    "approval","approved","partnership","integration","launch","launched",
    "listing","listed","adoption","upgrade","mainnet","etf","inflow","record",
    "surge","breakout","bullish","funding","investment","acquisition",
    "buyback","burn","staking","institutional","expansion","collaboration",
    "payments","treasury","reserve"
}
NEG = {
    "hack","hacked","exploit","lawsuit","investigation","delist","delisted",
    "outage","scam","fraud","vulnerability","ban","breach","attack","stolen",
    "liquidation","selloff","sell-off","unlock","dump","charges","bankruptcy",
    "insolvency","shutdown","suspension"
}
RUMOR = {
    "rumor","rumour","reportedly","talks","considering","speculation",
    "sources say","potential deal"
}

sess = requests.Session()
sess.headers.update({
    "User-Agent": "FireGateCryptoScanner/1.0",
    "Accept": "application/json,text/plain,*/*"
})

def sf(x, d=None):
    try:
        v = float(str(x).replace("%","").replace(",","").strip())
        return v if math.isfinite(v) else d
    except Exception:
        return d

def get(url, params=None, json_out=True, retries=2):
    last = None
    for n in range(retries + 1):
        try:
            r = sess.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (n + 1)); continue
            r.raise_for_status()
            return r.json() if json_out else r.text
        except Exception as e:
            last = e
            time.sleep(0.7 * (n + 1))
    raise RuntimeError(f"GET failed {url}: {last}")

def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0

def med(xs):
    xs = [float(x) for x in xs if x is not None]
    return statistics.median(xs) if xs else 0.0

def ema(xs, p):
    if not xs: return []
    k = 2 / (p + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(k*x + (1-k)*out[-1])
    return out

def rsi(xs, p=14):
    if len(xs) <= p: return 50.0
    gains, losses = [], []
    for i in range(-p, 0):
        d = xs[i] - xs[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag, al = sum(gains)/p, sum(losses)/p
    if al == 0: return 100.0
    return 100 - 100/(1 + ag/al)

def excluded(base):
    b = base.upper()
    return b in STABLES or any(b.endswith(x) for x in LEV_SUFFIX)

def load_state():
    try:
        return json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception:
        return {}

def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, sort_keys=True))

def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": text}, timeout=15
    )
    r.raise_for_status()

# ---------- Market universe ----------

def exchange_online_products():
    """
    Product IDs that are actually available on Coinbase Exchange REST.
    This prevents mixing Advanced Trade product IDs (for example a USDC pair)
    with Exchange candle endpoints that may not support that pair.
    """
    rows = get(f"{CBX}/products")
    out = {}
    for p in rows if isinstance(rows, list) else []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        if p.get("status") != "online":
            continue
        if p.get("trading_disabled", False):
            continue
        out[pid] = p
    return out

def rich_products():
    exchange_products = exchange_online_products()
    allowed_exchange_ids = set(exchange_products)
    print(f"[INFO] Coinbase Exchange online products: {len(allowed_exchange_ids)}")

    allp, cursor = [], None
    for _ in range(4):
        params = {
            "product_type": "SPOT",
            "products_sort_order": "PRODUCTS_SORT_ORDER_VOLUME_24H_DESCENDING",
            "limit": 250
        }
        if cursor: params["cursor"] = cursor
        d = get(f"{CBP}/products", params)
        rows = d.get("products", []) if isinstance(d, dict) else []
        if not rows: break
        allp += rows
        cursor = d.get("cursor")
        if not cursor: break

    out = {}
    for p in allp:
        base = str(p.get("base_currency_id") or p.get("base_display_symbol") or "").upper()
        quote = str(p.get("quote_currency_id") or "").upper()
        pid = str(p.get("product_id") or "")
        if not pid or not base or quote not in QUOTES or excluded(base):
            continue
        if pid not in allowed_exchange_ids:
            # Product exists in the broader Coinbase catalog but cannot be used
            # with api.exchange.coinbase.com candle/ticker endpoints.
            continue
        if p.get("trading_disabled") is True: continue

        price = sf(p.get("price"))
        qvol = sf(p.get("approximate_quote_24h_volume"))
        if qvol is None:
            qvol = (sf(p.get("volume_24h"),0) or 0) * (price or 0)
        if not price or not qvol: continue

        bid, ask = sf(p.get("best_bid_price")), sf(p.get("best_ask_price"))
        spread = None
        if bid and ask and bid > 0:
            spread = (ask-bid)/((ask+bid)/2)*100

        row = {
            "product_id": pid, "base": base, "quote": quote,
            "name": str(p.get("base_name") or base),
            "price": price,
            "pct24": sf(p.get("price_percentage_change_24h"),0) or 0,
            "dvol": qvol,
            "mcap": sf(p.get("market_cap")),
            "spread": spread,
        }
        quote_rank = {"USD": 3, "USDC": 2, "USDT": 1}
        prev = out.get(base)
        if (
            prev is None
            or quote_rank.get(row["quote"], 0) > quote_rank.get(prev["quote"], 0)
            or (
                quote_rank.get(row["quote"], 0) == quote_rank.get(prev["quote"], 0)
                and row["dvol"] > prev["dvol"]
            )
        ):
            out[base] = row
    return list(out.values())

def fallback_products():
    products = get(f"{CBX}/products")
    eligible = []
    for p in products:
        base, quote = str(p.get("base_currency","")).upper(), str(p.get("quote_currency","")).upper()
        if quote in QUOTES and p.get("status") == "online" and not excluded(base):
            eligible.append((p["id"], base, quote))

    def one(t):
        pid, base, quote = t
        try:
            s = get(f"{CBX}/products/{pid}/stats")
            price, op, vol = sf(s.get("last")), sf(s.get("open")), sf(s.get("volume"),0) or 0
            if not price: return None
            return {
                "product_id":pid, "base":base, "quote":quote, "name":base,
                "price":price, "pct24":pct(price,op) if op else 0,
                "dvol":vol*price, "mcap":None, "spread":None
            }
        except Exception:
            return None

    rows = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for x in ex.map(one, eligible):
            if x: rows.append(x)
    out = {}
    for x in rows:
        if x["base"] not in out or x["dvol"] > out[x["base"]]["dvol"]:
            out[x["base"]] = x
    return list(out.values())

def universe():
    try:
        x = rich_products()
        if len(x) >= 20:
            print(f"[INFO] Coinbase public catalog: {len(x)} assets")
            return x
    except Exception as e:
        print(f"[WARN] rich catalog unavailable: {e}")
    x = fallback_products()
    print(f"[INFO] Coinbase Exchange fallback: {len(x)} assets")
    return x

# ---------- Technical analysis ----------

def candles(pid):
    d = get(f"{CBX}/products/{urllib.parse.quote(pid)}/candles", {"granularity":300})
    out = []
    for x in d if isinstance(d,list) else []:
        if isinstance(x,list) and len(x) >= 6:
            try:
                out.append({
                    "t":int(x[0]), "lo":float(x[1]), "hi":float(x[2]),
                    "o":float(x[3]), "c":float(x[4]), "v":float(x[5])
                })
            except Exception: pass
    return sorted(out, key=lambda z:z["t"])

def spread_if_needed(a):
    if a["spread"] is not None: return a["spread"]
    try:
        t = get(f"{CBX}/products/{a['product_id']}/ticker")
        bid, ask = sf(t.get("bid")), sf(t.get("ask"))
        if bid and ask: return (ask-bid)/((ask+bid)/2)*100
    except Exception: pass
    return None

def tech(a):
    bars = candles(a["product_id"])
    ts = int(time.time())
    bars = [b for b in bars if b["t"] + 300 <= ts][-300:]
    if len(bars) < 60: return None

    cs = [b["c"] for b in bars]
    vs = [b["v"] for b in bars]
    last = bars[-1]
    price = a["price"]

    r5 = pct(last["c"], bars[-2]["c"])
    r15 = pct(last["c"], bars[-4]["c"]) if len(bars)>=4 else 0
    r1h = pct(last["c"], bars[-13]["c"]) if len(bars)>=13 else 0
    r4h = pct(last["c"], bars[-49]["c"]) if len(bars)>=49 else 0
    live = pct(price, last["c"])

    basevol = med(vs[-21:-1])
    vr = last["v"]/basevol if basevol else 0
    ranges = [b["hi"]-b["lo"] for b in bars[-21:-1]]
    rr = (last["hi"]-last["lo"])/med(ranges) if med(ranges) else 0

    p6 = bars[-73:-1] if len(bars)>=73 else bars[:-1]
    p24 = bars[-289:-1] if len(bars)>=289 else bars[:-1]
    h6 = max((b["hi"] for b in p6), default=0)
    h24 = max((b["hi"] for b in p24), default=0)
    b6 = bool(h6 and price >= h6*1.001)
    b24 = bool(h24 and price >= h24*1.001)

    e9, e21 = ema(cs,9), ema(cs,21)
    trend = bool(e9 and e21 and e9[-1] > e21[-1] and len(e21)>=4 and e21[-1] > e21[-4])
    rs = rsi(cs)
    sp = spread_if_needed(a)

    momentum = r15>=1.2 or r1h>=2.2 or (r5>=0.65 and live>=0.2)
    volume = vr>=1.6
    breakout = b6 or b24
    trendflag = trend and rs>=55
    confirms = sum([momentum, volume, breakout, trendflag])

    score = 0.0
    if r5>=0.65: score+=1
    if r5>=1.2: score+=1
    if r15>=1.2: score+=1.5
    if r15>=2.25: score+=1
    if r1h>=2.2: score+=1.5
    if r1h>=4: score+=1
    if r4h>=4: score+=1
    if vr>=1.6: score+=1.5
    if vr>=2.5: score+=1
    if vr>=4: score+=0.5
    if rr>=1.5: score+=0.75
    if b6: score+=1.5
    if b24: score+=2
    if trend: score+=1
    if 55<=rs<=78: score+=0.75
    elif rs>88: score-=2
    elif rs>82: score-=1
    if a["pct24"]>=4: score+=0.75
    if a["pct24"]>=8: score+=0.5
    if a["dvol"]>=50_000_000: score+=0.5
    if a["dvol"]>=200_000_000: score+=0.5
    if sp is not None:
        if sp<=0.2: score+=0.5
        elif sp>MAX_SPREAD: score-=3
    if a["pct24"]>=25 and rs>82: score-=2
    if r1h>=12 and rs>85: score-=2

    z = dict(a)
    z.update({
        "ret5":r5,"ret15":r15,"ret1h":r1h,"ret4h":r4h,
        "volratio":vr,"range_ratio":rr,"b6":b6,"b24":b24,
        "rsi":rs,"trend":trend,"spread":sp,"pre":score,"confirms":confirms
    })
    return z

# ---------- Free news / sentiment ----------

def fear_greed():
    try:
        d = get(FNG_URL, {"limit":1})
        x = d.get("data",[{}])[0]
        return {"value":int(x.get("value",50)), "label":str(x.get("value_classification","Neutral"))}
    except Exception:
        return {"value":50,"label":"Neutral"}

def title_score(title):
    t = " " + title.lower() + " "
    p = sum(w in t for w in POS)
    n = sum(w in t for w in NEG)
    rumor = any(w in t for w in RUMOR)
    s = min(2,p*0.6) - min(3,n*0.9)
    if rumor: s *= 0.45
    return s, rumor

def gdelt(a):
    q = f'("{a["name"]}" OR "{a["base"]} token" OR "{a["base"]} crypto") cryptocurrency'
    try:
        d = get(GDELT, {
            "query":q,"mode":"ArtList","maxrecords":20,"format":"json",
            "timespan":"6h","sort":"HybridRel"
        })
        return [{
            "title":" ".join(x.get("title","").split()),
            "domain":x.get("domain",""), "source":"GDELT"
        } for x in d.get("articles",[]) if x.get("title")]
    except Exception:
        return []

def gnews(a):
    q = f'"{a["name"]}" crypto OR "{a["base"]}" cryptocurrency when:6h'
    try:
        txt = get(GNEWS, {"q":q,"hl":"en-US","gl":"US","ceid":"US:en"}, json_out=False)
        root = ET.fromstring(txt)
        out=[]
        for x in root.findall(".//item")[:20]:
            title = " ".join((x.findtext("title") or "").split())
            src = x.find("source")
            domain = (src.text or "").strip() if src is not None else ""
            if title: out.append({"title":title,"domain":domain,"source":"GoogleNewsRSS"})
        return out
    except Exception:
        return []

def news(a):
    raw = gdelt(a) + gnews(a)
    uniq, seen = [], set()
    for x in raw:
        k=x["title"].lower()
        if k not in seen:
            seen.add(k); uniq.append(x)

    s=0; pos=0; neg=0; rumors=0; domains=set()
    for x in uniq[:30]:
        sc, ru = title_score(x["title"])
        s += sc; pos += sc>0.1; neg += sc<-0.1; rumors += ru
        if x["domain"]: domains.add(x["domain"].lower())
    s=max(-3,min(3,s))
    if pos>=2 and len(domains)>=2: s=min(3,s+0.75)
    if neg>=2: s=max(-3,s-0.75)
    return {"news_score":s,"news_count":len(uniq),"posnews":pos,"negnews":neg,"rumors":rumors}

def fng_adj(fng, a):
    v=fng["value"]; x=0
    if 45<=v<=75: x+=0.5
    elif v>=85:
        x-=0.75
        if a["rsi"]>80: x-=0.5
    elif v<=20: x-=0.5
    return x

def enrich(a, fng):
    n=news(a); adj=fng_adj(fng,a)
    final=a["pre"]+n["news_score"]+adj
    liquid=a["dvol"]>=MIN_DVOL
    spreadok=a["spread"] is None or a["spread"]<=MAX_SPREAD
    capok=a["mcap"] is None or a["mcap"]>=MIN_MCAP
    adverse=n["negnews"]>=2 and n["news_score"]<=-1.5 and a["pre"]<MIN_FINAL+3
    impulse=a["ret15"]>=1.2 or a["ret1h"]>=2.5 or a["b6"] or a["b24"]
    threshold=final>=MIN_FINAL or (a["pre"]>=MIN_FINAL+1.5 and n["negnews"]==0)
    signal=liquid and spreadok and capok and a["confirms"]>=2 and impulse and threshold and not adverse
    z=dict(a); z.update(n); z.update({"fng":fng,"adj":adj,"final":final,"signal":signal})
    return z

# ---------- Anti-spam ----------

def prune(s):
    cutoff=datetime.now(timezone.utc)-timedelta(days=14)
    out={}
    for k,v in s.items():
        try:
            t=datetime.fromisoformat(v["time"])
            if t.tzinfo is None: t=t.replace(tzinfo=timezone.utc)
            if t>=cutoff: out[k]=v
        except Exception: pass
    return out

def may_alert(a,s):
    old=s.get(a["base"])
    if not old: return True
    try:
        t=datetime.fromisoformat(old["time"])
        if t.tzinfo is None: t=t.replace(tzinfo=timezone.utc)
    except Exception: return True
    if (datetime.now(timezone.utc)-t).total_seconds()/3600 < COOLDOWN_H:
        return False
    oldp=sf(old.get("price"),0) or 0
    olds=sf(old.get("score"),0) or 0
    return (oldp and pct(a["price"],oldp)>=REALERT_GAIN) or a["final"]>=olds+REALERT_SCORE

def main():
    if TEST_TELEGRAM:
        telegram("AddFiregate BTC")
        print("[OK] AddFiregate BTC")
        return 0

    state=prune(load_state())
    fng=fear_greed()
    print(f"[INFO] Fear & Greed {fng['value']} {fng['label']}")

    u=[x for x in universe()
       if x["dvol"]>=MIN_DVOL and (x["mcap"] is None or x["mcap"]>=MIN_MCAP)]
    u=sorted(u,key=lambda x:x["dvol"],reverse=True)[:TOP_N]
    print(f"[INFO] Liquid universe: {len(u)}")

    def safe_tech(a):
        try:
            return tech(a)
        except Exception as e:
            print(
                f"[WARN] {a.get('base','?')} {a.get('product_id','?')}: "
                f"technical analysis skipped: {e}"
            )
            return None

    out=[]
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for z in ex.map(safe_tech, u):
            if z: out.append(z)

    for a in out:
        sp="n/d" if a["spread"] is None else f"{a['spread']:.3f}%"
        print(
            f"[CHECK] {a['base']:8} price={a['price']:.8g} 24h={a['pct24']:+.2f}% "
            f"5m={a['ret5']:+.2f}% 15m={a['ret15']:+.2f}% 1h={a['ret1h']:+.2f}% "
            f"RVOL5m={a['volratio']:.2f} B6={a['b6']} B24={a['b24']} "
            f"RSI={a['rsi']:.1f} spread={sp} pre={a['pre']:.2f}"
        )

    finalists=sorted(
        [a for a in out if a["pre"]>=MIN_PRE and a["confirms"]>=2],
        key=lambda x:x["pre"], reverse=True
    )[:12]

    enriched=[]
    for a in finalists:
        try:
            z=enrich(a,fng); enriched.append(z)
            print(
                f"[FINAL] {z['base']} pre={z['pre']:.2f} news={z['news_score']:+.2f} "
                f"score={z['final']:.2f} news={z['news_count']} rumors={z['rumors']} "
                f"signal={z['signal']}"
            )
        except Exception as e:
            print(f"[WARN] {a['base']} enrichment: {e}")

    signals=[a for a in enriched if a["signal"] and may_alert(a,state)]
    signals=sorted(signals,key=lambda x:(x["final"],x["dvol"]),reverse=True)[:MAX_ALERTS]

    sent=0
    for a in signals:
        msg=f"AddFiregate {a['base']}"
        if DRY_RUN:
            print(f"[DRY RUN] {msg}")
            continue
        telegram(msg)
        print(f"[ALERT] {msg}")
        state[a["base"]]={
            "time":datetime.now(timezone.utc).isoformat(),
            "price":a["price"],"score":a["final"],"product_id":a["product_id"]
        }
        sent+=1

    save_state(state)
    if not sent and not DRY_RUN:
        print("[INFO] No new high-conviction crypto setup.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
            r = sess.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (n + 1)); continue
            r.raise_for_status()
            return r.json() if json_out else r.text
        except Exception as e:
            last = e
            time.sleep(0.7 * (n + 1))
    raise RuntimeError(f"GET failed {url}: {last}")

def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0

def med(xs):
    xs = [float(x) for x in xs if x is not None]
    return statistics.median(xs) if xs else 0.0

def ema(xs, p):
    if not xs: return []
    k = 2 / (p + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(k*x + (1-k)*out[-1])
    return out

def rsi(xs, p=14):
    if len(xs) <= p: return 50.0
    gains, losses = [], []
    for i in range(-p, 0):
        d = xs[i] - xs[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag, al = sum(gains)/p, sum(losses)/p
    if al == 0: return 100.0
    return 100 - 100/(1 + ag/al)

def excluded(base):
    b = base.upper()
    return b in STABLES or any(b.endswith(x) for x in LEV_SUFFIX)

def load_state():
    try:
        return json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception:
        return {}

def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, sort_keys=True))

def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": text}, timeout=15
    )
    r.raise_for_status()

# ---------- Market universe ----------

def rich_products():
    allp, cursor = [], None
    for _ in range(4):
        params = {
            "product_type": "SPOT",
            "products_sort_order": "PRODUCTS_SORT_ORDER_VOLUME_24H_DESCENDING",
            "limit": 250
        }
        if cursor: params["cursor"] = cursor
        d = get(f"{CBP}/products", params)
        rows = d.get("products", []) if isinstance(d, dict) else []
        if not rows: break
        allp += rows
        cursor = d.get("cursor")
        if not cursor: break

    out = {}
    for p in allp:
        base = str(p.get("base_currency_id") or p.get("base_display_symbol") or "").upper()
        quote = str(p.get("quote_currency_id") or "").upper()
        pid = str(p.get("product_id") or "")
        if not pid or not base or quote not in QUOTES or excluded(base):
            continue
        if p.get("trading_disabled") is True: continue

        price = sf(p.get("price"))
        qvol = sf(p.get("approximate_quote_24h_volume"))
        if qvol is None:
            qvol = (sf(p.get("volume_24h"),0) or 0) * (price or 0)
        if not price or not qvol: continue

        bid, ask = sf(p.get("best_bid_price")), sf(p.get("best_ask_price"))
        spread = None
        if bid and ask and bid > 0:
            spread = (ask-bid)/((ask+bid)/2)*100

        row = {
            "product_id": pid, "base": base, "quote": quote,
            "name": str(p.get("base_name") or base),
            "price": price,
            "pct24": sf(p.get("price_percentage_change_24h"),0) or 0,
            "dvol": qvol,
            "mcap": sf(p.get("market_cap")),
            "spread": spread,
        }
        if base not in out or row["dvol"] > out[base]["dvol"]:
            out[base] = row
    return list(out.values())

def fallback_products():
    products = get(f"{CBX}/products")
    eligible = []
    for p in products:
        base, quote = str(p.get("base_currency","")).upper(), str(p.get("quote_currency","")).upper()
        if quote in QUOTES and p.get("status") == "online" and not excluded(base):
            eligible.append((p["id"], base, quote))

    def one(t):
        pid, base, quote = t
        try:
            s = get(f"{CBX}/products/{pid}/stats")
            price, op, vol = sf(s.get("last")), sf(s.get("open")), sf(s.get("volume"),0) or 0
            if not price: return None
            return {
                "product_id":pid, "base":base, "quote":quote, "name":base,
                "price":price, "pct24":pct(price,op) if op else 0,
                "dvol":vol*price, "mcap":None, "spread":None
            }
        except Exception:
            return None

    rows = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for x in ex.map(one, eligible):
            if x: rows.append(x)
    out = {}
    for x in rows:
        if x["base"] not in out or x["dvol"] > out[x["base"]]["dvol"]:
            out[x["base"]] = x
    return list(out.values())

def universe():
    try:
        x = rich_products()
        if len(x) >= 20:
            print(f"[INFO] Coinbase public catalog: {len(x)} assets")
            return x
    except Exception as e:
        print(f"[WARN] rich catalog unavailable: {e}")
    x = fallback_products()
    print(f"[INFO] Coinbase Exchange fallback: {len(x)} assets")
    return x

# ---------- Technical analysis ----------

def candles(pid):
    d = get(f"{CBX}/products/{urllib.parse.quote(pid)}/candles", {"granularity":300})
    out = []
    for x in d if isinstance(d,list) else []:
        if isinstance(x,list) and len(x) >= 6:
            try:
                out.append({
                    "t":int(x[0]), "lo":float(x[1]), "hi":float(x[2]),
                    "o":float(x[3]), "c":float(x[4]), "v":float(x[5])
                })
            except Exception: pass
    return sorted(out, key=lambda z:z["t"])

def spread_if_needed(a):
    if a["spread"] is not None: return a["spread"]
    try:
        t = get(f"{CBX}/products/{a['product_id']}/ticker")
        bid, ask = sf(t.get("bid")), sf(t.get("ask"))
        if bid and ask: return (ask-bid)/((ask+bid)/2)*100
    except Exception: pass
    return None

def tech(a):
    bars = candles(a["product_id"])
    ts = int(time.time())
    bars = [b for b in bars if b["t"] + 300 <= ts][-300:]
    if len(bars) < 60: return None

    cs = [b["c"] for b in bars]
    vs = [b["v"] for b in bars]
    last = bars[-1]
    price = a["price"]

    r5 = pct(last["c"], bars[-2]["c"])
    r15 = pct(last["c"], bars[-4]["c"]) if len(bars)>=4 else 0
    r1h = pct(last["c"], bars[-13]["c"]) if len(bars)>=13 else 0
    r4h = pct(last["c"], bars[-49]["c"]) if len(bars)>=49 else 0
    live = pct(price, last["c"])

    basevol = med(vs[-21:-1])
    vr = last["v"]/basevol if basevol else 0
    ranges = [b["hi"]-b["lo"] for b in bars[-21:-1]]
    rr = (last["hi"]-last["lo"])/med(ranges) if med(ranges) else 0

    p6 = bars[-73:-1] if len(bars)>=73 else bars[:-1]
    p24 = bars[-289:-1] if len(bars)>=289 else bars[:-1]
    h6 = max((b["hi"] for b in p6), default=0)
    h24 = max((b["hi"] for b in p24), default=0)
    b6 = bool(h6 and price >= h6*1.001)
    b24 = bool(h24 and price >= h24*1.001)

    e9, e21 = ema(cs,9), ema(cs,21)
    trend = bool(e9 and e21 and e9[-1] > e21[-1] and len(e21)>=4 and e21[-1] > e21[-4])
    rs = rsi(cs)
    sp = spread_if_needed(a)

    momentum = r15>=1.2 or r1h>=2.2 or (r5>=0.65 and live>=0.2)
    volume = vr>=1.6
    breakout = b6 or b24
    trendflag = trend and rs>=55
    confirms = sum([momentum, volume, breakout, trendflag])

    score = 0.0
    if r5>=0.65: score+=1
    if r5>=1.2: score+=1
    if r15>=1.2: score+=1.5
    if r15>=2.25: score+=1
    if r1h>=2.2: score+=1.5
    if r1h>=4: score+=1
    if r4h>=4: score+=1
    if vr>=1.6: score+=1.5
    if vr>=2.5: score+=1
    if vr>=4: score+=0.5
    if rr>=1.5: score+=0.75
    if b6: score+=1.5
    if b24: score+=2
    if trend: score+=1
    if 55<=rs<=78: score+=0.75
    elif rs>88: score-=2
    elif rs>82: score-=1
    if a["pct24"]>=4: score+=0.75
    if a["pct24"]>=8: score+=0.5
    if a["dvol"]>=50_000_000: score+=0.5
    if a["dvol"]>=200_000_000: score+=0.5
    if sp is not None:
        if sp<=0.2: score+=0.5
        elif sp>MAX_SPREAD: score-=3
    if a["pct24"]>=25 and rs>82: score-=2
    if r1h>=12 and rs>85: score-=2

    z = dict(a)
    z.update({
        "ret5":r5,"ret15":r15,"ret1h":r1h,"ret4h":r4h,
        "volratio":vr,"range_ratio":rr,"b6":b6,"b24":b24,
        "rsi":rs,"trend":trend,"spread":sp,"pre":score,"confirms":confirms
    })
    return z

# ---------- Free news / sentiment ----------

def fear_greed():
    try:
        d = get(FNG_URL, {"limit":1})
        x = d.get("data",[{}])[0]
        return {"value":int(x.get("value",50)), "label":str(x.get("value_classification","Neutral"))}
    except Exception:
        return {"value":50,"label":"Neutral"}

def title_score(title):
    t = " " + title.lower() + " "
    p = sum(w in t for w in POS)
    n = sum(w in t for w in NEG)
    rumor = any(w in t for w in RUMOR)
    s = min(2,p*0.6) - min(3,n*0.9)
    if rumor: s *= 0.45
    return s, rumor

def gdelt(a):
    q = f'("{a["name"]}" OR "{a["base"]} token" OR "{a["base"]} crypto") cryptocurrency'
    try:
        d = get(GDELT, {
            "query":q,"mode":"ArtList","maxrecords":20,"format":"json",
            "timespan":"6h","sort":"HybridRel"
        })
        return [{
            "title":" ".join(x.get("title","").split()),
            "domain":x.get("domain",""), "source":"GDELT"
        } for x in d.get("articles",[]) if x.get("title")]
    except Exception:
        return []

def gnews(a):
    q = f'"{a["name"]}" crypto OR "{a["base"]}" cryptocurrency when:6h'
    try:
        txt = get(GNEWS, {"q":q,"hl":"en-US","gl":"US","ceid":"US:en"}, json_out=False)
        root = ET.fromstring(txt)
        out=[]
        for x in root.findall(".//item")[:20]:
            title = " ".join((x.findtext("title") or "").split())
            src = x.find("source")
            domain = (src.text or "").strip() if src is not None else ""
            if title: out.append({"title":title,"domain":domain,"source":"GoogleNewsRSS"})
        return out
    except Exception:
        return []

def news(a):
    raw = gdelt(a) + gnews(a)
    uniq, seen = [], set()
    for x in raw:
        k=x["title"].lower()
        if k not in seen:
            seen.add(k); uniq.append(x)

    s=0; pos=0; neg=0; rumors=0; domains=set()
    for x in uniq[:30]:
        sc, ru = title_score(x["title"])
        s += sc; pos += sc>0.1; neg += sc<-0.1; rumors += ru
        if x["domain"]: domains.add(x["domain"].lower())
    s=max(-3,min(3,s))
    if pos>=2 and len(domains)>=2: s=min(3,s+0.75)
    if neg>=2: s=max(-3,s-0.75)
    return {"news_score":s,"news_count":len(uniq),"posnews":pos,"negnews":neg,"rumors":rumors}

def fng_adj(fng, a):
    v=fng["value"]; x=0
    if 45<=v<=75: x+=0.5
    elif v>=85:
        x-=0.75
        if a["rsi"]>80: x-=0.5
    elif v<=20: x-=0.5
    return x

def enrich(a, fng):
    n=news(a); adj=fng_adj(fng,a)
    final=a["pre"]+n["news_score"]+adj
    liquid=a["dvol"]>=MIN_DVOL
    spreadok=a["spread"] is None or a["spread"]<=MAX_SPREAD
    capok=a["mcap"] is None or a["mcap"]>=MIN_MCAP
    adverse=n["negnews"]>=2 and n["news_score"]<=-1.5 and a["pre"]<MIN_FINAL+3
    impulse=a["ret15"]>=1.2 or a["ret1h"]>=2.5 or a["b6"] or a["b24"]
    threshold=final>=MIN_FINAL or (a["pre"]>=MIN_FINAL+1.5 and n["negnews"]==0)
    signal=liquid and spreadok and capok and a["confirms"]>=2 and impulse and threshold and not adverse
    z=dict(a); z.update(n); z.update({"fng":fng,"adj":adj,"final":final,"signal":signal})
    return z

# ---------- Anti-spam ----------

def prune(s):
    cutoff=datetime.now(timezone.utc)-timedelta(days=14)
    out={}
    for k,v in s.items():
        try:
            t=datetime.fromisoformat(v["time"])
            if t.tzinfo is None: t=t.replace(tzinfo=timezone.utc)
            if t>=cutoff: out[k]=v
        except Exception: pass
    return out

def may_alert(a,s):
    old=s.get(a["base"])
    if not old: return True
    try:
        t=datetime.fromisoformat(old["time"])
        if t.tzinfo is None: t=t.replace(tzinfo=timezone.utc)
    except Exception: return True
    if (datetime.now(timezone.utc)-t).total_seconds()/3600 < COOLDOWN_H:
        return False
    oldp=sf(old.get("price"),0) or 0
    olds=sf(old.get("score"),0) or 0
    return (oldp and pct(a["price"],oldp)>=REALERT_GAIN) or a["final"]>=olds+REALERT_SCORE

def main():
    if TEST_TELEGRAM:
        telegram("AddFiregate BTC")
        print("[OK] AddFiregate BTC")
        return 0

    state=prune(load_state())
    fng=fear_greed()
    print(f"[INFO] Fear & Greed {fng['value']} {fng['label']}")

    u=[x for x in universe()
       if x["dvol"]>=MIN_DVOL and (x["mcap"] is None or x["mcap"]>=MIN_MCAP)]
    u=sorted(u,key=lambda x:x["dvol"],reverse=True)[:TOP_N]
    print(f"[INFO] Liquid universe: {len(u)}")

    out=[]
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for z in ex.map(lambda a: tech(a), u):
            if z: out.append(z)

    for a in out:
        sp="n/d" if a["spread"] is None else f"{a['spread']:.3f}%"
        print(
            f"[CHECK] {a['base']:8} price={a['price']:.8g} 24h={a['pct24']:+.2f}% "
            f"5m={a['ret5']:+.2f}% 15m={a['ret15']:+.2f}% 1h={a['ret1h']:+.2f}% "
            f"RVOL5m={a['volratio']:.2f} B6={a['b6']} B24={a['b24']} "
            f"RSI={a['rsi']:.1f} spread={sp} pre={a['pre']:.2f}"
        )

    finalists=sorted(
        [a for a in out if a["pre"]>=MIN_PRE and a["confirms"]>=2],
        key=lambda x:x["pre"], reverse=True
    )[:12]

    enriched=[]
    for a in finalists:
        try:
            z=enrich(a,fng); enriched.append(z)
            print(
                f"[FINAL] {z['base']} pre={z['pre']:.2f} news={z['news_score']:+.2f} "
                f"score={z['final']:.2f} news={z['news_count']} rumors={z['rumors']} "
                f"signal={z['signal']}"
            )
        except Exception as e:
            print(f"[WARN] {a['base']} enrichment: {e}")

    signals=[a for a in enriched if a["signal"] and may_alert(a,state)]
    signals=sorted(signals,key=lambda x:(x["final"],x["dvol"]),reverse=True)[:MAX_ALERTS]

    sent=0
    for a in signals:
        msg=f"AddFiregate {a['base']}"
        if DRY_RUN:
            print(f"[DRY RUN] {msg}")
            continue
        telegram(msg)
        print(f"[ALERT] {msg}")
        state[a["base"]]={
            "time":datetime.now(timezone.utc).isoformat(),
            "price":a["price"],"score":a["final"],"product_id":a["product_id"]
        }
        sent+=1

    save_state(state)
    if not sent and not DRY_RUN:
        print("[INFO] No new high-conviction crypto setup.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
