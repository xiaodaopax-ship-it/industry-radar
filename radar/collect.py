"""Fetch everything and save one immutable raw snapshot under data/."""
import datetime as dt
import json
import os

from . import us, yh as Y
from .classify import loosest, memberships, sectors
from .net import Yahoo
from .paths import CFG, DATA


def log(msg):
    print(msg, flush=True)


def _num(q, k):
    v = q.get(k)
    return v.get("raw") if isinstance(v, dict) else v


def collect_us(yh, fx, excluded):
    P, rows = us.fundamentals()
    L = loosest()
    funnel = [{"step": "美国：有可比季度营收的 SEC 申报公司", "n": len(rows)}]
    s1 = [r for r in rows if r["rev_ttm"] and L["ttm_revenue_min"] <= r["rev_ttm"] <= L["ttm_revenue_max"]
          and r["rev_q_ly"] >= L["prior_quarter_revenue_min"] and r["yoy"] >= L["yoy_growth_min"]]
    funnel.append({"step": f"美国：规模与增速预筛（同比 ≥ {L['yoy_growth_min']}%）", "n": len(s1)})
    log(f"US: {len(rows)} filers, {len(s1)} after pre-screen; fetching submissions ...")
    s2 = []
    for r in s1:
        sub = us.submissions(r["cik"])
        ex = [e for e in sub["exchanges"] if e in CFG["screen"]["exchanges"]]
        if not (sub["sic"] and ex and sub["tickers"]):
            continue
        r.update({"sic": sub["sic"], "sic_desc": sub["sic_desc"], "exchange": ex[0],
                  "ticker": sub["tickers"][sub["exchanges"].index(ex[0])], "_form4": sub["form4"]})
        mem, exc = memberships(r)
        excluded += [{"ticker": r["ticker"], "name": r["name"], "code": f"SIC {r['sic']}", **e} for e in exc]
        if mem:
            r["mem"] = mem
            s2.append(r)
    funnel.append({"step": "美国：属于某个环节并满足门槛，在主要交易所上市", "n": len(s2)})
    log(f"US: {len(s2)} in sectors; fetching market data ...")
    s3 = []
    for i, r in enumerate(s2):
        r["mkt"] = Y.market(yh, r["ticker"], fx)
        mc = (r["mkt"] or {}).get("mcap")
        r["mem"] = [m for m in r["mem"] if not mc or mc <= m["mcap_max"]]
        if r["mem"]:
            s3.append(r)
        if (i + 1) % 50 == 0:
            log(f"  market {i + 1}/{len(s2)}")
    funnel.append({"step": "美国：市值 ≤ 环节上限", "n": len(s3)})
    since = (dt.date.today() - dt.timedelta(days=CFG["form4"]["lookback_days"])).isoformat()
    log(f"US: parsing Form 4 since {since} ...")
    us.insiders(s3, since, log)
    return P, s3, funnel, since


def collect_intl(yh, fx, excluded):
    out, funnel, seen = [], [], set()
    for s in sectors():
        cfg = s.get("intl")
        if not cfg:
            continue
        cap_max = max(m.get("screen", {}).get("market_cap_max", CFG["screen"]["market_cap_max"]) for m in s["segments"])
        for region in cfg["regions"]:
            n_quotes = n_size = 0
            for industry in cfg["industries"]:
                quotes = yh.screener(region, industry, CFG["intl"]["max_per_query"])
                n_quotes += len(quotes)
                for q in quotes:
                    sym = q.get("symbol")
                    mc = Y.usd(_num(q, "marketCap"), q.get("currency"), fx)
                    if not sym or sym in seen or not mc or not (CFG["screen"]["market_cap_min_intl"] <= mc <= cap_max):
                        continue
                    seen.add(sym)
                    n_size += 1
                    f = Y.fundamentals(yh, sym, fx)
                    if not f:
                        continue
                    r = {"market": region.upper(), "ticker": sym, "name": q.get("longName") or q.get("shortName") or sym,
                         "industry": industry, "exchange": q.get("fullExchangeName") or q.get("exchange"), **f}
                    mem, exc = memberships(r, s["id"])
                    excluded += [{"ticker": sym, "name": r["name"], "code": industry, **e} for e in exc]
                    if not mem:
                        continue
                    r["mkt"] = Y.market(yh, sym, fx)
                    mc2 = (r["mkt"] or {}).get("mcap") or mc
                    r["mem"] = [m for m in mem if mc2 <= m["mcap_max"]]
                    if r["mem"]:
                        r["insider"] = None  # no Form 4 equivalent collected for non-US markets
                        out.append(r)
            log(f"{s['id']}/{region}: {n_quotes} quotes, {n_size} in size band, {sum(1 for r in out if r['market'] == region.upper())} candidates")
            funnel.append({"step": f"{region.upper()}：Yahoo 行业股票池 → 市值区间 → 满足环节门槛", "n": sum(1 for r in out if r["market"] == region.upper()),
                           "detail": f"{n_quotes} → {n_size}"})
    return out, funnel


def collect_leaders(yh, fx):
    out = {}
    syms = sorted({t for s in sectors() for g in s["segments"] for t in g.get("leaders", [])})
    log(f"leaders: {len(syms)} symbols ...")
    for sym in syms:
        m = Y.market(yh, sym, fx)
        f = Y.fundamentals(yh, sym, fx) or {}
        c = yh.chart(sym, "1y")
        out[sym] = {"ticker": sym, "mkt": m, "fund": f, "px": (c or {}).get("px", [])}
    return out


def collect_benchmarks(yh):
    syms = {CFG["market_benchmark"]["symbol"]} | {s["benchmark"]["symbol"] for s in sectors()}
    return {s: (yh.chart(s, "1y") or {}).get("px", []) for s in sorted(syms)}


def run():
    yh = Yahoo()
    fx = Y.fx_rates(yh)
    log(f"fx (per USD): {fx}")
    excluded = []
    P, us_rows, funnel, since = collect_us(yh, fx, excluded)
    intl_rows, f2 = collect_intl(yh, fx, excluded)
    raw = {"version": 2, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "periods": P, "fx": fx,
           "funnel": funnel + f2, "candidates": us_rows + intl_rows, "excluded": excluded, "form4_since": since,
           "leaders": collect_leaders(yh, fx), "benchmarks": collect_benchmarks(yh)}
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"raw_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    log(f"raw snapshot saved: {path}")
    return raw
