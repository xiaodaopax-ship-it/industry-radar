"""5-year PE(TTM) / PB percentiles for the 价值通道, 红利筛选 and 长期红利 rows (display there) and for every 新兴筛选 row
outside the snapshot's 估值短名单. For the 新兴筛选 rows the percentile feeds the 估值分位 gate and therefore the
基本面支撑 / 交易驱动 label (cn/build.valpct_fallback, only when this file has the snapshot's quote date); it never enters
the research-priority score, the ⚠ count or the 对比池 score.

    python -m cn valpct   em.valuation_history (RPT_VALUEANALYSIS_DET, 5 years daily) for every code of those views
                          (evaluation-card percentiles are copied, not fetched) -> data/cn/valpct_<ts>.json

Same method as the evaluation cards (em.valuation_percentiles): share of the last 5 years of daily values (> 0) that are
<= the latest value. Items of the previous file are reused when they were computed for the same quote date.
stdout is English only.
"""
import datetime as dt
import glob
import json
import os
import time

from . import em

DATA_DIR = os.path.join(em.ROOT, "data", "cn")
RULES = {"years": 5, "ttl": 1.0, "sleep": 0.1, "max_consecutive_fail": 10}


def log(msg):
    print(msg, flush=True)


def latest():
    fs = sorted(glob.glob(os.path.join(DATA_DIR, "valpct_*.json")))
    if not fs:
        return None
    try:
        with open(fs[-1], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


HDIR = os.path.join(DATA_DIR, "valhist")   # per-code daily [date, pe_ttm, pb], 5 years, updated incrementally


def history(code, as_of):
    """5 years of daily PE(TTM) / PB for one code: the local store plus only the days since its last date
    (one request a week instead of three); the first run fetches the full 5 years."""
    os.makedirs(HDIR, exist_ok=True)
    p = os.path.join(HDIR, f"{code}.json")
    try:
        with open(p, encoding="utf-8") as f:
            have = json.load(f)
    except (OSError, ValueError):
        have = []
    start5 = (dt.date.fromisoformat(as_of) - dt.timedelta(days=int(RULES["years"] * 365.25))).isoformat()
    if have and have[-1][0] >= (dt.date.fromisoformat(as_of) - dt.timedelta(days=120)).isoformat():
        since = (dt.date.fromisoformat(have[-1][0]) - dt.timedelta(days=7)).isoformat()   # overlap: late revisions
        rows, ok = em.dc_report("RPT_VALUEANALYSIS_DET", "TRADE_DATE,PE_TTM,PB_MRQ", f"(SECURITY_CODE=\"{code}\")(TRADE_DATE>='{since}')",
                                "TRADE_DATE", "1", ttl=RULES["ttl"], label=f"valpct {code}")
        if not ok:
            return []
        new = [[em.day(r.get("TRADE_DATE")), em.num(r.get("PE_TTM")), em.num(r.get("PB_MRQ"))] for r in rows if em.day(r.get("TRADE_DATE"))]
        merged = {d: [d, a, b] for d, a, b in have}
        merged.update({d: [d, a, b] for d, a, b in new})
        rows_ = [merged[d] for d in sorted(merged)]
    else:
        h = em.valuation_history(code, years=RULES["years"], ttl=RULES["ttl"])
        rows_ = [[x["date"], x["pe_ttm"], x["pb"]] for x in h if x.get("date")]
    rows_ = [r for r in rows_ if r[0] >= start5]
    if rows_:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rows_, f)
        os.replace(p + ".tmp", p)
    return [{"date": d, "pe_ttm": a, "pb": b} for d, a, b in rows_]


def view_codes(snap, res):
    """Codes of the value channel, dividend screen and long-term dividend view (same inputs as cn build), then every
    新兴筛选 row without a snapshot valuation (its 估值分位 gate and 基本面支撑 label use this file, see build.valpct_fallback)."""
    from . import quality as qmod, dividend as dmod, valuation as vmod
    qdoc, _ = qmod.load_latest()
    vdoc, _ = vmod.load_latest()
    rf = ((vdoc or {}).get("rf") or {}).get("value")
    out = []
    if qdoc:
        out += [r["code"] for r in qmod.channel(snap, res, qdoc, rf).get("rows") or []]
        out += [r["code"] for r in dmod.panel(snap, res, qdoc, rf).get("rows") or []]
        out += [r["code"] for r in dmod.long_term(snap, res, qdoc, rf).get("rows") or []]
    sv = snap.get("valuation") or {}
    out += [r["code"] for r in res["screen"] if r["code"] not in sv]
    return list(dict.fromkeys(out))


def fetch():
    from .build import analyze, latest_snap
    from .cards import with_extras, latest_cards
    sp = latest_snap()
    with open(sp, encoding="utf-8") as f:
        snap = json.load(f)
    res = with_extras(snap, analyze(snap))
    qd = next((q.get("as_of") for q in snap["quotes"] if q.get("as_of")), snap.get("today"))[:10]
    carded = {}
    cp = latest_cards()
    if cp:
        with open(cp, encoding="utf-8") as f:
            carded = {c: x["val_pct"] for c, x in (json.load(f).get("cards") or {}).items() if x.get("val_pct")}
    codes = view_codes(snap, res)
    prev = latest() or {}
    reuse = dict(prev.get("items") or {}) if prev.get("as_of") == qd else {}
    # evaluation cards already computed the same percentile for this snapshot: copy it instead of fetching again
    for c in codes:
        if c in carded and c not in reuse:
            vp = carded[c]
            reuse[c] = {"pe_pct": (vp.get("pe_ttm") or {}).get("percentile"), "pb_pct": (vp.get("pb") or {}).get("percentile"),
                        "n": (vp.get("pb") or {}).get("n"), "first": None, "last": None, "src": "card"}
    log(f"valpct: {len(codes)} codes, quote date {qd}, reusable (previous file or card) {sum(1 for c in codes if c in reuse)}")
    items, fails, consec = {}, [], 0
    for i, c in enumerate(codes, 1):
        if c in reuse:
            items[c] = reuse[c]
            continue
        hist = history(c, qd)
        if not hist:
            fails.append(c)
            consec += 1
            if consec >= RULES["max_consecutive_fail"]:
                log(f"  stopping after {consec} consecutive failures at {i}/{len(codes)}")
                break
            continue
        consec = 0
        p = {x["metric"]: x for x in em.valuation_percentiles(hist, keys=("pe_ttm", "pb"))}
        items[c] = {"pe_pct": (p.get("pe_ttm") or {}).get("percentile"), "pb_pct": (p.get("pb") or {}).get("percentile"),
                    "n": (p.get("pb") or {}).get("n"), "first": hist[0]["date"], "last": hist[-1]["date"]}
        time.sleep(RULES["sleep"])
        if i % 100 == 0:
            log(f"  valpct {i}/{len(codes)} ok={len(items)} failed={len(fails)}")
    t0 = dt.datetime.now(em.CST)
    path = os.path.join(DATA_DIR, f"valpct_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"fetched_at": t0.strftime("%Y-%m-%d %H:%M"), "as_of": qd, "snap": os.path.basename(sp), "rules": RULES,
                   "requested": len(codes), "items": items, "failed": fails, "http": dict(em.STATS)}, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    log(f"saved {os.path.relpath(path, em.ROOT)}: ok={len(items)}/{len(codes)} failed={len(fails)}")
    return path if len(items) >= 0.9 * len(codes) else None


def attach(rows, cards_by_code, doc):
    """Put pe_pct / pb_pct on view rows in place: the evaluation card percentile first, else the valpct file."""
    items = (doc or {}).get("items") or {}
    for r in rows:
        vp = (cards_by_code.get(r["code"]) or {}).get("val_pct") or {}
        it = items.get(r["code"]) or {}
        pe = (vp.get("pe_ttm") or {}).get("percentile") if vp else it.get("pe_pct")
        pb = (vp.get("pb") or {}).get("percentile") if vp else it.get("pb_pct")
        r["pe_pct"], r["pb_pct"] = pe, pb
