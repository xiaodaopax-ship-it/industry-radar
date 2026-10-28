"""红利筛选 (dividend screen) for A-shares. Research tooling, not investment advice; nothing here enters the ③ priority
score, the 基本面 / 交易 label, warn_n or the 对比池 score. Offline: built by `python -m cn build` from the latest
data/cn/quality_<ts>.json (5 fiscal years of cash dividends, parent net profit, weighted ROE, OCF, capex; see cn/quality.py)
and the snapshot (total market cap, PE / PB, latest YTD profit).

Rules (DIV_RULES; cash dividends = every plan whose REPORT_DATE falls in the fiscal year, interim + final, pre-tax):
  dividend yield = latest fiscal-year cash dividends / current total market cap >= 3%;
  cash dividends in every one of the 5 fiscal years; latest year >= 80% of the mean of the 3 years before (no big cut);
  payout (dividends / parent net profit, profitable years) 5-year median <= 80% (financials <= 50%);
  cash support (non-financial): 5-year dividends <= 5-year FCF (OCF - capex), FCF known for >= 4 years;
  financials (banks, insurers, brokers): 5-year median weighted ROE >= 8% instead of the FCF rule;
  profit: net profit > 0 in >= 4 of 5 years, latest YTD net profit > 0 and YTD net-profit YoY > -30%;
  not ST, total market cap >= 50亿.
Groups (each ranked on its own, never mixed): 稳定行业 / 金融 / 周期 (cn/build.RULES cyclical_industries: payouts follow
  the commodity cycle). Ranking: equal-weight mean of in-group percentiles of dividend yield, 4-year dividend CAGR,
  coverage (FCF / dividends; financials: net profit / dividends), median ROE and earnings yield (1 / PE TTM, PE > 0).
Limits: yield uses today's market cap; pre-tax (dividend tax depends on the holding period); dividend growth is on the
  company total, so share issuance / 送转 change it versus per-share dividends.
"""
import statistics

DIV_RULES = {"dy_min": 3.0, "years_paid": 5, "cut_ratio": 0.8, "payout_max": 0.8, "payout_max_fin": 0.5, "roe_fin_min": 8.0,
             "np_pos_min": 4, "ytd_np_yoy_min": -30.0, "mcap_min": 5e9}
RANK_KEYS = ("dy", "div_cagr", "cover", "roe_med", "ey")


def _r(x, d=2):
    return None if x is None else round(x, d)


def _pct(vals):
    items = sorted((v, k) for k, v in vals.items() if v is not None)
    n, out, i = len(items), {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        for t in range(i, j + 1):
            out[items[t][1]] = round(100.0 * ((i + j) / 2) / (n - 1), 1) if n > 1 else 50.0
        i = j + 1
    return out


def evaluate(it, years, q, f, fin):
    """(metrics, fail codes) of one company. it = quality items[code] (per-year dicts), q = quote, f = latest YTD financials."""
    R = DIV_RULES
    ys = [it.get(y) or {} for y in years]
    div = [d.get("div") or 0.0 for d in ys]
    npl = [d.get("np") for d in ys]
    roe = [d.get("roe") for d in ys if d.get("roe") is not None]
    fcf = [d["ocf"] - d["cx"] for d in ys if d.get("ocf") is not None and d.get("cx") is not None]
    pay = [dv / n for dv, n in zip(div, npl) if n and n > 0 and dv > 0]
    mc = q.get("total_mcap")
    m = {"div": div, "dy": 100 * div[-1] / mc if mc and div[-1] else None,
         "paid": sum(1 for x in div if x > 0), "prev3": statistics.mean(div[-4:-1]) if len(div) >= 4 else None,
         "payout": statistics.median(pay) if pay else None, "roe_med": statistics.median(roe) if roe else None,
         "np_pos": sum(1 for x in npl if x and x > 0), "fcf_sum": sum(fcf) if len(fcf) >= 4 else None, "n_fcf": len(fcf),
         "div_sum": sum(div), "div_cagr": ((div[-1] / div[0]) ** (1 / (len(div) - 1)) - 1) * 100 if div[0] > 0 and div[-1] > 0 else None,
         "mcap": mc, "pe": q.get("pe_ttm"), "pb": q.get("pb"), "ytd_np": f.get("parent_net_profit"), "ytd_np_yoy": f.get("net_profit_yoy_pct")}
    sum_np = sum(x for x in npl if x and x > 0)
    m["cover"] = (sum_np / m["div_sum"] if fin else (m["fcf_sum"] / m["div_sum"] if m["fcf_sum"] is not None else None)) if m["div_sum"] > 0 else None
    m["ey"] = 100 / m["pe"] if m["pe"] and m["pe"] > 0 else None
    w = []
    if not mc or mc < R["mcap_min"]:
        w.append("d_small")
    if m["dy"] is None or m["dy"] < R["dy_min"]:
        w.append("d_yield")
    if m["paid"] < R["years_paid"]:
        w.append("d_years")
    if m["prev3"] and div[-1] < R["cut_ratio"] * m["prev3"]:
        w.append("d_cut")
    if m["payout"] is None or m["payout"] > (R["payout_max_fin"] if fin else R["payout_max"]):
        w.append("d_payout")
    if fin:
        if m["roe_med"] is None or m["roe_med"] < R["roe_fin_min"]:
            w.append("d_roe")
    elif m["fcf_sum"] is None or m["fcf_sum"] < m["div_sum"]:
        w.append("d_cash")
    if m["np_pos"] < R["np_pos_min"]:
        w.append("d_np")
    if not m["ytd_np"] or m["ytd_np"] <= 0 or (m["ytd_np_yoy"] is not None and m["ytd_np_yoy"] < R["ytd_np_yoy_min"]):
        w.append("d_recent")
    return m, w


def panel(snap, res, qdoc, rf=None):
    """Page payload: passing rows (ranked inside their group) and a fail-code string for every quoted code."""
    from .build import RULES, FIN_INDUSTRY_WORDS
    if not qdoc:
        return {"file": None}
    years = qdoc["fiscal_years"][-5:]       # the dividend screen uses the last 5 fiscal years (the long-term view uses all)
    Q = {r["code"]: r for r in snap["quotes"]}
    fl = {r["code"]: r for r in snap["fin"]["latest"]}
    screen = {r["code"]: r for r in res["screen"]}
    rows, why = [], {}
    for code, q in Q.items():
        f, it = fl.get(code) or {}, qdoc["items"].get(code)
        name, ind = q.get("name") or f.get("name") or "", q.get("industry") or f.get("industry") or ""
        if f.get("risk_warning") or "ST" in name.upper():
            why[code] = "d_st"
            continue
        if not it:
            why[code] = "d_nodata"
            continue
        fin = (bool(f) and not f.get("has_balance_sheet")) or any(w in ind for w in FIN_INDUSTRY_WORDS)
        m, w = evaluate(it, years, q, f, fin)
        if w:
            why[code] = ",".join(w)
            continue
        why[code] = ""
        grp = "fin" if fin else "cyc" if ind in RULES["cyclical_industries"] else "stable"
        rows.append({"code": code, "name": name, "industry": ind, "group": grp, "price": q.get("price"), "mcap": m["mcap"],
                     "dy": _r(m["dy"]), "spread": _r(m["dy"] - rf) if rf is not None else None, "div": [_r(x / 1e8, 2) for x in m["div"]],
                     "div_cagr": _r(m["div_cagr"], 1), "payout": _r(m["payout"], 3), "cover": _r(m["cover"]), "roe_med": _r(m["roe_med"], 1),
                     "pe": _r(m["pe"], 1), "pb": _r(m["pb"]), "ey": _r(m["ey"]), "ytd_np_yoy": _r(m["ytd_np_yoy"], 1),
                     "in2": code in screen, "label": screen[code]["label"] if code in screen else None})
    n_rank = {}
    for g in ("stable", "fin", "cyc"):
        rk = [r for r in rows if r["group"] == g]
        pc = {k: _pct({r["code"]: r[k] for r in rk}) for k in RANK_KEYS}
        for r in rk:
            fs = {k: pc[k].get(r["code"]) for k in RANK_KEYS}
            av = [v for v in fs.values() if v is not None]
            r["f"], r["score"] = fs, round(statistics.mean(av), 1) if av else None
        rk.sort(key=lambda r: -(r["score"] or 0))
        for i, r in enumerate(rk, 1):
            r["rank"] = i
        n_rank[g] = len(rk)
    rows.sort(key=lambda r: ({"stable": 0, "fin": 1, "cyc": 2}[r["group"]], r["rank"]))
    cnt = {}
    for w in why.values():
        for k in (w.split(",") if w else ["pass"]):
            cnt[k] = cnt.get(k, 0) + 1
    return {"file": qdoc.get("_path") and qdoc["_path"].replace("\\", "/").split("/")[-1], "fetched_at": qdoc.get("fetched_at"),
            "years": years, "rules": DIV_RULES, "rf": rf, "rows": rows, "n": n_rank, "why": why, "counts": cnt}


# ----------------------------------------------------------------------------- 长期红利 (10 fiscal years)
# A stricter view for long holding periods: the question is whether the dividend can last and grow through a full cycle,
# not how high today's yield is. Uses all fiscal years in the quality file (10). Display only, same caveats as above.
LONG_RULES = {"n_years": 10, "years_paid": 10, "max_cut": -30.0, "cut_ratio": 0.8, "roe_min": 8.0, "roe_med": 12.0,
              "roe_min_fin": 6.0, "roe_med_fin": 10.0, "payout_lo": 0.2, "payout_hi": 0.75, "payout_hi_fin": 0.5,
              "cover_min": 1.0, "fcf_years": 8, "rev_cagr_min": 2.0, "div_cagr_min": 0.0, "gm_sd_max": 8.0, "dy_min": 2.5,
              "ytd_np_yoy_min": -20.0, "de_max": 0.8, "gw_max_pct": 30.0, "mcap_min": 5e9, "growth_cap": 10.0}
# industries whose profits and dividends swing with a cycle or are exposed to disruption / policy beyond the ones in
# cn/build.RULES cyclical_industries; excluded from the long-term view (they can still pass the 5-year dividend screen)
LONG_CYCLICAL_EXTRA = ["光伏设备", "电池", "汽车零部件", "乘用车", "商用车", "房地产开发", "房地产服务", "航空机场", "化学制品",
                       "证券Ⅱ", "风电设备", "半导体", "消费电子"]
LONG_KEYS = ("dy", "div_cagr", "roe_min", "cover", "gm_sdn")
LONG_CHECKS = ["护城河：品牌、渠道、牌照、成本或网络效应是否可持续，10 年后会不会被替代",
               "行业趋势：需求是否在结构性萎缩，是否依赖单一政策、单一客户或单一产品",
               "分红承诺：是否有明确的股东回报规划（如三年分红规划），控股股东是否依赖分红",
               "治理：控股股东是否稳定，减持、质押、关联交易情况",
               "买入价格：当前股息率和 PE 相对自身历史的位置（页面只给 5 年 PE 分位，且只覆盖短名单）"]


def _cagr(a, b, n):
    return ((b / a) ** (1 / n) - 1) * 100 if a and b and a > 0 and b > 0 and n > 0 else None


def long_eval(it, years, q, f, fin):
    R = LONG_RULES
    ys = [it.get(y) or {} for y in years]
    div = [d.get("div") or 0.0 for d in ys]
    npl = [d.get("np") for d in ys]
    roe = [d.get("roe") for d in ys if d.get("roe") is not None]
    fcf = [d["ocf"] - d["cx"] for d in ys if d.get("ocf") is not None and d.get("cx") is not None]
    gm = [d.get("gm") for d in ys if d.get("gm") is not None]
    rev = [d.get("rev") for d in ys]
    pay = [dv / n for dv, n in zip(div, npl) if n and n > 0 and dv > 0]
    cuts = [(b / a - 1) * 100 for a, b in zip(div, div[1:]) if a > 0]
    last, mc = ys[-1], q.get("total_mcap")
    eq = last.get("eq")
    m = {"div": div, "paid": sum(1 for x in div if x > 0), "max_cut": min(cuts) if cuts else None,
         "prev3": statistics.mean(div[-4:-1]), "roe_min": min(roe) if roe else None, "roe_med": statistics.median(roe) if roe else None,
         "n_roe": len(roe), "payout": statistics.median(pay) if pay else None, "fcf_sum": sum(fcf) if len(fcf) >= R["fcf_years"] else None,
         "div_sum": sum(div), "np_sum": sum(x for x in npl if x and x > 0), "rev_cagr": _cagr(rev[0], rev[-1], len(rev) - 1),
         "div_cagr": _cagr(div[0], div[-1], len(div) - 1), "gm_sd": statistics.pstdev(gm) if len(gm) >= 8 else None,
         "dy": 100 * div[-1] / mc if mc and div[-1] else None, "mcap": mc, "pe": q.get("pe_ttm"), "pb": q.get("pb"),
         "de": (last.get("ibd") or 0) / eq if eq and eq > 0 else None, "gw_pct": 100 * (last.get("gw") or 0) / eq if eq and eq > 0 else None,
         "ytd_np": f.get("parent_net_profit"), "ytd_np_yoy": f.get("net_profit_yoy_pct")}
    m["cover"] = (m["np_sum"] / m["div_sum"] if fin else (m["fcf_sum"] / m["div_sum"] if m["fcf_sum"] is not None else None)) if m["div_sum"] > 0 else None
    w = []
    if not mc or mc < R["mcap_min"]:
        w.append("l_small")
    if m["paid"] < R["years_paid"]:
        w.append("l_years")
    if m["max_cut"] is not None and m["max_cut"] < R["max_cut"]:
        w.append("l_cut")
    if div[-1] < R["cut_ratio"] * m["prev3"]:
        w.append("l_recent_cut")
    if m["n_roe"] < 9 or m["roe_min"] < (R["roe_min_fin"] if fin else R["roe_min"]) or m["roe_med"] < (R["roe_med_fin"] if fin else R["roe_med"]):
        w.append("l_roe")
    if m["payout"] is None or not R["payout_lo"] <= m["payout"] <= (R["payout_hi_fin"] if fin else R["payout_hi"]):
        w.append("l_payout")
    if not fin and (m["cover"] is None or m["cover"] < R["cover_min"]):
        w.append("l_cash")
    if m["rev_cagr"] is None or m["rev_cagr"] < R["rev_cagr_min"]:
        w.append("l_growth")
    if m["div_cagr"] is None or m["div_cagr"] < R["div_cagr_min"]:
        w.append("l_divgrowth")
    if not fin and (m["gm_sd"] is None or m["gm_sd"] > R["gm_sd_max"]):
        w.append("l_gm")
    if not fin and ((m["de"] is not None and m["de"] > R["de_max"]) or (m["gw_pct"] is not None and m["gw_pct"] > R["gw_max_pct"])):
        w.append("l_balance")
    if m["dy"] is None or m["dy"] < R["dy_min"]:
        w.append("l_yield")
    if not m["ytd_np"] or m["ytd_np"] <= 0 or (m["ytd_np_yoy"] is not None and m["ytd_np_yoy"] < R["ytd_np_yoy_min"]):
        w.append("l_recent")
    return m, w


def long_term(snap, res, qdoc, rf=None):
    from .build import RULES, FIN_INDUSTRY_WORDS
    if not qdoc:
        return {"file": None}
    years = qdoc["fiscal_years"]
    if len(years) < LONG_RULES["n_years"]:
        return {"file": None, "why_none": f"quality file has {len(years)} fiscal years; run python -m cn quality (10 years)"}
    cyc = set(RULES["cyclical_industries"]) | set(LONG_CYCLICAL_EXTRA)
    Q = {r["code"]: r for r in snap["quotes"]}
    fl = {r["code"]: r for r in snap["fin"]["latest"]}
    screen = {r["code"]: r for r in res["screen"]}
    rows, why = [], {}
    for code, q in Q.items():
        f, it = fl.get(code) or {}, qdoc["items"].get(code)
        name, ind = q.get("name") or f.get("name") or "", q.get("industry") or f.get("industry") or ""
        if f.get("risk_warning") or "ST" in name.upper():
            why[code] = "d_st"
            continue
        if ind in cyc:
            why[code] = "l_cyc"
            continue
        if not it:
            why[code] = "d_nodata"
            continue
        fin = (bool(f) and not f.get("has_balance_sheet")) or any(w in ind for w in FIN_INDUSTRY_WORDS)
        m, w = long_eval(it, years, q, f, fin)
        why[code] = ",".join(w)
        if w:
            continue
        g = min(m["div_cagr"], LONG_RULES["growth_cap"])
        rows.append({"code": code, "name": name, "industry": ind, "group": "fin" if fin else "stable", "price": q.get("price"),
                     "mcap": m["mcap"], "dy": _r(m["dy"]), "div": [_r(x / 1e8, 2) for x in m["div"]], "div_cagr": _r(m["div_cagr"], 1),
                     "max_cut": _r(m["max_cut"], 1), "roe_min": _r(m["roe_min"], 1), "roe_med": _r(m["roe_med"], 1),
                     "payout": _r(m["payout"], 3), "cover": _r(m["cover"]), "rev_cagr": _r(m["rev_cagr"], 1), "gm_sd": _r(m["gm_sd"], 1),
                     "gm_sdn": -m["gm_sd"] if m["gm_sd"] is not None else None, "de": _r(m["de"]),
                     "exp_ret": _r(m["dy"] + g, 1), "pe": _r(m["pe"], 1), "pb": _r(m["pb"]), "ytd_np_yoy": _r(m["ytd_np_yoy"], 1),
                     "in2": code in screen, "label": screen[code]["label"] if code in screen else None})
    n_rank = {}
    for grp in ("stable", "fin"):
        rk = [r for r in rows if r["group"] == grp]
        pc = {k: _pct({r["code"]: r[k] for r in rk}) for k in LONG_KEYS}
        for r in rk:
            fs = {k: pc[k].get(r["code"]) for k in LONG_KEYS}
            av = [v for v in fs.values() if v is not None]
            r["f"], r["score"] = fs, round(statistics.mean(av), 1) if av else None
        rk.sort(key=lambda r: -(r["score"] or 0))
        for i, r in enumerate(rk, 1):
            r["rank"] = i
        n_rank[grp] = len(rk)
    for r in rows:
        r.pop("gm_sdn", None)
    rows.sort(key=lambda r: (r["group"] != "stable", r["rank"]))
    cnt = {}
    for w in why.values():
        for k in (w.split(",") if w else ["pass"]):
            cnt[k] = cnt.get(k, 0) + 1
    return {"file": qdoc.get("_path") and qdoc["_path"].replace("\\", "/").split("/")[-1], "years": years, "rules": LONG_RULES,
            "cyclical_extra": LONG_CYCLICAL_EXTRA, "checks": LONG_CHECKS, "rf": rf, "rows": rows, "n": n_rank, "why": why, "counts": cnt}
