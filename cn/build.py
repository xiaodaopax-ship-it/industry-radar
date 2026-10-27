"""A-share pipeline: fetch a snapshot (network, via cn/em.py only) and render site/cn/index.html (offline).

    python -m cn fetch   pull data, save data/cn/snap_<ts>.json
    python -m cn build   render site/cn/index.html from the latest snapshot (no network)

Research tooling, not investment advice. All thresholds live in RULES below and are shown on the page.
stdout is English only; Chinese text lives in the snapshot / rendered HTML.
"""
import collections
import datetime as dt
import glob
import json
import os
import statistics
import sys

from . import em

ROOT = em.ROOT
DATA_DIR = os.path.join(ROOT, "data", "cn")
SITE_DIR = os.path.join(ROOT, "site", "cn")
TEMPLATE = os.path.join(ROOT, "templates", "cn_dashboard.html")

RULES = {
    "min_industry_n": 8,                 # companies per industry for the heat table
    "growth_yoy_min": 20.0,              # YTD revenue YoY %, growth screen
    "revenue_floor_per_quarter": 1e8,    # CNY per quarter in the YTD period (H1 => 2亿)
    "ocf_np_warn": 0.5,                  # warn if OCF / parent NP < 0.5 (or OCF < 0 with NP > 0)
    "ar_gap_pp": 20.0,                   # warn if receivables YoY > revenue YoY + 20pt
    "inv_gap_pp": 20.0,                  # warn if inventory YoY > revenue YoY + 20pt
    "goodwill_equity_warn": 30.0,        # % of parent equity
    "pledge_warn": 30.0,                 # % of total shares (company-wide)
    "net_sell_warn": 1.0,                # net holder decrease >= 1% of total shares in 6 months
    "holder_months": 6,
    "unlock_days": 90,
    "unlock_float_warn": 5.0,            # % of current float shares
    "reg_months": 12,
    "pe_pct_high": 80.0,                 # PE percentile >= 80 => 偏博弈 trigger / ⚠
    "value_pe_pct_max": 50.0,            # 偏价值 needs PE percentile <= 50
    "value_ocf_np_min": 0.8,             # 偏价值 needs OCF / NP >= 0.8
    "valuation_years": 5,
    "shortlist_max": 60,                 # stocks that get valuation_history (rate-limit budget)
    "priority_top": 30,
    "priority_max_warn": 1,              # risk-gate ⚠ allowed for the research-priority list
    "cyclical_top": 20,                  # rows in the separate cyclical-industry list
    # Eastmoney industries whose earnings move mainly with commodity / freight prices or a capacity cycle. Companies in
    # them are never labeled 偏价值 and are listed separately instead of entering the research-priority ranking (same
    # treatment as cyclical segments in the US pipeline). Low PE near a cycle peak is a known trap, so PE percentile is
    # not used as a "cheap" signal for them.
    "cyclical_industries": ["油气开采Ⅱ", "油服工程", "炼化及贸易", "煤炭开采", "焦炭Ⅱ", "工业金属", "小金属", "贵金属",
                            "能源金属", "普钢", "特钢Ⅱ", "冶钢原料", "化学原料", "化学纤维", "农化制品", "塑料", "橡胶",
                            "水泥", "玻璃玻纤", "造纸", "养殖业", "饲料", "航运港口", "工程机械"],
}
# regulatory kinds that count as a hit (review_inquiry = IPO/refinancing review Q&A, case_filing_other = lawsuits)
REG_COUNT_KINDS = {"inquiry_letter", "concern_letter", "regulatory_letter", "warning_letter", "investigation",
                   "disciplinary_action", "administrative_penalty"}
REG_KIND_CN = {"inquiry_letter": "问询函", "concern_letter": "关注函", "regulatory_letter": "监管函",
               "warning_letter": "警示函", "investigation": "立案调查", "disciplinary_action": "纪律处分",
               "administrative_penalty": "行政处罚"}
NEG_FORECAST = {"decrease", "slight_decrease", "first_loss", "continued_loss", "larger_loss"}
FIN_INDUSTRY_WORDS = ("银行", "保险", "证券", "多元金融")
RISK_GATES = ("ocf", "ar", "inv", "goodwill", "pledge", "selling", "unlock", "reg")


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------- dates
def quarter_ends_before(d, n):
    out, y = [], d.year
    while len(out) < n:
        for m, dd in ((12, 31), (9, 30), (6, 30), (3, 31)):
            q = dt.date(y, m, dd)
            if q < d and len(out) < n:
                out.append(q)
        y -= 1
    return out


def prev_quarter(d):
    return {3: dt.date(d.year - 1, 12, 31), 6: dt.date(d.year, 3, 31), 9: dt.date(d.year, 6, 30),
            12: dt.date(d.year, 9, 30)}[d.month]


def year_ago(d):
    return dt.date(d.year - 1, d.month, d.day)


def months_ago(d, m):
    return d - dt.timedelta(days=int(m * 30.44))


# ----------------------------------------------------------------------------- trimming
Q_KEYS = ("code", "name", "board", "price", "total_mcap", "float_mcap", "pe_ttm", "pb", "industry",
          "total_shares", "float_shares", "as_of", "quote_source")
F_KEYS = ("code", "name", "risk_warning", "industry", "notice_date", "revenue", "revenue_yoy_pct",
          "parent_net_profit", "net_profit_yoy_pct", "gross_margin_pct", "operating_cash_flow",
          "accounts_receivable", "inventory", "goodwill", "parent_equity", "has_balance_sheet")


def trim(rows, keys):
    return [{k: r.get(k) for k in keys} for r in rows]


# ----------------------------------------------------------------------------- fetch
def fetch():
    today = dt.date.today()
    t0 = dt.datetime.now(em.CST)
    snap = {"fetched_at": t0.strftime("%Y-%m-%d %H:%M"), "today": today.isoformat(), "rules": RULES}

    log("fetch: quotes (EOD datacenter snapshot)")
    q = em.quotes(live=False)
    snap["quotes"] = trim(q, Q_KEYS)
    log(f"  quotes rows={len(q)}")

    # pick the latest report date whose coverage is >= 80% of the best-covered recent date
    fin_cache, counts = {}, {}
    for d in quarter_ends_before(today, 2):
        rows = em.financials(d.isoformat(), ttl=1.0)
        fin_cache[d] = rows
        counts[d] = sum(1 for r in rows if r.get("revenue") is not None)
        log(f"  financials {d} rows={len(rows)} with_revenue={counts[d]}")
    best = max(counts.values()) if counts else 0
    latest = next((d for d in sorted(counts, reverse=True) if best and counts[d] >= 0.8 * best), None)
    if latest is None:
        log("ERROR: no usable financial report date")
        sys.exit(1)
    prev, yago = prev_quarter(latest), year_ago(latest)
    snap["coverage"] = {k.isoformat(): v for k, v in counts.items()}
    for d in (prev, yago):
        if d not in fin_cache:
            fin_cache[d] = em.financials(d.isoformat(), ttl=30.0 if d < latest else 1.0)
            log(f"  financials {d} rows={len(fin_cache[d])}")
    snap["periods"] = {"latest": latest.isoformat(), "prev": prev.isoformat(), "year_ago": yago.isoformat()}
    snap["fin"] = {k: trim(fin_cache[dt.date.fromisoformat(v)], F_KEYS) for k, v in snap["periods"].items()}

    since_h = months_ago(today, RULES["holder_months"]).isoformat()
    log(f"fetch: forecasts since {since_h}")
    fc = em.forecasts(since_h)
    snap["forecasts"] = [r for r in fc if r.get("metric_code") == "004"]
    for r in snap["forecasts"]:
        r.pop("content_cn", None)
        r.pop("reason_cn", None)
    log(f"  forecasts rows={len(fc)} net_profit_rows={len(snap['forecasts'])}")

    log(f"fetch: holder changes since {since_h}")
    hc = em.holder_changes(since_h)
    snap["holders"] = trim(hc, ("source", "code", "date", "start_date", "holder", "direction", "shares", "ratio_pct"))
    log(f"  holder rows={len(hc)}")

    log("fetch: pledge")
    pl = em.pledge()
    snap["pledge"] = trim(pl, ("code", "stats_date", "pledge_ratio_pct"))
    log(f"  pledge rows={len(pl)}")

    end = (today + dt.timedelta(days=RULES["unlock_days"])).isoformat()
    log(f"fetch: unlocks {today} .. {end}")
    ul = em.unlocks(today.isoformat(), end)
    snap["unlocks"] = trim(ul, ("code", "date", "shares", "tradable_shares", "ratio_total_pct", "ratio_float_pct",
                                "unlock_value", "type_cn"))
    snap["unlock_window"] = [today.isoformat(), end]
    log(f"  unlock rows={len(ul)}")

    # cninfo title search, one market-wide call per calendar month (keeps each keyword under the page cap)
    reg, seen = [], set()
    start = months_ago(today, RULES["reg_months"])
    win = start
    log(f"fetch: regulatory (cninfo title search) since {start}, monthly windows")
    def reg_window(a, b):
        """One cninfo pass; if a keyword hit the per-keyword page cap (~600 rows), split the window in two."""
        ttl = 1.0 if b >= today - dt.timedelta(days=3) else 30.0
        rows = em.regulatory(a.isoformat(), b.isoformat(), ttl=ttl)
        per_kw = {}
        for r in rows:
            per_kw[r.get("keyword_cn")] = per_kw.get(r.get("keyword_cn"), 0) + 1
        if max(per_kw.values() or [0]) >= 20 * 30 - 15 and (b - a).days >= 4:
            mid = a + dt.timedelta(days=(b - a).days // 2)
            log(f"  window {a}..{b} hit the page cap; splitting at {mid}")
            return rows + reg_window(a, mid) + reg_window(mid + dt.timedelta(days=1), b)
        return rows

    while win <= today:
        nxt = min(win + dt.timedelta(days=30), today)
        rows = reg_window(win, nxt)
        for r in rows:
            if r["announcement_id"] in seen:
                continue
            seen.add(r["announcement_id"])
            reg.append({k: r.get(k) for k in ("code", "date", "title_cn", "kind", "url")})
        log(f"  window {win}..{nxt} rows={len(rows)}")
        if nxt >= today:
            break
        win = nxt + dt.timedelta(days=1)
    snap["regulatory"] = reg
    snap["reg_window"] = [start.isoformat(), today.isoformat()]
    log(f"  regulatory rows={len(reg)}")

    # shortlist (pre-valuation ranking) -> valuation history, <= shortlist_max stocks
    pre = analyze(snap, valuation={})
    shortlist = pre["shortlist"]
    log(f"fetch: valuation history for shortlist n={len(shortlist)} ({RULES['valuation_years']}y)")
    val = {}
    for i, code in enumerate(shortlist, 1):
        h = em.valuation_history(code, RULES["valuation_years"])
        ps = {p["metric"]: p for p in em.valuation_percentiles(h, keys=("pe_ttm", "pb"))}
        val[code] = {"n_days": len(h), "first": h[0]["date"] if h else None, "last": h[-1]["date"] if h else None,
                     "pe": ps.get("pe_ttm"), "pb": ps.get("pb")}
        if i % 10 == 0:
            log(f"  valuation {i}/{len(shortlist)}")
    snap["valuation"] = val
    snap["shortlist"] = shortlist
    snap["http"] = dict(em.STATS)

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"snap_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    log(f"saved {os.path.relpath(path, ROOT)} ({os.path.getsize(path) / 1e6:.1f} MB); http={em.STATS}")
    from .cards import fetch_cards
    fetch_cards(path, snap)
    return path


# ----------------------------------------------------------------------------- analysis helpers
def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def pct_rank(values):
    """dict key -> percentile 0..100 (average rank for ties); None values excluded."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        p = 100.0 * ((i + j) / 2) / (n - 1) if n > 1 else 50.0
        for t in range(i, j + 1):
            out[items[t][1]] = round(p, 1)
        i = j + 1
    return out


def ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def growth(cur, old):
    return None if cur is None or old is None or old <= 0 else (cur / old - 1) * 100


def r1(x, d=1):
    return None if x is None else round(x, d)


def quarters_in(period):
    return {3: 1, 6: 2, 9: 3, 12: 4}[int(period[5:7])]


# ----------------------------------------------------------------------------- analysis
def analyze(snap, valuation=None):
    valuation = snap.get("valuation", {}) if valuation is None else valuation
    P = snap["periods"]
    Q = {r["code"]: r for r in snap["quotes"]}
    F = {k: {r["code"]: r for r in snap["fin"][k]} for k in ("latest", "prev", "year_ago")}
    fl, fp, fy = F["latest"], F["prev"], F["year_ago"]

    # ---- industry heat (Eastmoney industry from quotes BOARD_NAME)
    groups = {}
    for code, q in Q.items():
        f = fl.get(code)
        if not f or f.get("revenue") is None or not q.get("industry"):
            continue
        groups.setdefault(q["industry"], []).append(code)

    # ---- per-company aggregates
    today = dt.date.fromisoformat(snap["today"])
    hold = {}
    seen = set()
    for h in snap["holders"]:
        key = (h["source"], h["code"], h.get("holder"), h.get("start_date"), h.get("date"), h.get("shares"))
        if key in seen or h.get("shares") is None:
            continue
        seen.add(key)
        a = hold.setdefault(h["code"], {"exec": 0.0, "major": 0.0, "n": 0})
        a["exec" if h["source"] == "executive" else "major"] += h["shares"]
        a["n"] += 1
    unl = {}
    for u in snap["unlocks"]:
        a = unl.setdefault(u["code"], {"shares": 0.0, "tradable": 0.0, "ratio_total": 0.0, "ratio_float": 0.0,
                                       "value": 0.0, "first": u["date"], "types": set()})
        a["shares"] += u.get("shares") or 0
        a["tradable"] += u.get("tradable_shares") or u.get("shares") or 0
        a["ratio_total"] += u.get("ratio_total_pct") or 0
        a["ratio_float"] += u.get("ratio_float_pct") or 0
        a["value"] += u.get("unlock_value") or 0
        a["first"] = min(a["first"] or "9", u["date"] or "9")
        if u.get("type_cn"):
            a["types"].add(u["type_cn"])
    reg = {}
    for r in snap["regulatory"]:
        if r.get("kind") in REG_COUNT_KINDS:
            reg.setdefault(r["code"], []).append(r)
    for v in reg.values():
        v.sort(key=lambda r: r.get("date") or "", reverse=True)
    pledge = {p["code"]: p.get("pledge_ratio_pct") for p in snap["pledge"]}
    fcast = {}
    for r in snap["forecasts"]:
        c = r["code"]
        if c not in fcast or (r.get("notice_date") or "") > (fcast[c].get("notice_date") or ""):
            fcast[c] = r

    industries = []
    for ind, codes in groups.items():
        ys = [fl[c].get("revenue_yoy_pct") for c in codes]
        with_y = [c for c in codes if fl[c].get("revenue_yoy_pct") is not None and fl[c]["revenue_yoy_pct"] > -100]
        if len(with_y) < RULES["min_industry_n"]:
            continue
        rev = sum(fl[c]["revenue"] for c in with_y)
        rev_old = sum(fl[c]["revenue"] / (1 + fl[c]["revenue_yoy_pct"] / 100) for c in with_y)
        both = [c for c in with_y if fp.get(c, {}).get("revenue_yoy_pct") is not None]
        gm = [fl[c]["gross_margin_pct"] - fy[c]["gross_margin_pct"] for c in codes
              if fl[c].get("gross_margin_pct") is not None and fy.get(c, {}).get("gross_margin_pct") is not None]
        on = [fl[c]["operating_cash_flow"] / fl[c]["parent_net_profit"] for c in codes
              if fl[c].get("operating_cash_flow") is not None and (fl[c].get("parent_net_profit") or 0) > 0]
        industries.append({
            "name": ind, "n": len(with_y), "revenue": rev, "cyclical": ind in RULES["cyclical_industries"],
            "mcap": sum(Q[c].get("total_mcap") or 0 for c in codes),
            "yoy_median": med(ys), "yoy_weighted": r1((rev / rev_old - 1) * 100 if rev_old > 0 else None, 2),
            "share_ge20": r1(100 * sum(1 for c in with_y if fl[c]["revenue_yoy_pct"] >= 20) / len(with_y)),
            "share_accel": r1(100 * sum(1 for c in both if fl[c]["revenue_yoy_pct"] > fp[c]["revenue_yoy_pct"]) / len(both))
            if both else None, "n_accel_base": len(both),
            "gm_change_median": med(gm), "n_gm": len(gm),
            "ocf_np_median": med(on), "n_ocf": len(on),
        })
    industries.sort(key=lambda x: -x["revenue"])

    # ---- growth screen
    floor = RULES["revenue_floor_per_quarter"] * quarters_in(P["latest"])
    excl = {"st": 0, "financial": 0, "small": 0, "no_quote": 0}
    screen = []
    for code, f in fl.items():
        y = f.get("revenue_yoy_pct")
        if y is None or y < RULES["growth_yoy_min"] or (f.get("revenue") or 0) <= 0:
            continue
        q = Q.get(code)
        if not q:
            excl["no_quote"] += 1
            continue
        name = q.get("name") or f.get("name") or ""
        if f.get("risk_warning") or "ST" in name.upper():
            excl["st"] += 1
            continue
        ind = q.get("industry") or f.get("industry") or ""
        if not f.get("has_balance_sheet") or any(w in ind for w in FIN_INDUSTRY_WORDS):
            excl["financial"] += 1
            continue
        if f["revenue"] < floor:
            excl["small"] += 1
            continue
        screen.append(code)
    lookup, dropped = full_lookup(Q, fl, floor)

    rows = []
    for code in screen:
        f, q, p, yo = fl[code], Q[code], fp.get(code, {}), fy.get(code, {})
        np_, ocf, ry = f.get("parent_net_profit"), f.get("operating_cash_flow"), f["revenue_yoy_pct"]
        g = {}
        # 1 OCF / NP
        on = ratio(ocf, np_) if (np_ or 0) > 0 else None
        if ocf is None or np_ is None:
            g["ocf"] = ("na", "无数据", "—")
        elif np_ <= 0:
            g["ocf"] = ("na", f"归母净利润≤0，比值不适用（经营现金流 {ocf / 1e8:.2f} 亿）", "亏损")
        else:
            g["ocf"] = ("warn" if ocf < 0 or on < RULES["ocf_np_warn"] else "ok", f"{on:.2f}")
        # 2 receivables, 3 inventory (balance sheet vs same date last year)
        for key, fld, cn, gap in (("ar", "accounts_receivable", "应收", RULES["ar_gap_pp"]),
                                  ("inv", "inventory", "存货", RULES["inv_gap_pp"])):
            gr = growth(f.get(fld), yo.get(fld))
            if gr is None:
                g[key] = ("na", "无可比数据", "—")
            else:
                g[key] = ("warn" if gr > ry + gap else "ok", f"{cn} {gr:+.0f}% vs 营收 {ry:+.0f}%", f"{gr:+.0f}%")
        # 4 goodwill / equity
        eq, gw = f.get("parent_equity"), f.get("goodwill") or 0
        if eq is None:
            g["goodwill"] = ("na", "无数据", "—")
        elif eq <= 0:
            g["goodwill"] = ("warn", "归母权益≤0", "权益≤0")
        else:
            v = 100 * gw / eq
            g["goodwill"] = ("warn" if v > RULES["goodwill_equity_warn"] else "ok", f"{v:.1f}%")
        # 5 pledge
        pr = pledge.get(code)
        g["pledge"] = ("ok", "无质押记录", "无") if pr is None else (
            "warn" if pr > RULES["pledge_warn"] else "ok", f"{pr:.1f}%")
        # 6 net holder change (6 months), % of total shares
        h, ts = hold.get(code), q.get("total_shares")
        net_pct = None
        if not h:
            g["selling"] = ("ok", "无增减持记录", "无")
        elif not ts:
            g["selling"] = ("na", "缺总股本", "—")
        else:
            net_pct = 100 * (h["exec"] + h["major"]) / ts
            g["selling"] = ("warn" if net_pct <= -RULES["net_sell_warn"] else "ok",
                            f"净{'减' if net_pct < 0 else '增'}持 {abs(net_pct):.2f}% 总股本"
                            f"（董监高 {100 * h['exec'] / ts:+.2f}%，股东 {100 * h['major'] / ts:+.2f}%）", f"{net_pct:+.2f}%")
        # 7 unlocks next 90 days, % of current float shares
        u, fs = unl.get(code), q.get("float_shares")
        unl_pct = None
        if not u:
            g["unlock"] = ("ok", "无", "无")
        else:
            unl_pct = 100 * u["tradable"] / fs if fs else u["ratio_float"]
            mc = q.get("total_mcap")
            g["unlock"] = ("warn" if unl_pct > RULES["unlock_float_warn"] else "ok",
                           f"{unl_pct:.1f}% 流通股，{u['first']} 起，约 {u['value'] / 1e8:.1f} 亿"
                           + (f"（{100 * u['value'] / mc:.1f}% 总市值）" if mc else ""), f"{unl_pct:.1f}%")
        # 8 regulatory titles (12 months)
        rh = reg.get(code, [])
        if rh:
            kinds = {}
            for r in rh:
                kinds[REG_KIND_CN[r["kind"]]] = kinds.get(REG_KIND_CN[r["kind"]], 0) + 1
            g["reg"] = ("warn", f"{len(rh)} 条：" + "、".join(f"{k}{v}" for k, v in kinds.items()), f"{len(rh)} 条")
        else:
            g["reg"] = ("ok", "无标题命中", "无")
        # 9 valuation percentile (shortlist only)
        vv = valuation.get(code)
        pe_pct = pb_pct = None
        if vv is None:
            g["val"] = ("na", "未取历史（不在估值短名单）", "未取")
        else:
            pe, pb = vv.get("pe") or {}, vv.get("pb") or {}
            pe_pct, pb_pct = pe.get("percentile"), pb.get("percentile")
            txt = (f"PE {pe.get('latest'):.1f} 分位 {pe_pct:.0f}%" if pe_pct is not None else "PE≤0 无分位")
            txt += f"；PB 分位 {pb_pct:.0f}%" if pb_pct is not None else ""
            txt += f"（{vv.get('first')}~{vv.get('last')}）"
            g["val"] = ("na", txt, "PE≤0") if pe_pct is None else ("warn" if pe_pct >= RULES["pe_pct_high"] else "ok", txt, f"{pe_pct:.0f}%")
        # 10 latest 业绩预告
        fc = fcast.get(code)
        if not fc:
            g["fc"] = ("na", "近 6 个月无归母净利润预告", "无")
        else:
            rng = ""
            if fc.get("yoy_low_pct") is not None or fc.get("yoy_high_pct") is not None:
                lo, hi = fc.get("yoy_low_pct"), fc.get("yoy_high_pct")
                rng = f"同比 {lo if lo is not None else '—'}% ~ {hi if hi is not None else '—'}%"
            elif fc.get("profit_low") is not None:
                rng = f"{fc['profit_low'] / 1e8:.2f} ~ {(fc.get('profit_high') or fc['profit_low']) / 1e8:.2f} 亿"
            g["fc"] = ("warn" if fc.get("forecast_type") in NEG_FORECAST else "ok",
                       f"{fc.get('report_period')} {fc.get('forecast_type_cn') or ''} {rng}".strip(), fc.get("forecast_type_cn") or "—")

        # ---- 价值 / 博弈 label
        cyc = (q.get("industry") or "") in RULES["cyclical_industries"]
        game, value_fail = [], []
        if np_ is None or np_ <= 0:
            game.append("年初至今归母净利润≤0")
        if pe_pct is not None and pe_pct >= RULES["pe_pct_high"]:
            game.append(f"PE 分位 {pe_pct:.0f}% ≥ {RULES['pe_pct_high']:.0f}%")
        if unl_pct is not None and unl_pct > RULES["unlock_float_warn"]:
            game.append(f"90 天内解禁 {unl_pct:.1f}% 流通股")
        if net_pct is not None and net_pct <= -RULES["net_sell_warn"]:
            game.append(f"6 个月净减持 {abs(net_pct):.2f}% 总股本")
        if rh:
            game.append(f"12 个月监管类标题 {len(rh)} 条")
        cyc_why = (f"周期行业（东财：{q.get('industry')}）：盈利主要随商品价格或产能周期变化，"
                   f"周期高点时利润高、PE 反而低，所以不标偏价值，也不参与研究优先级排序")
        if game:
            label, reasons = "偏博弈", game + ([cyc_why] if cyc else [])
        elif cyc:
            label, reasons = "中性", [cyc_why]
        else:
            if on is None or on < RULES["value_ocf_np_min"]:
                value_fail.append(f"经营现金流/净利润 {'—' if on is None else f'{on:.2f}'} < {RULES['value_ocf_np_min']}")
            if pe_pct is None:
                value_fail.append("无 PE 分位（未取历史或 PE≤0）")
            elif pe_pct > RULES["value_pe_pct_max"]:
                value_fail.append(f"PE 分位 {pe_pct:.0f}% > {RULES['value_pe_pct_max']:.0f}%")
            if value_fail:
                label, reasons = "中性", value_fail
            else:
                label, reasons = "偏价值", [f"盈利，经营现金流/净利润 {on:.2f}", f"PE 分位 {pe_pct:.0f}%", "无监管类标题命中"]

        acc = (ry - p["revenue_yoy_pct"]) if p.get("revenue_yoy_pct") is not None else None
        rows.append({
            "code": code, "name": q.get("name") or f.get("name"), "industry": q.get("industry"), "board": q.get("board"),
            "revenue": f["revenue"], "rev_yoy": r1(ry), "rev_yoy_prev": r1(p.get("revenue_yoy_pct")), "accel": r1(acc),
            "np": np_, "np_yoy": r1(f.get("net_profit_yoy_pct")), "gm": r1(f.get("gross_margin_pct")),
            "gm_chg": r1(f["gross_margin_pct"] - yo["gross_margin_pct"])
            if f.get("gross_margin_pct") is not None and yo.get("gross_margin_pct") is not None else None,
            "ocf_np": r1(on, 2), "mcap": q.get("total_mcap"), "pe_ttm": r1(q.get("pe_ttm")), "pb": r1(q.get("pb"), 2),
            "pe_pct": pe_pct, "pb_pct": pb_pct,
            # display only: never used in labels, gates, priority or the 对比池 score
            "price": q.get("price"), "px_date": q.get("as_of"),
            "gates": {k: {"s": t[0], "v": t[1], "sv": t[2] if len(t) > 2 else t[1]} for k, t in g.items()},
            "warn_n": sum(1 for k in RISK_GATES if g[k][0] == "warn"),
            "label": label, "reasons": reasons, "cyclical": cyc,
            "reg_items": [{"date": r.get("date"), "title": r.get("title_cn"), "url": r.get("url"),
                           "kind": REG_KIND_CN[r["kind"]]} for r in rh[:3]],
        })

    # ---- research priority: eligible = profitable + <= priority_max_warn risk-gate warnings
    base = [r for r in rows if (r["np"] or 0) > 0 and r["ocf_np"] is not None and r["warn_n"] <= RULES["priority_max_warn"]]
    elig = [r for r in base if not r["cyclical"]]
    # cyclical industries: listed separately by growth, never ranked against structural growers
    cyc_list = [r["code"] for r in sorted((r for r in base if r["cyclical"]), key=lambda r: -(r["rev_yoy"] or 0))][:RULES["cyclical_top"]]
    pg = pct_rank({r["code"]: r["rev_yoy"] for r in elig})
    pa = pct_rank({r["code"]: r["accel"] for r in elig})
    po = pct_rank({r["code"]: r["ocf_np"] for r in elig})
    pre = sorted(elig, key=lambda r: -statistics.mean([x for x in (pg.get(r["code"]), pa.get(r["code"]),
                                                                   po.get(r["code"])) if x is not None]))
    shortlist = [r["code"] for r in pre[:RULES["shortlist_max"]]]
    sl = [r for r in elig if r["code"] in set(shortlist)]
    vraw = {r["code"]: (r["pe_pct"] if r["pe_pct"] is not None else r["pb_pct"]) for r in sl}
    pv = {k: round(100 - v, 1) for k, v in pct_rank(vraw).items()}  # lower valuation percentile => higher score
    pg2 = pct_rank({r["code"]: r["rev_yoy"] for r in sl})
    pa2 = pct_rank({r["code"]: r["accel"] for r in sl})
    po2 = pct_rank({r["code"]: r["ocf_np"] for r in sl})
    prio = []
    for r in sl:
        fs = {"growth": pg2.get(r["code"]), "accel": pa2.get(r["code"]), "ocf": po2.get(r["code"]), "val": pv.get(r["code"])}
        av = [v for v in fs.values() if v is not None]
        prio.append({"code": r["code"], "score": round(statistics.mean(av), 1) if av else None, "f": fs,
                     "val_basis": "PE" if r["pe_pct"] is not None else ("PB" if r["pb_pct"] is not None else None),
                     "n_factors": len(av)})
    prio.sort(key=lambda x: -(x["score"] or 0))
    prio = prio[:RULES["priority_top"]]
    for i, p in enumerate(prio, 1):
        p["rank"] = i

    rows.sort(key=lambda r: -(r["rev_yoy"] or 0))
    n_fin = sum(1 for c in Q if c in fl and fl[c].get("revenue") is not None)
    all_y = [fl[c].get("revenue_yoy_pct") for c in Q if c in fl]
    return {
        "industries": industries, "screen": rows, "priority": prio, "shortlist": shortlist, "cyclical_list": cyc_list,
        "counts": {"quotes": len(Q), "with_fin": n_fin, "industries_all": len(groups), "industries": len(industries),
                   "screen_raw": len(screen) + sum(excl.values()), "screen": len(rows), "excluded": excl, "dropped": dropped,
                   "eligible": len(elig), "eligible_cyclical": len(base) - len(elig), "shortlist": len(shortlist), "valuation_ok": sum(1 for c in shortlist if c in valuation),
                   "priority": len(prio), "regulatory_rows": len(snap["regulatory"]),
                   "reg_hit_rows": sum(len(v) for v in reg.values())},
        "market": {"yoy_median": med(all_y),
                   "share_ge20": r1(100 * sum(1 for y in all_y if y is not None and y >= 20) / max(1, sum(1 for y in all_y if y is not None)))},
        "revenue_floor": floor, "lookup": lookup,
    }


# 全市场速查: every quoted stock with its key numbers and EVERY screen rule it fails (not only the first one), so a code
# that is not in ② can still be found and the reason is visible. Display only. Reason keys (page text in LK_WHY):
#   no_fin    no financial row for the latest report date (new listing, suspended, late report)
#   no_rev    revenue missing or <= 0
#   no_yoy    revenue YoY missing (no comparable period, e.g. listed within a year)
#   growth    revenue YoY < growth_yoy_min
#   st        ST / *ST / risk-warning board
#   financial bank / insurer / broker / other financial, or no general-template balance sheet
#   small     YTD revenue below the floor
# Rows: [code, name, industry, board, price, mcap, pe_ttm, pb, revenue, rev_yoy, parent_np, np_yoy, gross_margin,
#        ocf / np (NP > 0 only), reasons "a,b" ("" = in the ② screen)]
LOOKUP_COLS = ["code", "name", "industry", "board", "price", "mcap", "pe", "pb", "rev", "rev_yoy", "np", "np_yoy", "gm", "ocf_np", "why"]


def full_lookup(Q, fl, floor):
    rows, cnt = [], {}
    for code in sorted(Q):
        q, f = Q[code], fl.get(code)
        why = []
        name = q.get("name") or (f or {}).get("name") or ""
        ind = q.get("industry") or (f or {}).get("industry") or ""
        if not f:
            why.append("no_fin")
        else:
            rev, y = f.get("revenue"), f.get("revenue_yoy_pct")
            if rev is None or rev <= 0:
                why.append("no_rev")
            elif y is None:
                why.append("no_yoy")
            elif y < RULES["growth_yoy_min"]:
                why.append("growth")
            if f.get("risk_warning") or "ST" in name.upper():
                why.append("st")
            if not f.get("has_balance_sheet") or any(w in ind for w in FIN_INDUSTRY_WORDS):
                why.append("financial")
            if rev is not None and 0 < rev < floor:
                why.append("small")
        for w in why or ["screen"]:
            cnt[w] = cnt.get(w, 0) + 1
        f = f or {}
        np_, ocf = f.get("parent_net_profit"), f.get("operating_cash_flow")
        rows.append([code, name, ind, q.get("board"), q.get("price"), q.get("total_mcap"), r1(q.get("pe_ttm")), r1(q.get("pb"), 2),
                     f.get("revenue"), r1(f.get("revenue_yoy_pct")), np_, r1(f.get("net_profit_yoy_pct")), r1(f.get("gross_margin_pct")),
                     r1(ocf / np_, 2) if ocf is not None and np_ and np_ > 0 else None, ",".join(why)])
    # amounts in 亿 with 2 decimals keep the page small (the page shows 亿 anyway)
    for r in rows:
        for i in (5, 8, 10):
            r[i] = None if r[i] is None else round(r[i] / 1e8, 2)
    return {"cols": LOOKUP_COLS, "rows": rows, "unit": "mcap / rev / np in 亿元"}, cnt


# ----------------------------------------------------------------------------- build
def latest_snap():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "snap_*.json")))
    if not files:
        log("ERROR: no data/cn/snap_*.json found; run `python -m cn fetch` first")
        sys.exit(1)
    return files[-1]


def build(path=None):
    path = path or latest_snap()
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    res = analyze(snap)
    qd = next((q.get("as_of") for q in snap["quotes"] if q.get("as_of")), None)
    payload = {
        "built_at": dt.datetime.now(em.CST).strftime("%Y-%m-%d %H:%M"), "fetched_at": snap["fetched_at"],
        "quote_date": qd, "periods": snap["periods"], "coverage": snap.get("coverage"), "rules": RULES,
        "unlock_window": snap.get("unlock_window"), "reg_window": snap.get("reg_window"),
        "pledge_date": next((p.get("stats_date") for p in snap["pledge"] if p.get("stats_date")), None),
        "reg_kinds": sorted(REG_KIND_CN.values()),
        **res,
    }
    payload.pop("shortlist")
    # evaluation cards: merge the latest cards_<ts>.json (match by code; codes no longer on the card list are ignored)
    from . import cards as cardmod
    cpath = cardmod.latest_cards()
    cdoc = None
    if cpath:
        with open(cpath, encoding="utf-8") as f:
            cdoc = json.load(f)
    want = cardmod.card_codes(res)
    # 52-week qfq range: attached AFTER analyze() so it cannot reach any score / label / rank (display only)
    from . import ranges as rngmod
    rdoc, rpath = rngmod.load_latest()
    if rdoc and rdoc.get("snap") != os.path.basename(path):
        log(f"NOTE: {os.path.basename(rpath)} was fetched for {rdoc.get('snap')}, not {os.path.basename(path)}; "
            f"items with a different quote date are flagged")
    rngmod.attach(res["screen"], rdoc, qd)
    payload["ranges_meta"] = {"file": os.path.basename(rpath) if rdoc else None, "fetched_at": (rdoc or {}).get("fetched_at"),
                              "as_of": (rdoc or {}).get("as_of"), "window_start": (rdoc or {}).get("window_start"),
                              "item_bases": (rdoc or {}).get("item_bases"), "complete": (rdoc or {}).get("complete"),
                              "rules": rngmod.RANGE_RULES, "n_screen": len(res["screen"]),
                              "n_price": sum(1 for r in res["screen"] if r.get("price") is not None),
                              "n_range": sum(1 for r in res["screen"] if (r.get("rng") or {}).get("hi52") is not None),
                              "n_pos": sum(1 for r in res["screen"] if r.get("pos52") is not None)}
    # tracked holders in the top-10 float holders: attached AFTER analyze(), display only (never in any score / label / rank)
    from . import holders as hmod
    hdoc, hpath = hmod.load_latest()
    hmod.attach(res["screen"], hdoc)
    payload["inst_meta"] = hmod.meta(hdoc, hpath, res["screen"])
    payload["cards"] = cardmod.card_payload(snap, res, cdoc, RULES)
    for c in payload["cards"]:
        c["inst"] = {k: hmod.compact(v) for k, v in hmod.derive_code(hdoc, c["code"]).items()}
    # 现金流估值 inputs: attached AFTER analyze(), display only (never in any score / label / rank / bkScore);
    # the scenario DCF itself is computed in the page so the user can change the parameters
    from . import valuation as vmod
    vdoc, vpath = vmod.load_latest()
    if vdoc and vdoc.get("snap") != os.path.basename(path):
        log(f"NOTE: {os.path.basename(vpath)} was fetched for {vdoc.get('snap')}, not {os.path.basename(path)}")
    vmod.attach(res["screen"], vdoc, snap)
    for c in payload["cards"]:
        c["dcf_g"] = vmod.consensus_growth(c.get("consensus"), snap["periods"]["latest"])
        # one-off guard (c): consensus EPS next year vs current-year estimate (display only)
        c["dcf_drop"] = vmod.eps_drop(c.get("consensus"), snap["periods"]["latest"])
    payload["dcf_meta"] = vmod.meta(vdoc, vpath, res["screen"], payload["cards"])
    # 价值通道 (value channel, `python -m cn quality`): a second entry that does not require growth; optional block
    try:
        from . import quality as qmod
        qdoc, qpath = qmod.load_latest()
        if qdoc:
            qdoc["_path"] = qpath
        payload["value"] = qmod.channel(snap, res, qdoc, ((vdoc or {}).get("rf") or {}).get("value"))
        pv = payload["value"]
        if pv.get("file"):
            log(f"value channel: {pv['file']} pass={pv['n_pass']} ranked={pv['n_ranked']} complete={pv['complete']}")
    except Exception as e:  # noqa: BLE001
        log(f"WARNING: value channel skipped ({type(e).__name__}: {e})")
        payload["value"] = {"file": None}
    # 估值台账 (default-parameter valuation history, display only): written by `python -m cn ledger`, embedded compactly.
    # Optional: the page works without it (a later step owns cn/ledger.py)
    try:
        from . import ledger as lmod
        payload["ledger"] = lmod.page_payload([c["code"] for c in payload["cards"]], [r["code"] for r in res["screen"]],
                                              os.path.basename(path), os.environ.get("CN_LEDGER_FILE") or lmod.LEDGER_FILE)
    except Exception as e:  # noqa: BLE001 - optional block, never break the build
        log(f"WARNING: ledger skipped ({type(e).__name__}: {e})")
        payload["ledger"] = {}
    if payload["ledger"].get("file"):
        try:   # 前向跟踪 of the tags (aggregates only, display only); optional
            from . import forward as fmod
            payload["ledger"]["fwd"] = fmod.page_payload(os.environ.get("CN_LEDGER_FILE") or lmod.LEDGER_FILE)
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: forward tracking skipped ({type(e).__name__}: {e})")
    have = set((cdoc or {}).get("cards", {}))
    payload["cards_meta"] = {"file": os.path.basename(cpath) if cpath else None, "fetched_at": (cdoc or {}).get("fetched_at"),
                             "snap": (cdoc or {}).get("snap"), "rules": cardmod.CARD_RULES,
                             "n": len(want), "n_extra": sum(1 for c in want if c in have),
                             "stale_ignored": len(have - set(want))}
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    if "/*__DATA__*/null" not in tpl:
        log("ERROR: template placeholder /*__DATA__*/null not found")
        sys.exit(1)
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    # CN_BUILD_OUT / CN_LEDGER_FILE: test-only overrides (render a page from a temporary ledger without touching site/)
    out = os.environ.get("CN_BUILD_OUT") or os.path.join(SITE_DIR, "index.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", data, 1))
    c = res["counts"]
    log(f"built {os.path.relpath(out, ROOT)} from {os.path.basename(path)}: quotes={c['quotes']} with_fin={c['with_fin']} "
        f"industries={c['industries']}/{c['industries_all']} screen={c['screen']} excluded={c['excluded']} "
        f"eligible={c['eligible']} shortlist={c['shortlist']} valuation={c['valuation_ok']} priority={c['priority']} "
        f"periods={snap['periods']}")
    cm = payload["cards_meta"]
    log(f"cards: {cm['n']} rendered, {cm['n_extra']} with F10 extras from {cm['file']}, stale codes ignored={cm['stale_ignored']}")
    rm = payload["ranges_meta"]
    ct = payload["cards"]
    log(f"prices: screen {rm['n_price']}/{rm['n_screen']}; 52w ranges {rm['n_range']}/{rm['n_screen']} (pos52 {rm['n_pos']}) "
        f"from {rm['file']}; cards with range {sum(1 for c in ct if (c['range'].get('rng') or {}).get('hi52') is not None)}/{len(ct)}, "
        f"with target median (>= {cardmod.CARD_RULES['target_min_org']} institutions) {sum(1 for c in ct if c['target']['median'] is not None)}/{len(ct)}")
    im = payload["inst_meta"]
    if im.get("file"):
        log(f"holders: {im['file']} periods {im['periods'][0]}..{im['periods'][-1]} partial "
            f"{[(p['period'], p['n_reported']) for p in im['partial']]}; screen rows with a top-10 list at {im['periods'][-1]}: "
            f"{im['n_reported_screen']}/{im['n_screen']}")
        for k, v in im["counts"].items():
            a, b = v["all"], v["value"]
            log(f"  {k}: in_top10_latest={a['in_latest']} new={a['new']} up={a['up']} down={a['down']} out={a['out']} "
                f"| value-label: in_latest={b['in_latest']} new={b['new']} up={b['up']} new_or_up={b['new'] + b['up']}")
    else:
        log("holders: no data/cn/holders_*.json (run `python -m cn holders`); 机构动向 column empty")
    vm = payload["dcf_meta"]
    if vm.get("file"):
        card_codes_ = {c["code"] for c in ct}
        crow = [r for r in res["screen"] if r["code"] in card_codes_]
        bm = lambda r: vmod.base_margin(r["dcf"]["m"], r["cyclical"])[0] if r.get("dcf") else None
        neg = lambda rows: sum(1 for r in rows if bm(r) is not None and bm(r) <= 0)
        m3 = lambda rows: sum(1 for r in rows if r.get("dcf") and sum(1 for m in r["dcf"]["m"][-3:] if m is not None) == 3)
        log(f"valuation: {vm['file']} rf={vm['rf']['value']} ({vm['rf']['date']}); screen ttm_rev={vm['n_r0']}/{vm['n_screen']} "
            f"fcf_3y={m3(res['screen'])} balance={vm['n_bal']} base_margin<=0={neg(res['screen'])}; cards fcf_3y={m3(crow)}/{len(ct)} "
            f"base_margin<=0={neg(crow)} consensus_growth={vm['n_cons']}")
        drops = {c["code"]: c.get("dcf_drop") for c in ct}

        def mbs(r, b):
            d = r["dcf"]
            return vmod.choose_basis(d["m"], d.get("mt"), r["cyclical"], b, vmod.one_off_guard(d, drops.get(r["code"])))

        def tally(rows):
            t = collections.Counter()
            for r in rows:
                if not r.get("dcf"):
                    continue
                a, b = mbs(r, "auto"), mbs(r, "avg3")
                t["cyclical" if a["why"] == "auto_cyc" else a["used"]] += 1
                t["guarded"] += bool(a["guard"])
                t["neg_to_pos"] += b["m_end"] is not None and b["m_end"] <= 0 and a["m_end"] is not None and a["m_end"] > 0
            return t
        ts, tc = tally(res["screen"]), tally(crow)
        fmt = lambda t: " ".join(f"{k}={t[k]}" for k in ("ttm", "ramp", "avg3", "fy", "cyclical", "guarded", "neg_to_pos"))
        log(f"margin basis (default auto): screen {fmt(ts)} / {len(res['screen'])}; cards {fmt(tc)} / {len(ct)}")
    else:
        log("valuation: no data/cn/valuation_*.json (run `python -m cn valuation`); 现金流估值 shows N/A")
    lm = payload["ledger"] or {}
    if lm.get("file"):
        log(f"ledger: {lm['file']} records={lm['n_records']} runs={len(lm['runs'])} latest={lm['latest']['date']} "
            f"({lm['latest']['snap']}) matches_snapshot={lm['matches_snap']} cards_embedded={len(lm['cards'])} rows_embedded={len(lm['all'])} "
            f"bytes={len(json.dumps(lm, ensure_ascii=False).encode('utf-8'))}")
    else:
        log("ledger: no data/cn/ledger/cn_ledger.jsonl (run `python -m cn ledger`); 估值台账 empty")
    return out
