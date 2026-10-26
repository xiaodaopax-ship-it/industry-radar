"""现金流估值 (cash-flow valuation reference) inputs for the ② screen rows + a pure-Python mirror of the page's JS engine.
Display only: nothing here enters the ④ priority score, the 价值 / 博弈 label, warn_n or the 对比池 bkScore.
情景估值参考，不是目标价，不是买卖建议.

    python -m cn valuation   latest snapshot -> data/cn/valuation_<ts>.json (only the ② screen codes are kept)

Sources (Eastmoney datacenter-web, market-wide paged queries filtered by REPORT_DATE; verified 2026-10-26):
  RPT_DMSK_FN_CASHFLOW (现金流量表摘要): NETCASH_OPERATE 经营活动产生的现金流量净额, CONSTRUCT_LONG_ASSET
      购建固定资产、无形资产和其他长期资产支付的现金 (positive number = cash paid). YTD cumulative.
  RPT_DMSK_FN_INCOME (利润表摘要): TOTAL_OPERATE_INCOME 营业总收入. YTD cumulative.
  RPT_F10_FINANCE_GBALANCE (资产负债表, general template) at the latest report date: MONETARYFUNDS 货币资金,
      TRADE_FINASSET_NOTFVTPL 交易性金融资产 (TRADE_FINASSET, the pre-2018 FVTPL line, is empty market-wide at
      2026-06-30 and is added only if present), SHORT_LOAN 短期借款, LONG_LOAN 长期借款, BOND_PAYABLE 应付债券,
      NONCURRENT_LIAB_1YEAR 一年内到期的非流动负债 (also contains the current part of lease liabilities; LEASE_LIAB
      itself is excluded). Missing lines of an existing balance sheet count as 0.
  RPTA_WEB_TREASURYYIELD (中美国债收益率, data.eastmoney.com/cjsj/zmgzsyl.html): EMM00166466 = China 10-year
      government bond yield %, latest SOLAR_DATE. Field check on every run: 2y EMM00588704 <= 10y and
      10y - 2y == EMM01276014 (the published 10y-2y spread) within 0.005pt.
  Shares and price: the snapshot quotes (RPT_VALUEANALYSIS_DET TOTAL_SHARES and CLOSE_PRICE of the same trading day);
      flagged when TOTAL_MARKET_CAP differs from price x TOTAL_SHARES by > 2%.
Periods: fiscal years FY(y-4)..FY(y) where y = last completed fiscal year before the latest report date, plus the
latest YTD period and the same YTD period one year earlier. TTM = FY(y) + YTD(latest) - YTD(year ago).
Cached (em.fetch_json; ttl 30 days for closed fiscal years, 1 day for the latest periods), polite (em 2 req/s limiter
+ pause between queries), resumable (finished pages come from the cache on a re-run). stdout is English only.
"""
import datetime as dt
import glob
import json
import math
import os
import time
import urllib.parse

from . import em

ROOT = em.ROOT
DATA_DIR = os.path.join(ROOT, "data", "cn")
VAL_RULES = {
    "n_fy": 5,                 # fiscal years of cash flow / revenue kept
    "sleep": 0.3,              # pause between market-wide queries on top of em's limiter
    "ttl_closed": 30.0,        # cache days for closed fiscal years
    "ttl_recent": 1.0,         # cache days for the latest YTD periods, the balance sheet and the bond yield
    "mcap_check_pct": 2.0,     # flag when total mcap vs price x total shares differs by more than this
    "consensus_min_org": 3,    # consensus EPS CAGR used as growth proxy only with >= 3 institutions
}
# page defaults (percent); the page lets the user change them. Mirrored by DCF_DEFAULTS in templates/cn_dashboard.html
DCF_DEFAULTS = {"erp": 5.0, "gT": 2.5, "dr": 1.0, "ag": 30.0, "am": 20.0, "glo": -10.0, "ghi": 40.0}
DCF_YEARS = 10
REV_COLS = "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,TOTAL_OPERATE_INCOME"
BAL_COLS = ("SECURITY_CODE,NOTICE_DATE,UPDATE_DATE,REPORT_DATE,MONETARYFUNDS,TRADE_FINASSET,TRADE_FINASSET_NOTFVTPL,"
            "SHORT_LOAN,LONG_LOAN,BOND_PAYABLE,NONCURRENT_LIAB_1YEAR,LEASE_LIAB")
YIELD_COLS = "SOLAR_DATE,EMM00588704,EMM00166462,EMM00166466,EMM00166469,EMM01276014"
YIELD_PAGE = "https://data.eastmoney.com/cjsj/zmgzsyl.html"


def log(msg):
    print(msg, flush=True)


def latest_valuation():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "valuation_*.json")))
    return files[-1] if files else None


def load_latest():
    p = latest_valuation()
    if not p:
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), p
    except (OSError, ValueError) as e:
        log(f"WARNING: {os.path.basename(p)} unreadable ({type(e).__name__}); valuation ignored")
        return None, p


def plan_periods(latest):
    """latest 'YYYY-MM-DD' -> (fiscal years oldest first, ytd_latest or None, ytd_year_ago or None)."""
    d = dt.date.fromisoformat(latest)
    last_fy = d.year if (d.month, d.day) == (12, 31) else d.year - 1
    fys = [f"{y}-12-31" for y in range(last_fy - VAL_RULES["n_fy"] + 1, last_fy + 1)]
    if (d.month, d.day) == (12, 31):
        return fys, None, None
    return fys, latest, dt.date(d.year - 1, d.month, d.day).isoformat()


def ttm(fy_last, ytd, ytd_ago):
    """TTM = last fiscal year + latest YTD - same YTD a year earlier. Returns (value, basis) with basis 'ttm' | 'fy' | None.
    When there is no YTD period (latest is a fiscal year end) the fiscal year is the TTM. Falls back to the fiscal year
    (basis 'fy') when a YTD value is missing."""
    if fy_last is None:
        return None, None
    if ytd is None and ytd_ago is None:
        return fy_last, "fy"
    if ytd is None or ytd_ago is None:
        return fy_last, "fy"
    return fy_last + ytd - ytd_ago, "ttm"


def risk_free(ttl):
    q = {"reportName": "RPTA_WEB_TREASURYYIELD", "columns": YIELD_COLS, "pageSize": 10, "pageNumber": 1,
         "source": "WEB", "client": "WEB", "sortColumns": "SOLAR_DATE", "sortTypes": "-1"}
    url = em.DC_URL + "?" + urllib.parse.urlencode(q)
    d = em.fetch_json(url, ttl=ttl)
    rows = ((d or {}).get("result") or {}).get("data") or []
    r = next((x for x in rows if em.num(x.get("EMM00166466")) is not None), None)
    if not r:
        return {"value": None, "date": None, "source": "RPTA_WEB_TREASURYYIELD EMM00166466", "url": url, "page": YIELD_PAGE,
                "check": "not available"}
    y2, y10, sp = em.num(r.get("EMM00588704")), em.num(r.get("EMM00166466")), em.num(r.get("EMM01276014"))
    ok = y2 is not None and sp is not None and y2 <= y10 + 1e-9 and abs((y10 - y2) - sp) <= 0.005
    return {"value": y10, "date": em.day(r.get("SOLAR_DATE")), "y2": y2, "y5": em.num(r.get("EMM00166462")),
            "y30": em.num(r.get("EMM00166469")), "spread_10_2": sp,
            "source": "东方财富数据中心 RPTA_WEB_TREASURYYIELD 字段 EMM00166466（中国国债收益率 10 年）",
            "url": url, "page": YIELD_PAGE, "check": "ok" if ok else "field check failed (10y-2y spread mismatch)"}


def fetch_valuation(snap_path=None):
    from .build import analyze
    from .cards import latest_snap
    snap_path = snap_path or latest_snap()
    if not snap_path:
        log("ERROR: no data/cn/snap_*.json found; run `python -m cn fetch` first")
        return None
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    res = analyze(snap)
    codes = [r["code"] for r in res["screen"]]
    want = set(codes)
    latest = snap["periods"]["latest"]
    fys, ytd, ytd_ago = plan_periods(latest)
    t0 = dt.datetime.now(em.CST)
    log(f"valuation: {len(codes)} screen codes; fiscal years {fys[0][:4]}..{fys[-1][:4]}, YTD {ytd} vs {ytd_ago}")
    ok_all = True
    srcs = {}

    def q(report, cols, rd, ttl, extra=""):
        nonlocal ok_all
        rows, ok = em.dc_report(report, cols, f"(REPORT_DATE='{rd}'){extra}", "SECURITY_CODE", "1", ttl=ttl,
                                label=f"valuation/{report} {rd}")
        best = em._latest_by_code(rows, date_keys=("UPDATE_DATE", "NOTICE_DATE") if "UPDATE_DATE" in cols else ("NOTICE_DATE",))
        srcs[f"{report} {rd}"] = {"ok": ok, "rows": len(rows), "codes": len(best), "screen": sum(1 for c in best if c in want)}
        log(f"  {report} {rd}: rows={len(rows)} codes={len(best)} screen={srcs[f'{report} {rd}']['screen']} ok={ok}")
        ok_all = ok_all and ok
        time.sleep(VAL_RULES["sleep"])
        return best

    dates = fys + [d for d in (ytd_ago, ytd) if d]
    items = {c: {"rev": {}, "ocf": {}, "capex": {}} for c in codes}
    for rd in dates:
        ttl = VAL_RULES["ttl_recent"] if rd >= (ytd_ago or fys[-1]) else VAL_RULES["ttl_closed"]
        cf = q("RPT_DMSK_FN_CASHFLOW", em._CF_COLS, rd, ttl)
        inc = q("RPT_DMSK_FN_INCOME", REV_COLS, rd, ttl)
        for c in codes:
            a, b = cf.get(c) or {}, inc.get(c) or {}
            items[c]["ocf"][rd] = em.num(a.get("NETCASH_OPERATE"))
            items[c]["capex"][rd] = em.num(a.get("CONSTRUCT_LONG_ASSET"))
            items[c]["rev"][rd] = em.num(b.get("TOTAL_OPERATE_INCOME"))
    bal = q("RPT_F10_FINANCE_GBALANCE", BAL_COLS, latest, VAL_RULES["ttl_recent"], '(SECURITY_TYPE_CODE="058001001")')
    for c in codes:
        b = bal.get(c)
        items[c]["bal"] = None if not b else {k.lower(): em.num(b.get(k)) for k in
                                               ("MONETARYFUNDS", "TRADE_FINASSET", "TRADE_FINASSET_NOTFVTPL", "SHORT_LOAN",
                                                "LONG_LOAN", "BOND_PAYABLE", "NONCURRENT_LIAB_1YEAR", "LEASE_LIAB")}
    rf = risk_free(VAL_RULES["ttl_recent"])
    log(f"  risk-free (China 10y): {rf.get('value')} on {rf.get('date')} check={rf.get('check')}")
    doc = {"fetched_at": t0.strftime("%Y-%m-%d %H:%M"), "snap": os.path.basename(snap_path), "latest": latest,
           "fiscal_years": fys, "ytd": ytd, "ytd_ago": ytd_ago, "rules": VAL_RULES, "complete": ok_all, "rf": rf,
           "sources": srcs, "items": items, "http": dict(em.STATS)}
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"valuation_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, path)
    # coverage summary
    Q = {x["code"]: x for x in snap["quotes"]}
    rows_by = {r["code"]: r for r in res["screen"]}
    cmp_ = [compact(items[c], doc, Q.get(c)) for c in codes]
    n3 = sum(1 for x in cmp_ if x and sum(1 for m in x["m"][-3:] if m is not None) == 3)
    nr0 = sum(1 for x in cmp_ if x and x["r0"] is not None)
    nbal = sum(1 for x in cmp_ if x and x["cash"] is not None)
    neg = sum(1 for c, x in zip(codes, cmp_) if x and (base_margin(x["m"], rows_by[c]["cyclical"])[0] or 0) <= 0
              and base_margin(x["m"], rows_by[c]["cyclical"])[0] is not None)
    log(f"saved {os.path.relpath(path, ROOT)}: complete={ok_all} codes={len(codes)} ttm_revenue={nr0} "
        f"fcf_3y={n3} balance={nbal} base_margin<=0={neg} rf={rf.get('value')} ({rf.get('date')}) http={em.STATS}")
    return path


# ----------------------------------------------------------------------------- derived inputs (compact, for the page)
def _r(x, d=0):
    return None if x is None else (round(x) if d == 0 else round(x, d))


def compact(it, doc, quote):
    """Compact DCF inputs of one company: r0 TTM revenue, rb basis ('ttm' | 'fy'), o / x TTM OCF / capex,
    m FCF margins per fiscal year (fraction, oldest first, None if missing or revenue <= 0), g3 revenue CAGR % from
    FY(y-3) to FY(y), cash / debt / sh, mcf = True only when total mcap vs price x shares differs by > 2%."""
    if not it:
        return None
    fys, ytd, ago = doc["fiscal_years"], doc.get("ytd"), doc.get("ytd_ago")
    rev, ocf, cap = it.get("rev") or {}, it.get("ocf") or {}, it.get("capex") or {}
    r0, rb = ttm(rev.get(fys[-1]), rev.get(ytd) if ytd else None, rev.get(ago) if ago else None)
    o, ob = ttm(ocf.get(fys[-1]), ocf.get(ytd) if ytd else None, ocf.get(ago) if ago else None)
    x, xb = ttm(cap.get(fys[-1]), cap.get(ytd) if ytd else None, cap.get(ago) if ago else None)
    m = []
    for y in fys:
        rv, oc, cx = rev.get(y), ocf.get(y), cap.get(y)
        m.append(None if rv is None or rv <= 0 or oc is None or cx is None else round((oc - cx) / rv, 6))
    a, b = rev.get(fys[-4]) if len(fys) >= 4 else None, rev.get(fys[-1])
    g3 = round(((b / a) ** (1 / 3) - 1) * 100, 3) if a and b and a > 0 and b > 0 else None
    bl = it.get("bal")
    cash = debt = None
    if bl:
        cash = (bl.get("monetaryfunds") or 0) + (bl.get("trade_finasset_notfvtpl") or 0) + (bl.get("trade_finasset") or 0)
        debt = sum(bl.get(k) or 0 for k in ("short_loan", "long_loan", "bond_payable", "noncurrent_liab_1year"))
        if bl.get("monetaryfunds") is None:
            cash = None
    q = quote or {}
    sh, px, mc = q.get("total_shares"), q.get("price"), q.get("total_mcap")
    mcf = bool(sh and px and mc and abs(mc / (sh * px) - 1) * 100 > VAL_RULES["mcap_check_pct"])
    # TTM OCF / capex are shown on the card only (not used in the valuation); dropped when their bases differ
    same = ob == xb
    out = {"r0": _r(r0), "rb": rb, "o": _r(o) if same else None, "x": _r(x) if same else None,
           "m": m, "g3": g3, "cash": _r(cash), "debt": _r(debt), "sh": _r(sh)}
    if mcf:
        out["mcf"] = True
    return out


def consensus_growth(cons, latest, min_org=None):
    """Consensus EPS CAGR % from the current-year estimate to +2 years (+1 if only two years), same rule as the
    forward PEG growth (cards.peg), but computed for every card (also cyclical / PE <= 0 ones).
    Returns {g, y0, yn, org} or {g: None, why}."""
    min_org = VAL_RULES["consensus_min_org"] if min_org is None else min_org
    if not cons:
        return {"g": None, "why": "无机构一致预期"}
    org = cons.get("org_num") or 0
    if org < min_org:
        return {"g": None, "why": f"一致预期覆盖机构 {org:.0f} 家 < {min_org}", "org": org}
    est = {e["year"]: e["eps"] for e in cons.get("eps", []) if e.get("kind") == "estimate" and e.get("eps") is not None}
    y_rep = int(latest[:4])
    y0 = min((y for y in est if y >= y_rep), default=None)
    if y0 is None:
        return {"g": None, "why": f"缺当年（{y_rep}E）一致预期", "org": org}
    n = 2 if (y0 + 2) in est and (y0 + 1) in est else 1 if (y0 + 1) in est else 0
    if n == 0:
        return {"g": None, "why": f"只有 {y0}E 一年预期", "org": org}
    path = [est[y0 + k] for k in range(n + 1)]
    if any(e <= 0 for e in path):
        return {"g": None, "why": "一致预期 EPS 有 ≤ 0 的年度", "org": org}
    return {"g": round(((path[-1] / path[0]) ** (1 / n) - 1) * 100, 4), "y0": y0, "yn": y0 + n, "org": org}


def attach(rows, doc, snap):
    """Add r['dcf'] (compact inputs) to every ② screen row in place. Called after analyze(): display only."""
    Q = {q["code"]: q for q in snap["quotes"]}
    items = (doc or {}).get("items") or {}
    for r in rows:
        r["dcf"] = compact(items.get(r["code"]), doc, Q.get(r["code"])) if doc else None


def meta(doc, path, rows, cards):
    if not doc:
        return {"file": None, "defaults": DCF_DEFAULTS, "years": DCF_YEARS}
    have = [r for r in rows if r.get("dcf")]
    return {"file": os.path.basename(path), "fetched_at": doc.get("fetched_at"), "snap": doc.get("snap"),
            "complete": doc.get("complete"), "fy": [y[:4] for y in doc["fiscal_years"]], "ytd": doc.get("ytd"),
            "ytd_ago": doc.get("ytd_ago"), "latest": doc.get("latest"),
            "rf": {k: doc["rf"].get(k) for k in ("value", "date", "source", "page", "check", "y2", "spread_10_2")},
            "defaults": DCF_DEFAULTS, "years": DCF_YEARS, "n_screen": len(rows),
            "n_r0": sum(1 for r in have if r["dcf"]["r0"] is not None),
            "n_m3": sum(1 for r in have if sum(1 for m in r["dcf"]["m"][-3:] if m is not None) == 3),
            "n_bal": sum(1 for r in have if r["dcf"]["cash"] is not None),
            "n_cons": sum(1 for c in cards if (c.get("dcf_g") or {}).get("g") is not None)}


# ----------------------------------------------------------------------------- engine (mirror of the page JS)
def base_margin(m, cyclical):
    """(m0, years_used, flag). Non-cyclical: mean of the last 3 fiscal years that have a margin (within the last 3);
    cyclical: mean of up to 5 years."""
    win = m[-5:] if cyclical else m[-3:]
    xs = [v for v in win if v is not None]
    if not xs:
        return None, 0, "missing"
    return sum(xs) / len(xs), len(xs), ("cyc" if cyclical else "") + ("" if len(xs) == len(win) else "short")


def dcf_engine(r0, m, g0, gT, r, nc, sh, n=DCF_YEARS):
    """Fractions. Revenue grows g_t, fading linearly from g0 (year 1) to gT (year n); FCF_t = revenue_t x m;
    TV = FCF_n x (1 + gT) / (r - gT); EV = sum FCF_t / (1 + r)^t + TV / (1 + r)^n; equity = EV + net cash."""
    rev, pv, fcf = r0, 0.0, 0.0
    for t in range(1, n + 1):
        g = g0 + (gT - g0) * (t - 1) / (n - 1)
        rev *= 1 + g
        fcf = rev * m
        pv += fcf / (1 + r) ** t
    tv = fcf * (1 + gT) / (r - gT)
    pv_tv = tv / (1 + r) ** n
    ev = pv + pv_tv
    eq = ev + nc
    return {"ev": ev, "eq": eq, "vps": eq / sh, "pv_tv": pv_tv}


def dcf_reverse(r0, m, gT, r, nc, sh, price, lo=-0.30, hi=1.00, iters=200):
    """Starting growth g0 such that value per share == price (bisection). m must be > 0."""
    f = lambda g: dcf_engine(r0, m, g, gT, r, nc, sh)["vps"] - price
    if f(lo) > 0:
        return {"status": "below", "g": None}
    if f(hi) < 0:
        return {"status": "above", "g": None}
    a, b = lo, hi
    for _ in range(iters):
        c = (a + b) / 2
        if f(c) < 0:
            a = c
        else:
            b = c
    return {"status": "ok", "g": (a + b) / 2}


def margin_required(r0, g0, gT, r, nc, sh, price):
    unit = dcf_engine(r0, 1.0, g0, gT, r, 0.0, sh)["ev"]
    return (price * sh - nc) / unit


def dcf_case(d, price, cyclical, cons_g, rev_yoy, P):
    """Mirror of the page's dcfCase(). d = compact inputs, P = percent params {rf, erp, gT, dr, ag, am, glo, ghi}.
    Growth source: consensus EPS CAGR (cards, >= 3 institutions) > 3-year revenue CAGR > latest YTD revenue YoY.
    Returns {status, why?, m0, g0, gsrc, capped, gT, r: {bear, base, bull}, vps: {...}, rev_g, m_req}."""
    if not d:
        return {"status": "na", "why": "nodata"}
    if P.get("rf") is None:
        return {"status": "na", "why": "rf"}
    if d.get("r0") is None or d["r0"] <= 0:
        return {"status": "na", "why": "r0"}
    if d.get("cash") is None:
        return {"status": "na", "why": "bal"}
    if not d.get("sh") or not price:
        return {"status": "na", "why": "sh"}
    m0, nm, mflag = base_margin(d["m"], cyclical)
    if m0 is None:
        return {"status": "na", "why": "m"}
    if cons_g is not None:
        g, src = cons_g, "cons"
    elif d.get("g3") is not None:
        g, src = d["g3"], "g3"
    else:
        g, src = rev_yoy, "yoy"
    if g is None:
        return {"status": "na", "why": "g"}
    gc = min(max(g, P["glo"]), P["ghi"])
    gT = min(P["gT"], P["rf"])
    rb = P["rf"] + P["erp"]
    nc = d["cash"] - d["debt"]
    out = {"m0": m0, "g0": gc, "graw": g, "gsrc": src, "capped": gc != g, "gT": gT, "nc": nc}
    R = {"bear": rb + P["dr"], "base": rb, "bull": rb - P["dr"]}
    out["r"] = R
    if rb - gT <= 0.5:
        out.update({"status": "na", "why": "rg"})
        return out
    if m0 <= 0:
        out["status"] = "negfcf"
        out["m_req"] = margin_required(d["r0"], gc / 100, gT / 100, rb / 100, nc, d["sh"], price)
        return out
    G = {"bear": gc - abs(gc) * P["ag"] / 100, "base": gc, "bull": gc + abs(gc) * P["ag"] / 100}
    M = {"bear": m0 * (1 - P["am"] / 100), "base": m0, "bull": m0 * (1 + P["am"] / 100)}
    out["vps"] = {}
    for k in ("bear", "base", "bull"):
        out["vps"][k] = None if R[k] - gT <= 0.5 else dcf_engine(d["r0"], M[k], G[k] / 100, gT / 100, R[k] / 100, nc, d["sh"])["vps"]
    out["status"] = "ok"
    rv = dcf_reverse(d["r0"], m0, gT / 100, rb / 100, nc, d["sh"], price)
    out["rev"] = rv["status"]
    out["rev_g"] = None if rv["g"] is None else rv["g"] * 100
    out["m_req"] = margin_required(d["r0"], gc / 100, gT / 100, rb / 100, nc, d["sh"], price)
    return out
