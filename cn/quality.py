"""价值通道 (value channel): a second entry to the A-share funnel that does NOT require revenue growth >= 20%.
Research tooling, not investment advice. The channel lists companies with long-run quality (multi-year ROE, free cash
flow, cash conversion, moderate leverage) and ranks them on quality + simple valuation yields. It does not change the ②
growth screen, the 价值 / 博弈 label, warn_n, the ③ priority list or the 对比池 score.

    python -m cn quality   market-wide fiscal-year fundamentals (5 years) + cash dividends -> data/cn/quality_<ts>.json

Sources (Eastmoney datacenter-web, market-wide paged queries; cached, ttl 30 days for closed fiscal years):
  em.financials(FY-end) = RPT_LICO_FN_CPD (营业总收入, 归母净利润, 加权 ROE WEIGHTAVG_ROE) + RPT_DMSK_FN_CASHFLOW
      (经营现金流 NETCASH_OPERATE, 资本开支 CONSTRUCT_LONG_ASSET) + RPT_F10_FINANCE_GBALANCE (归母权益, 货币资金,
      短期借款 + 长期借款 + 应付债券 = 有息负债, 商誉), fiscal years FY(y-4)..FY(y), y = last completed fiscal year.
  RPT_SHAREBONUS_DET (分红送配, verified 2026-10-27): PRETAX_BONUS_RMB = 税前每 10 股派现 (元), TOTAL_SHARES at the plan,
      ASSIGN_PROGRESS (plans containing 取消 / 未通过 / 不分配 are ignored); every plan whose REPORT_DATE falls in the
      fiscal year (interim + final) is summed: cash = PRETAX_BONUS_RMB / 10 x TOTAL_SHARES.
Channel rules (VALUE_RULES; fiscal-year data only, so a company is judged on 5 completed years, not one good half-year):
  entry: has a quote, not ST, not financial (banks / insurers / brokers need a different model: not covered here),
         latest YTD revenue >= the ② revenue floor, latest YTD parent net profit > 0, ROE in >= 4 of 5 fiscal years,
         median ROE >= 10%, minimum ROE >= 5%, FCF (OCF - capex) > 0 in >= 4 of 5 fiscal years, 5-year sum OCF / sum
         parent NP >= 0.8, revenue FY(y) >= FY(y-4) (not shrinking), interest-bearing debt / parent equity <= 0.8,
         goodwill / parent equity <= 30% (latest fiscal year).
  ranking (non-cyclical passers only; cyclical industries are listed separately, same rule as ③): equal-weight mean of
         percentiles inside the channel of median ROE, minimum ROE (stability), cash conversion, FCF yield (5-year mean FCF /
         total mcap), earnings yield (1 / PE TTM, PE > 0) and dividend yield (latest fiscal year cash dividends / total mcap).
  display only (not in the score): reverse DCF "现价隐含增速" with the same engine as the page (cn/valuation.dcf_reverse):
         r0 = FY(y) revenue, m = mean FCF margin of the last 3 fiscal years (> 0 only), r = rf + 5%, gT = min(2.5%, rf),
         net cash = cash - interest-bearing debt (FY(y) balance sheet), shares = snapshot total shares; compared with the
         4-year revenue CAGR FY(y-4) -> FY(y). The difference is a description of what the price assumes, not a signal.
stdout is English only.
"""
import datetime as dt
import glob
import json
import os
import statistics
import time

from . import em

ROOT = em.ROOT
DATA_DIR = os.path.join(ROOT, "data", "cn")
# n_fy: fiscal years fetched (10, for the long-term dividend view); the value channel itself is judged on the last 5 (RULE_YEARS)
RULE_YEARS = 5
VALUE_RULES = {"n_fy": 10, "roe_years_min": 4, "roe_median_min": 10.0, "roe_min_min": 5.0, "fcf_pos_min": 4,
               "conv_min": 0.8, "de_max": 0.8, "goodwill_max_pct": 30.0, "top": 30, "erp": 5.0, "gT": 2.5,
               "recent_np_drop": -30.0,     # display flag: latest YTD parent NP YoY <= -30% (not a rule, not in the score)
               "sleep": 0.3, "ttl_closed": 30.0}
DIV_COLS = "SECURITY_CODE,REPORT_DATE,PRETAX_BONUS_RMB,TOTAL_SHARES,ASSIGN_PROGRESS"
DIV_SKIP = ("取消", "未通过", "不分配", "终止")
FY_KEYS = ("revenue", "parent_net_profit", "roe_pct", "operating_cash_flow", "capex", "parent_equity", "cash",
           "interest_bearing_debt", "goodwill", "gross_margin_pct")
SHORT = {"revenue": "rev", "parent_net_profit": "np", "roe_pct": "roe", "operating_cash_flow": "ocf", "capex": "cx",
         "parent_equity": "eq", "cash": "cash", "interest_bearing_debt": "ibd", "goodwill": "gw", "gross_margin_pct": "gm"}


def log(msg):
    print(msg, flush=True)


def fiscal_years(latest):
    d = dt.date.fromisoformat(latest)
    y = d.year if (d.month, d.day) == (12, 31) else d.year - 1
    return [f"{x}-12-31" for x in range(y - VALUE_RULES["n_fy"] + 1, y + 1)]


def latest_file():
    fs = sorted(glob.glob(os.path.join(DATA_DIR, "quality_*.json")))
    return fs[-1] if fs else None


def load_latest():
    p = latest_file()
    if not p:
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), p
    except (OSError, ValueError) as e:
        log(f"WARNING: {os.path.basename(p)} unreadable ({type(e).__name__}); value channel skipped")
        return None, p


def fetch_quality(snap_path=None):
    from .build import latest_snap
    snap_path = snap_path or latest_snap()
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    fys = fiscal_years(snap["periods"]["latest"])
    t0 = dt.datetime.now(em.CST)
    log(f"quality: fiscal years {fys[0][:4]}..{fys[-1][:4]} market-wide + cash dividends")
    items, src, ok_all = {}, {}, True
    for fy in fys:
        rows = em.financials(fy, ttl=VALUE_RULES["ttl_closed"])
        n = 0
        for r in rows:
            if r.get("revenue") is None and r.get("parent_net_profit") is None:
                continue
            items.setdefault(r["code"], {})[fy[:4]] = {SHORT[k]: r.get(k) for k in FY_KEYS}
            n += 1
        src[f"financials {fy}"] = {"rows": len(rows), "codes": n}
        ok_all = ok_all and n > 0
        log(f"  financials {fy}: rows={len(rows)} with_data={n}")
        time.sleep(VALUE_RULES["sleep"])
        y = fy[:4]
        drows, ok = em.dc_report("RPT_SHAREBONUS_DET", DIV_COLS, f"(REPORT_DATE>='{y}-01-01')(REPORT_DATE<='{y}-12-31')",
                                 "SECURITY_CODE,REPORT_DATE", "1,1", ttl=VALUE_RULES["ttl_closed"], label=f"dividends {y}")
        plans = {}
        for d in drows:
            c, rd = d.get("SECURITY_CODE"), em.day(d.get("REPORT_DATE"))
            if not em.is_a_code(c) or any(w in (d.get("ASSIGN_PROGRESS") or "") for w in DIV_SKIP):
                continue
            per10, sh = em.num(d.get("PRETAX_BONUS_RMB")), em.num(d.get("TOTAL_SHARES"))
            if per10 and sh:
                plans[(c, rd)] = per10 / 10 * sh            # one plan per (code, report date)
        cash = {}
        for (c, _), v in plans.items():
            cash[c] = cash.get(c, 0.0) + v
        for c, v in cash.items():
            items.setdefault(c, {}).setdefault(y, {})["div"] = v
        src[f"dividends {y}"] = {"rows": len(drows), "ok": ok, "codes": len(cash)}
        ok_all = ok_all and ok
        log(f"  dividends {y}: rows={len(drows)} codes_with_cash={len(cash)} ok={ok}")
        time.sleep(VALUE_RULES["sleep"])
    doc = {"fetched_at": t0.strftime("%Y-%m-%d %H:%M"), "snap": os.path.basename(snap_path), "fiscal_years": [f[:4] for f in fys],
           "rules": VALUE_RULES, "complete": ok_all, "sources": src, "items": items, "http": dict(em.STATS)}
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"quality_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    log(f"saved {os.path.relpath(path, ROOT)}: codes={len(items)} complete={ok_all} http={em.STATS}")
    return path


# ----------------------------------------------------------------------------- analysis (offline, used by cn build)
def _r(x, d=2):
    return None if x is None else round(x, d)


def metrics(it, years):
    """Multi-year quality metrics of one company from its fiscal-year items (oldest first)."""
    ys = [it.get(y) or {} for y in years]
    roe = [y.get("roe") for y in ys]
    rv = [x for x in roe if x is not None]
    fcf = [None if y.get("ocf") is None or y.get("cx") is None else y["ocf"] - y["cx"] for y in ys]
    fv = [x for x in fcf if x is not None]
    so = sum(y["ocf"] for y in ys if y.get("ocf") is not None and y.get("np") is not None)
    sn = sum(y["np"] for y in ys if y.get("ocf") is not None and y.get("np") is not None)
    r0, ra = ys[-1].get("rev"), ys[0].get("rev")
    cagr = ((r0 / ra) ** (1 / (len(years) - 1)) - 1) * 100 if r0 and ra and r0 > 0 and ra > 0 else None
    eq, ibd, gw = ys[-1].get("eq"), ys[-1].get("ibd"), ys[-1].get("gw")
    mg = [((y["ocf"] - y["cx"]) / y["rev"]) for y in ys[-3:] if y.get("ocf") is not None and y.get("cx") is not None and (y.get("rev") or 0) > 0]
    pay = [y["div"] / y["np"] for y in ys if y.get("div") and (y.get("np") or 0) > 0]
    return {"roe": roe, "n_roe": len(rv), "roe_med": statistics.median(rv) if rv else None, "roe_min": min(rv) if rv else None,
            "fcf": fcf, "fcf_pos": sum(1 for x in fv if x > 0), "n_fcf": len(fv), "fcf_avg": statistics.mean(fv) if fv else None,
            "conv": so / sn if sn > 0 else None, "cagr": cagr, "r0": r0,
            "de": (ibd or 0) / eq if eq and eq > 0 else None, "eq": eq, "gw_pct": 100 * (gw or 0) / eq if eq and eq > 0 else None,
            "net_cash": (ys[-1]["cash"] - (ibd or 0)) if ys[-1].get("cash") is not None else None,
            "m3": statistics.mean(mg) if mg else None, "div": ys[-1].get("div") or 0.0,
            "payout": statistics.median(pay) if pay else None}


def implied_growth(m, q, rf):
    """Reverse DCF g0 under the page engine (display only). None when inputs are missing or the margin is <= 0."""
    from . import valuation as V
    if rf is None or not m["r0"] or not m["m3"] or m["m3"] <= 0 or m["net_cash"] is None or not q.get("total_shares") or not q.get("price"):
        return None, "no_input"
    r, gT = (rf + VALUE_RULES["erp"]) / 100, min(VALUE_RULES["gT"], rf) / 100
    o = V.dcf_reverse(m["r0"], m["m3"], gT, r, m["net_cash"], q["total_shares"], q["price"])
    return (o["g"] * 100 if o["status"] == "ok" else None), o["status"]


def channel(snap, res, qdoc, rf=None):
    """Value channel payload for the page: rows (all passers), fail reasons for every quoted code, counts."""
    from .build import RULES, FIN_INDUSTRY_WORDS, pct_rank
    if not qdoc:
        return {"file": None}
    years = qdoc["fiscal_years"][-RULE_YEARS:]      # the channel rules use the last 5 fiscal years
    R = VALUE_RULES
    Q = {r["code"]: r for r in snap["quotes"]}
    fl = {r["code"]: r for r in snap["fin"]["latest"]}
    screen = {r["code"]: r for r in res["screen"]}
    floor = res["revenue_floor"]
    why_all, rows = {}, []
    for code, q in Q.items():
        f, it = fl.get(code) or {}, qdoc["items"].get(code)
        name, ind = q.get("name") or f.get("name") or "", q.get("industry") or f.get("industry") or ""
        why = []
        if f.get("risk_warning") or "ST" in name.upper():
            why.append("st")
        if (f and not f.get("has_balance_sheet")) or any(w in ind for w in FIN_INDUSTRY_WORDS):
            why.append("financial")
        if not f or (f.get("revenue") or 0) < floor:
            why.append("small")
        if not f or (f.get("parent_net_profit") or 0) <= 0:
            why.append("np")
        m = metrics(it, years) if it else None
        if not m:
            why.append("nodata")
        else:
            if m["n_roe"] < R["roe_years_min"]:
                why.append("roe_n")
            else:
                if m["roe_med"] < R["roe_median_min"]:
                    why.append("roe_med")
                if m["roe_min"] < R["roe_min_min"]:
                    why.append("roe_min")
            if m["n_fcf"] < R["fcf_pos_min"] or m["fcf_pos"] < R["fcf_pos_min"]:
                why.append("fcf")
            if m["conv"] is None or m["conv"] < R["conv_min"]:
                why.append("conv")
            if m["cagr"] is None or m["cagr"] < 0:
                why.append("shrink")
            if m["de"] is None or m["de"] > R["de_max"]:
                why.append("de")
            if m["gw_pct"] is not None and m["gw_pct"] > R["goodwill_max_pct"]:
                why.append("goodwill")
        why_all[code] = ",".join(why)
        if why:
            continue
        mc = q.get("total_mcap")
        pe = q.get("pe_ttm")
        ig, igs = implied_growth(m, q, rf)
        rows.append({"code": code, "name": name, "industry": ind, "cyclical": ind in RULES["cyclical_industries"],
                     "price": q.get("price"), "mcap": mc, "pe": _r(pe, 1), "pb": _r(q.get("pb")),
                     "roe": [_r(x, 1) for x in m["roe"]], "roe_med": _r(m["roe_med"], 1), "roe_min": _r(m["roe_min"], 1),
                     "conv": _r(m["conv"]), "fcf_pos": m["fcf_pos"], "n_fcf": m["n_fcf"], "cagr": _r(m["cagr"], 1), "de": _r(m["de"]),
                     "fcfy": _r(100 * m["fcf_avg"] / mc) if mc and m["fcf_avg"] is not None else None,
                     "ey": _r(100 / pe) if pe and pe > 0 else None, "dy": _r(100 * m["div"] / mc) if mc else None,
                     "payout": _r(m["payout"]), "ig": _r(ig, 1), "igs": igs,
                     "ig_gap": _r(ig - m["cagr"], 1) if ig is not None and m["cagr"] is not None else None,
                     "in2": code in screen, "label": screen[code]["label"] if code in screen else None,
                     # latest YTD change (display only): a five-year record can hide a recent break, so it is shown next to the score
                     "rev_yoy": _r(f.get("revenue_yoy_pct"), 1), "np_yoy": _r(f.get("net_profit_yoy_pct"), 1),
                     "recent_drop": (f.get("net_profit_yoy_pct") is not None and f["net_profit_yoy_pct"] <= R["recent_np_drop"])})
    # ranking inside the channel (non-cyclical); missing factors are skipped, not filled
    rk = [r for r in rows if not r["cyclical"]]
    keys = ("roe_med", "roe_min", "conv", "fcfy", "ey", "dy")
    pc = {k: pct_rank({r["code"]: r[k] for r in rk}) for k in keys}
    for r in rk:
        f = {k: pc[k].get(r["code"]) for k in keys}
        av = [v for v in f.values() if v is not None]
        r["f"], r["score"] = f, round(statistics.mean(av), 1) if av else None
    rk.sort(key=lambda r: -(r["score"] or 0))
    for i, r in enumerate(rk, 1):
        r["rank"] = i
    rows.sort(key=lambda r: (r["cyclical"], r.get("rank") or 9999, -(r["roe_med"] or 0)))
    cnt = {}
    for w in why_all.values():
        for k in (w.split(",") if w else ["pass"]):
            cnt[k] = cnt.get(k, 0) + 1
    return {"file": os.path.basename(qdoc.get("_path") or "") or None, "fetched_at": qdoc.get("fetched_at"), "years": years,
            "rules": R, "rf": rf, "rows": rows, "why": why_all, "counts": cnt, "n_pass": len(rows), "n_ranked": len(rk),
            "complete": qdoc.get("complete")}
