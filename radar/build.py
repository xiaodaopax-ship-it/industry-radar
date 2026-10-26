"""Turn a raw snapshot into the dashboard data model and render site/index.html."""
import datetime as dt
import glob
import json
import os
import statistics as st

from . import heat, research, valuation
from . import score as S
from .yh import first_sentence
from .classify import memberships, no_peg, profile, sectors, view
from .paths import CFG, DATA, ROOT, SITE, TEMPLATES, load


MARKET = {"TW": "TW", "TWO": "TW", "T": "JP", "KS": "KR", "KQ": "KR", "HK": "HK", "AS": "NL", "PA": "FR", "DE": "DE", "L": "UK"}


def latest_raw():
    for f in sorted(glob.glob(os.path.join(DATA, "raw_*.json")), key=os.path.getmtime, reverse=True):
        with open(f, encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("version") == 2:
            return raw
    raise SystemExit("no compatible raw snapshot in data/; run `python -m radar fetch` first")


def _opt(name):
    try:
        return {k: v for k, v in load(name).items() if not k.startswith("_") and k != "EXAMPLE"}
    except FileNotFoundError:
        return {}


def _ret(px, days=None):
    if len(px) < 2:
        return None
    a = px[0][1] if days is None else next((p[1] for p in px if p[0] >= (dt.date.fromisoformat(px[-1][0]) - dt.timedelta(days=days)).isoformat()), px[0][1])
    return round((px[-1][1] / a - 1) * 100, 1)


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(st.median(vals), 1) if vals else None


def _oneliner(ticker, m, notes, zh):
    if notes.get(ticker, {}).get("oneliner"):
        return notes[ticker]["oneliner"], "note"
    if ticker in zh:
        return zh[ticker], "zh"
    # snapshots taken before first_sentence() existed still hold the long text; normalise here too
    return first_sentence(((m or {}).get("profile") or {}).get("summary") or ""), "en"


DIMS = ["business", "customers", "management", "industry", "product", "risks"]


def _qual(rs):
    """Summarise the six qualitative ratings into one label (counts only, no weighting)."""
    if not rs:
        return {"label": "待研究", "pos": 0, "neg": 0, "unknown": len(DIMS)}
    rt = [((rs.get("dimensions") or {}).get(k) or {}).get("rating", "unknown") for k in DIMS]
    pos, neg, unk = rt.count("positive"), rt.count("negative"), rt.count("unknown")
    label = ("资料不足" if unk >= 3 else "偏正面" if pos - neg >= 2 else "偏负面" if neg - pos >= 2 else "中性")
    return {"label": label, "pos": pos, "neg": neg, "unknown": unk}


def _valuation(tickers):
    """Compact per-ticker valuation inputs + run metadata for the template (D.val). None when no valuation file exists."""
    V = valuation.latest()
    if not V:
        return None
    by = {}
    for t in sorted(tickers):
        v = V["by"].get(t)
        if not v:
            continue
        fy = v.get("fy") or []
        last = fy[-1] if fy else {}
        tt = v.get("ttm")
        by[t] = {"src": v.get("src"), "ccy": v.get("ccy"), "why": v.get("why"), "note": v.get("note"),
                 "fy": [[r["end"], r["rev"], r["ocf"], r["capex"]] for r in fy],
                 "tags": [last.get("ocf_tag"), last.get("capex_tag"), last.get("rev_tag")] if last else None,
                 "ttm": [tt["end"], tt["rev"], tt["ocf"], tt["capex"], tt.get("how")] if tt else None,
                 "rev_ttm": v.get("rev_ttm"), "rev_ttm_end": v.get("rev_ttm_end"), "ttm_yoy": v.get("ttm_yoy"), "cagr3": v.get("cagr3"),
                 "bs": v.get("bs"), "sh": v.get("shares"), "sh_dil": v.get("shares_dil"), "no_capex": v.get("no_capex_tag"),
                 "cons": v.get("cons"), "urls": v.get("urls"), "fetched": (v.get("fetched") or "")[:10]}
    return {"fetched_at": V["fetched_at"], "rf": V.get("rf"), "defaults": CFG.get("valuation", {}).get("defaults"), "by": by}


def build(raw):
    secs, notes, zh = sectors(), _opt("notes.json"), _opt("profiles_zh.json")
    deep = CFG.get("deep_dives", {})
    bench = raw["benchmarks"]

    # ---- emerging entries: one per (company, sector) membership
    entries = []
    # a hand-picked leader is shown in the leader layer of its sector, never also as an emerging candidate there
    lead_by = {s["id"]: {t for g in s["segments"] for t in g.get("leaders", [])} for s in secs}
    for r in raw["candidates"]:
        mem, _ = memberships(r)
        mc = (r.get("mkt") or {}).get("mcap")
        mem = [m for m in mem if (not mc or mc <= m["mcap_max"]) and r["ticker"] not in lead_by[m["sector"]]]
        for m in mem:
            rv = view(r, m["profile"])  # the financial profile reads revenue from its own tag list
            d = S.derived(rv, m)
            bull, bear = S.bull_bear(rv, d, m["cyclical"], m["segment"])
            entries.append({"_r": rv, "d": d, "kind": "emerging", "sector": m["sector"], "segment": m["segment"], "override": m["override"],
                            "cyclical": m["cyclical"], "profile": m["profile"], "checks": S.checklist(rv, d, m["cyclical"]), "bull": bull, "bear": bear,
                            "verify": S.verify(rv, d, m["cyclical"], m["segment"]),
                            "gate": S.gate(rv, d, m), "factors": {}})
    for s in secs:
        for p in {profile(g) for g in s["segments"]}:  # percentiles never mix evaluation profiles
            S.rank([e for e in entries if e["sector"] == s["id"] and e["profile"] == p and not e["gate"]], p)

    # ---- leaders: hand-picked per segment, any market
    leaders = []
    for s in secs:
        for g in s["segments"]:
            for t in g.get("leaders", []):
                L = raw["leaders"].get(t) or {}
                f = L.get("fund") or {}
                if not f:
                    continue
                r = {"ticker": t, "name": (L.get("mkt") or {}).get("name") or t, "market": MARKET.get(t.split(".")[-1], "US") if "." in t else "US",
                     "mkt": L.get("mkt"), "insider": None, **f}
                r = view(r, profile(g))
                d = S.derived(r, {"profile": profile(g), "credit": bool(g.get("credit")), "verify": g.get("verify", []), "cyclical": bool(g.get("cyclical")),
                                  "no_peg": no_peg(g)})
                bull, bear = S.bull_bear(r, d, bool(g.get("cyclical")), g["id"], leader=True)
                px = L.get("px") or []
                b = bench.get(s["benchmark"]["symbol"]) or []
                leaders.append({"_r": r, "d": d, "kind": "leader", "sector": s["id"], "segment": g["id"], "cyclical": bool(g.get("cyclical")), "profile": profile(g),
                                "checks": S.checklist(r, d, bool(g.get("cyclical"))), "bull": bull, "bear": bear, "factors": {},
                                "verify": S.verify(r, d, bool(g.get("cyclical")), g["id"]),
                                "ret1y": _ret(px), "ret3m": _ret(px, 91), "bench1y": _ret(b),
                                "spark": [p[1] for p in px[:: max(1, len(px) // 60)]], "deep": deep.get(t)})

    # ---- flatten for the template
    out = []
    for e in entries + leaders:
        r = e.pop("_r")
        if e["kind"] == "emerging":
            e["level"], e["conclusion"] = S.conclusion(e)
        else:
            e["level"] = "龙头"
            e["conclusion"] = ("龙头观察。" + ("；".join(e["bull"][:2]) or "数据上没有突出的变化") + "。风险：" + ("；".join(e["bear"][:2]) or "数据上没有明显风险点") + "。")
        e["oneliner"], e["oneliner_src"] = _oneliner(r["ticker"], r.get("mkt"), notes, zh)
        # PEG screen (cross-sector module): pass = both PEGs agree it is cheap; watch = forward PEG reliable and
        # <= good_max but the historical side or a divergence flag does not agree
        F = e["d"].get("pegf") or {}
        e["peg_pass"] = bool(S.peg_cheap(e["d"]))
        e["peg_watch"] = (not e["peg_pass"]) and bool(F.get("ok")) and F.get("v") is not None and F["v"] <= S.PG["good_max"]
        e["opp"] = e["bull"][0] if e["bull"] else "数据上没有突出的机会信号"
        e["risk"] = e["bear"][0] if e["bear"] else "数据上没有明显的风险信号"
        e["ok"] = sum(1 for i in e["checks"] if i["status"] == "ok")
        e["warn"] = sum(1 for i in e["checks"] if i["status"] == "warn")
        r["name"] = " ".join(str(r.get("name") or r["ticker"]).replace("\xa0", " ").split())
        out.append({**{k: v for k, v in r.items() if k not in ("mem", "_form4")}, **e})

    # ---- qualitative layer: sourced research files + automatically extracted filing evidence
    R, A = research.manual(), research.latest_auto()
    for e in out:
        rs = R.get(e["ticker"])
        e["research"], e["auto"] = rs, A.get(e["ticker"])
        e["qual"] = _qual(rs)
        e["final"] = (f"数据面：{e['level']}；综合面：{e['qual']['label']}"
                      + (f"（{'已人工审核' if rs.get('status') == 'reviewed' else '调研草稿，待人工审核'}，{rs.get('as_of', '')}）。{rs.get('summary', '')}" if rs else "。还没有综合调研，业务、管理层、行业和产品维度待研究。"))

    # ---- sector and segment aggregates
    overview = {}
    spy = bench.get(CFG["market_benchmark"]["symbol"]) or []
    for s in secs:
        mine = [e for e in out if e["sector"] == s["id"]]
        segs = []
        for g in s["segments"]:
            es = [e for e in mine if e["segment"] == g["id"]]
            segs.append({"id": g["id"], "n_leader": sum(1 for e in es if e["kind"] == "leader"), "n_emerging": sum(1 for e in es if e["kind"] == "emerging"),
                         "n_pool": sum(1 for e in es if e.get("pool")), "mcap": sum(e["d"]["mcap"] or 0 for e in es),
                         "yoy": _median(e["yoy"] for e in es), "gm": _median(e.get("gm") for e in es), "ps": _median(e["d"]["ps"] for e in es),
                         **({"pretax_m": _median(e["d"].get("pretax_m") for e in es), "roe": _median(e["d"].get("roe") for e in es),
                             "pb": _median(e["d"].get("pb") for e in es), "pe": _median(e["d"].get("pe") for e in es)} if g.get("profile") == "financial" else {}),
                         "yoy_leaders": _median(e["yoy"] for e in es if e["kind"] == "leader"),
                         "yoy_emerging": _median(e["yoy"] for e in es if e["kind"] == "emerging")})
        b = bench.get(s["benchmark"]["symbol"]) or []
        overview[s["id"]] = {"segments": segs, "bench_ret": _ret(b), "spy_ret": _ret(spy), "bench_3m": _ret(b, 91), "spy_3m": _ret(spy, 91),
                             "bench_px": b, "spy_px": spy, "n": len(mine), "mcap": sum(x["mcap"] for x in segs),
                             "yoy": _median(e["yoy"] for e in mine), "gm": _median(e.get("gm") for e in mine),
                             **({"pretax_m": _median(e["d"].get("pretax_m") for e in mine), "roe": _median(e["d"].get("roe") for e in mine)}
                                if any(g.get("profile") == "financial" for g in s["segments"]) else {})}

    # ---- market-wide heat map (optional; needs `python -m radar heat`)
    sector_sics = {s["id"]: {c for g in s["segments"] for c in g.get("sic", [])} for s in secs}
    hm = heat.summarize(heat.latest(), sector_sics)
    sec_names = {s["id"]: s["name"] for s in secs}

    # ---- cash-flow valuation inputs (optional; needs `python -m radar valuation`). Display only: the page computes the
    # scenario / reverse DCF in the browser; nothing here touches score, factors, level, PEG tiers or the basket score.
    val = _valuation({e["ticker"] for e in out})

    data = {"heat": hm, "sector_names": sec_names, "val": val,
            "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "fetched_at": raw["fetched_at"], "periods": raw["periods"],
            "fx": raw.get("fx"), "funnel": raw["funnel"], "form4_since": raw["form4_since"], "sectors": secs, "entries": out,
            "overview": overview, "excluded": raw["excluded"], "notes": notes, "factor_cn": S.FACTOR_CN,
            "cfg": {"flags": CFG["flags"], "pool": CFG["pool"], "benchmark": CFG["market_benchmark"]}}
    with open(os.path.join(TEMPLATES, "dashboard.html"), encoding="utf-8") as f:
        tpl = f.read()
    os.makedirs(SITE, exist_ok=True)
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    by = {s["id"]: (sum(1 for e in out if e["sector"] == s["id"] and e["kind"] == "leader"),
                    sum(1 for e in out if e["sector"] == s["id"] and e["kind"] == "emerging")) for s in secs}
    print(f"site/index.html written. (leaders, emerging) per sector: {by}")
    return data
