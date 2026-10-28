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
    years = qdoc["fiscal_years"]
    Q = {r["code"]: r for r in snap["quotes"]}
    fl = {r["code"]: r for r in snap["fin"]["latest"]}
    screen = {r["code"]: r for r in res["screen"]}
    vc_codes = set()
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
