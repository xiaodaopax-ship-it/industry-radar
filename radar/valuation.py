"""Cash-flow valuation inputs (display only): annual OCF / capex / revenue, net cash, shares, consensus revenue growth, 10-year rate.

    python -m radar valuation            fetch what is missing or older than valuation.cache_days, then rebuild the site
    python -m radar valuation --refresh  ignore the per-company cache

Nothing here feeds the score, factors, level, PEG screen tiers or the comparison-basket score: the dashboard computes the
reverse DCF / scenario DCF in the browser from these inputs (see templates/dashboard.html dcfEngine / dcfReverse).

Sources (all recorded per company in "urls"):
  SEC filers  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (us-gaap + dei facts)
  others      Yahoo fundamentals-timeseries (annual cash flow / revenue, latest quarterly balance sheet), unofficial
  consensus   Yahoo quoteSummary earningsTrend revenueEstimate (0y, +1y), unofficial
  10-year     U.S. Treasury daily par yield curve CSV (10 Yr), FRED DGS10 as a fallback
Results are cached per company under data/cache/val/ so an interrupted run resumes; the merged output is data/valuation_<ts>.json.
"""
import concurrent.futures as cf
import csv
import datetime as dt
import glob
import io
import json
import os
import re
import sys
import time

from .net import Yahoo, get, raw as yraw, sec
from .paths import CACHE, CFG, DATA

VC = CFG.get("valuation", {})
VDIR = os.path.join(CACHE, "val")
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:010d}.json"

# us-gaap tags, earlier tags take precedence per period (filers switch tags over the years)
TAGS = {
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements"],
    "rev": list(CFG["revenue_tags"]) + ["SalesRevenueNet"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligations",
             "LongTermDebtAndCapitalLeaseObligationsCurrent", "ConvertibleNotesPayable", "ConvertibleDebtNoncurrent",
             "ConvertibleNotesPayableCurrent", "ShortTermBorrowings", "CommercialPaper"],
    "dil": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
DEI_SHARES = "EntityCommonStockSharesOutstanding"
YH_TS = ["annualOperatingCashFlow", "annualCapitalExpenditure", "annualTotalRevenue", "annualTotalDebt",
         "annualCashCashEquivalentsAndShortTermInvestments", "annualCashAndCashEquivalents",
         "trailingOperatingCashFlow", "trailingCapitalExpenditure", "trailingTotalRevenue",
         "quarterlyTotalDebt", "quarterlyCashCashEquivalentsAndShortTermInvestments", "quarterlyCashAndCashEquivalents",
         "quarterlyCapitalLeaseObligations", "annualCapitalLeaseObligations"]


def log(msg):
    print(msg, flush=True)


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# ---------------------------------------------------------------- risk-free rate
def risk_free():
    """Latest US 10-year Treasury par yield (percent). Treasury CSV first, FRED DGS10 second; None when both fail."""
    today = dt.date.today()
    for y in (today.year, today.year - 1):
        url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all"
               f"?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
        try:
            rows = list(csv.DictReader(io.StringIO(get(url, as_json=False, retries=2, timeout=30).decode("utf-8-sig"))))
        except Exception as ex:
            log(f"treasury {y}: {type(ex).__name__}")
            continue
        best = None
        for r in rows:
            v, d = (r.get("10 Yr") or "").strip(), (r.get("Date") or "").strip()
            try:
                day = dt.datetime.strptime(d, "%m/%d/%Y").date()
                val = float(v)
            except ValueError:
                continue
            if not best or day > best[0]:
                best = (day, val)
        if best:
            return {"value": best[1], "date": best[0].isoformat(), "series": "U.S. Treasury daily par yield curve, 10 Yr",
                    "url": url, "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
    try:
        rows = list(csv.reader(io.StringIO(get(url, as_json=False, retries=1, timeout=20).decode("utf-8-sig"))))
        good = [(r[0], float(r[1])) for r in rows[1:] if len(r) > 1 and re.match(r"^-?\d", r[1] or "")]
        if good:
            return {"value": good[-1][1], "date": good[-1][0], "series": "FRED DGS10", "url": url,
                    "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
    except Exception as ex:
        log(f"fred: {type(ex).__name__}")
    return None


# ---------------------------------------------------------------- SEC companyfacts
def _facts(cik):
    """Only the facts this module needs, from companyfacts. Reuses research.py's cached companyfacts file when it is fresh."""
    p = os.path.join(CACHE, f"facts_{cik}.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < VC.get("cache_days", 6) * 86400:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    else:
        d = sec(FACTS_URL.format(cik))
    g, dei = ((d or {}).get("facts") or {}).get("us-gaap") or {}, ((d or {}).get("facts") or {}).get("dei") or {}
    out = {}
    for tags in TAGS.values():
        for t in tags:
            u = (g.get(t) or {}).get("units") or {}
            rows = u.get("USD") or u.get("shares") or []
            if rows:
                out[t] = [{k: f.get(k) for k in ("start", "end", "val", "form", "filed")} for f in rows]
    u = ((dei.get(DEI_SHARES) or {}).get("units") or {}).get("shares") or []
    if u:
        out[DEI_SHARES] = [{k: f.get(k) for k in ("end", "val", "form", "filed")} for f in u]
    return out, bool(g)


def _dur(F, tag):
    """{(start, end): val} for one duration concept; the latest filing wins for each period (restatements)."""
    best = {}
    for f in F.get(tag, []):
        if f.get("start") and f.get("end") and f.get("val") is not None:
            k = (f["start"], f["end"])
            if k not in best or (f.get("filed") or "") >= best[k][1]:
                best[k] = (f["val"], f.get("filed") or "")
    return {k: v for k, (v, _) in best.items()}


def _inst(F, tag):
    best = {}
    for f in F.get(tag, []):
        if f.get("end") and f.get("val") is not None and not f.get("start"):
            if f["end"] not in best or (f.get("filed") or "") >= best[f["end"]][1]:
                best[f["end"]] = (f["val"], f.get("filed") or "")
    return {k: v for k, (v, _) in best.items()}


def _annual(F, tags):
    """{fiscal-year end: (val, tag)}; per period the first tag in the list that has a value."""
    out = {}
    for t in tags:
        for (s, e), v in _dur(F, t).items():
            if 350 <= _days(s, e) <= 380 and e not in out:
                out[e] = (v, t)
    return out


def _ttm_one(d):
    """TTM from one tag's durations: latest annual if it is the latest period, else FY + YTD - prior-year YTD."""
    if not d:
        return None
    end = max(e for _, e in d)
    fy = next(((v, s) for (s, e), v in d.items() if e == end and 350 <= _days(s, e) <= 380), None)
    if fy:
        return {"end": end, "val": fy[0], "how": "FY"}
    ytd = [(s, v) for (s, e), v in d.items() if e == end and 80 <= _days(s, e) <= 290]
    if not ytd:
        return None
    s0, v0 = min(ytd)  # the longest year-to-date figure
    n = _days(s0, end)
    prev_fy = next((v for (s, e), v in d.items() if abs(_days(e, s0)) <= 7 and 350 <= _days(s, e) <= 380), None)
    prev = next((v for (s, e), v in d.items() if abs(_days(e, end) - 365) <= 10 and abs(_days(s, e) - n) <= 10), None)
    if prev_fy is None or prev is None:
        return None
    return {"end": end, "val": prev_fy + v0 - prev, "how": f"FY + {round(n / 30.4)}M YTD - prior YTD"}


def _ttm(F, tags, end=None):
    """TTM for the given end date (first tag that has it), or, with end=None, the tag whose TTM is the most recent
    (filers switch tags, e.g. NVDA stopped using RevenueFromContractWithCustomer... in FY2023); ties keep the tag order."""
    best = None
    for t in tags:
        x = _ttm_one(_dur(F, t))
        if not x:
            continue
        if end is not None:
            if x["end"] == end:
                return {**x, "tag": t}
        elif best is None or x["end"] > best["end"]:
            best = {**x, "tag": t}
    return best


def _near(series, e, tol=10):
    """Value in {end: (val, tag)} with an end date within tol days of e."""
    for k, v in series.items():
        if abs(_days(k, e)) <= tol:
            return v
    return None


def _ttm_ly(F, tags, end):
    """TTM ending about one year before `end` (for a TTM revenue growth rate)."""
    for t in tags:
        d = _dur(F, t)
        tgt = (dt.date.fromisoformat(end) - dt.timedelta(days=365)).isoformat()
        cut = {k: v for k, v in d.items() if _days(k[1], tgt) >= -10}
        x = _ttm_one(cut)
        if x and abs(_days(x["end"], tgt)) <= 10:
            return x["val"]
    return None


def _balance(F):
    """Cash (+ short-term investments) and total debt at the latest balance-sheet date of the cash concept.

    Debt = LongTermDebt (includes the current portion) or LongTermDebtNoncurrent + LongTermDebtCurrent, or the
    ...AndCapitalLeaseObligations pair (finance leases, when that is all the filer tags), or convertible notes;
    plus ShortTermBorrowings (CommercialPaper when ShortTermBorrowings is absent). Operating lease liabilities are excluded.
    """
    cs = {}
    for t in TAGS["cash"]:
        for e, v in _inst(F, t).items():
            cs.setdefault(e, (v, t))
    if not cs:
        return None
    at = max(cs)
    val = lambda t: _inst(F, t).get(at)
    cash, parts = cs[at][0], [cs[at][1]]
    for t in TAGS["sti"]:
        if val(t) is not None:
            cash += val(t)
            parts.append(t)
            break
    debt, dparts = 0.0, []
    for combo in (["LongTermDebt"], ["LongTermDebtNoncurrent", "LongTermDebtCurrent"],
                  ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
                  ["ConvertibleNotesPayable"], ["ConvertibleDebtNoncurrent", "ConvertibleNotesPayableCurrent"]):
        if val(combo[0]) is not None:
            for t in combo:
                if val(t) is not None:
                    debt += val(t)
                    dparts.append(t)
            break
    for t in ("ShortTermBorrowings", "CommercialPaper"):
        if val(t) is not None:
            debt += val(t)
            dparts.append(t)
            break
    return {"date": at, "cash": cash, "cash_tags": parts, "debt": debt, "debt_tags": dparts or ["（未找到债务科目，按 0 计）"]}


def from_sec(cik):
    F, has_gaap = _facts(cik)
    url = FACTS_URL.format(cik)
    if not has_gaap:
        return None, "SEC 没有 us-gaap 数据（可能是 20-F / 40-F 申报人）", url
    ocf, cap, rev = _annual(F, TAGS["ocf"]), _annual(F, TAGS["capex"]), _annual(F, TAGS["rev"])
    if not ocf:
        return None, "SEC 申报里没有年度经营现金流", url
    no_capex = not cap
    fy = []
    for e in sorted(ocf, reverse=True):
        if fy and _days(e, fy[-1]["end"]) < 300:
            continue
        r = _near(rev, e)
        c = _near(cap, e, 10)
        if c is None and not no_capex:
            continue  # capex tagged in other years but not this one: skip the year rather than guess
        cv = c[0] if c else 0.0
        row = {"end": e, "ocf": ocf[e][0], "capex": cv, "rev": r[0] if r else None, "ocf_tag": ocf[e][1],
               "capex_tag": c[1] if c else None, "rev_tag": r[1] if r else None}
        row["fcf"] = row["ocf"] - cv
        row["m"] = row["fcf"] / row["rev"] if row["rev"] and row["rev"] > 0 else None
        fy.append(row)
        if len(fy) == 5:
            break
    fy.reverse()
    t_ocf = _ttm(F, TAGS["ocf"])
    ttm = None
    if t_ocf and fy and t_ocf["end"] > fy[-1]["end"]:
        t_cap = _ttm(F, TAGS["capex"], t_ocf["end"])
        t_rev = _ttm(F, TAGS["rev"], t_ocf["end"])
        if (t_cap or no_capex) and t_rev:
            cv = t_cap["val"] if t_cap else 0.0
            ttm = {"end": t_ocf["end"], "ocf": t_ocf["val"], "capex": cv, "rev": t_rev["val"], "fcf": t_ocf["val"] - cv,
                   "how": t_ocf["how"], "ocf_tag": t_ocf["tag"], "capex_tag": t_cap["tag"] if t_cap else None, "rev_tag": t_rev["tag"]}
            ttm["m"] = ttm["fcf"] / ttm["rev"] if ttm["rev"] > 0 else None
    t_rev = _ttm(F, TAGS["rev"])
    ttm_yoy = None
    if t_rev:
        ly = _ttm_ly(F, [t_rev["tag"]], t_rev["end"])
        ttm_yoy = (t_rev["val"] / ly - 1) * 100 if ly and ly > 0 else None
    sh = _inst(F, DEI_SHARES)
    shares = {"v": sh[max(sh)], "date": max(sh), "tag": "dei:" + DEI_SHARES} if sh else None
    dil = [(e, v) for (s, e), v in _dur(F, TAGS["dil"][0]).items() if 80 <= _days(s, e) <= 100]
    out = {"src": "sec", "ccy": "USD", "fy": fy, "ttm": ttm, "rev_ttm": t_rev["val"] if t_rev else None,
           "rev_ttm_end": t_rev["end"] if t_rev else None, "ttm_yoy": ttm_yoy, "bs": _balance(F), "shares": shares,
           "shares_dil": {"v": max(dil)[1], "date": max(dil)[0], "tag": TAGS["dil"][0]} if dil else None,
           "no_capex_tag": no_capex}
    return out, None, url


# ---------------------------------------------------------------- Yahoo (non-SEC filers)
def from_yahoo(yh, sym):
    ts = yh.timeseries(sym, YH_TS, years=6)
    url = f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}?type=annualOperatingCashFlow,..."
    by = {k: {x["date"]: x for x in v} for k, v in ts.items()}
    A = lambda k: by.get("annual" + k, {})
    ocf = A("OperatingCashFlow")
    if not ocf:
        return None, "海外公司现金流数据未取到", url
    ccys = {x.get("ccy") for x in ocf.values() if x.get("ccy")}
    fy = []
    for e in sorted(ocf)[-5:]:
        c, r = A("CapitalExpenditure").get(e), A("TotalRevenue").get(e)
        if c is None:
            continue
        row = {"end": e, "ocf": ocf[e]["val"], "capex": -c["val"], "rev": r["val"] if r else None,
               "ocf_tag": "yahoo:annualOperatingCashFlow", "capex_tag": "yahoo:annualCapitalExpenditure", "rev_tag": "yahoo:annualTotalRevenue"}
        row["fcf"] = row["ocf"] - row["capex"]
        row["m"] = row["fcf"] / row["rev"] if row["rev"] and row["rev"] > 0 else None
        fy.append(row)
    T = lambda k: by.get("trailing" + k, {})
    ttm = None
    if T("OperatingCashFlow") and fy:
        e = max(T("OperatingCashFlow"))
        c, r = T("CapitalExpenditure").get(e), T("TotalRevenue").get(e)
        if e > fy[-1]["end"] and c and r and r["val"] > 0:
            o = T("OperatingCashFlow")[e]["val"]
            ttm = {"end": e, "ocf": o, "capex": -c["val"], "rev": r["val"], "fcf": o + c["val"], "how": "yahoo trailing",
                   "ocf_tag": "yahoo:trailingOperatingCashFlow", "capex_tag": "yahoo:trailingCapitalExpenditure", "rev_tag": "yahoo:trailingTotalRevenue"}
            ttm["m"] = ttm["fcf"] / ttm["rev"]
    bs = None
    for pre in ("quarterly", "annual"):
        cash = by.get(pre + "CashCashEquivalentsAndShortTermInvestments") or by.get(pre + "CashAndCashEquivalents") or {}
        if cash:
            at = max(cash)
            dbt = (by.get(pre + "TotalDebt") or {}).get(at)
            lease = (by.get(pre + "CapitalLeaseObligations") or {}).get(at)
            # Yahoo's TotalDebt includes lease liabilities; subtract them when Yahoo reports them separately
            dv = (dbt["val"] - (lease["val"] if lease and lease["val"] < dbt["val"] else 0)) if dbt else 0.0
            dtag = (["yahoo:" + pre + "TotalDebt − " + pre + "CapitalLeaseObligations"] if lease else ["yahoo:" + pre + "TotalDebt（可能含租赁负债）"]) if dbt else ["（未找到债务，按 0 计）"]
            bs = {"date": at, "cash": cash[at]["val"], "cash_tags": ["yahoo:" + pre + ("CashCashEquivalentsAndShortTermInvestments" if by.get(pre + "CashCashEquivalentsAndShortTermInvestments") else "CashAndCashEquivalents")],
                  "debt": dv, "debt_tags": dtag}
            break
    rv = A("TotalRevenue")
    tr = T("TotalRevenue")
    rev_ttm = tr[max(tr)]["val"] if tr else (rv[max(rv)]["val"] if rv else None)
    return {"src": "yahoo", "ccy": (sorted(ccys) or [None])[0], "fy": fy, "ttm": ttm, "rev_ttm": rev_ttm,
            "rev_ttm_end": max(tr) if tr else (max(rv) if rv else None), "ttm_yoy": None, "bs": bs, "shares": None,
            "shares_dil": None, "no_capex_tag": False, "rev_annual": [[e, rv[e]["val"]] for e in sorted(rv)]}, None, url


# ---------------------------------------------------------------- consensus revenue growth (Yahoo earningsTrend)
def consensus(yh, sym):
    r = yh.summary(sym, "earningsTrend")
    trend = {t.get("period"): t for t in ((r or {}).get("earningsTrend") or {}).get("trend", [])}
    out = {}
    for k in ("0y", "+1y"):
        e = (trend.get(k) or {}).get("revenueEstimate") or {}
        if yraw(e, "avg") is None:
            continue
        out[k] = {"avg": yraw(e, "avg"), "n": yraw(e, "numberOfAnalysts"), "growth": yraw(e, "growth"),
                  "year_ago": yraw(e, "yearAgoRevenue"), "end": trend[k].get("endDate")}
    return out or None


# ---------------------------------------------------------------- one company
def _cagr3(fy, rev_annual=None):
    pts = [(r["end"], r["rev"]) for r in fy if r.get("rev")] if not rev_annual else [tuple(x) for x in rev_annual if x[1]]
    pts.sort()
    if len(pts) < 4 or pts[-4][1] <= 0 or pts[-1][1] <= 0:
        return None
    yrs = _days(pts[-4][0], pts[-1][0]) / 365.25
    return ((pts[-1][1] / pts[-4][1]) ** (1 / yrs) - 1) * 100 if yrs > 2.5 else None


def company(yh, t):
    cache = os.path.join(VDIR, re.sub(r"[^A-Za-z0-9._-]", "_", t["ticker"]) + ".json")
    out, why, urls = None, None, []
    if t.get("cik"):
        try:
            out, why, u = from_sec(t["cik"])
        except Exception as ex:  # not cached: retried next run
            return t["ticker"], None, f"SEC 请求失败：{type(ex).__name__}"
        urls.append(u)
    if out is None or not out.get("fy"):
        y, why2, u = from_yahoo(yh, t["ticker"])
        urls.append(u)
        if y and y.get("fy"):
            out, why = y, (f"{why}；改用 Yahoo" if why else None)
        else:
            why = why or why2
    if out is None:
        out = {"src": None, "fy": []}
    out["why"] = why if not out.get("fy") else None
    out["note"] = why if out.get("fy") and why else None
    out["cagr3"] = _cagr3(out["fy"], out.pop("rev_annual", None))
    out["cons"] = consensus(yh, t["ticker"])
    urls.append(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{t['ticker']}?modules=earningsTrend")
    out["urls"], out["fetched"] = urls, dt.datetime.now().isoformat(timespec="seconds")
    os.makedirs(VDIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return t["ticker"], out, None


def _cached(ticker, days):
    p = os.path.join(VDIR, re.sub(r"[^A-Za-z0-9._-]", "_", ticker) + ".json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < days * 86400:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def targets(raw):
    from .research import ticker_map
    tm = ticker_map()
    out = {}
    for r in raw["candidates"]:
        out[r["ticker"]] = {"ticker": r["ticker"], "cik": r.get("cik") if r.get("market") == "US" else None}
    for s in raw["leaders"]:
        if s not in out:
            out[s] = {"ticker": s, "cik": tm.get(s.upper()) if "." not in s else None}
    return list(out.values())


def run(refresh=False):
    from .build import latest_raw
    raw = latest_raw()
    rf = risk_free()
    log(f"10y treasury: {rf['value']}% on {rf['date']} ({rf['series']})" if rf else "10y treasury: not available")
    ts = targets(raw)
    days = VC.get("cache_days", 6)
    res, todo = {}, []
    for t in ts:
        c = None if refresh else _cached(t["ticker"], days)
        if c is not None:
            res[t["ticker"]] = c
        else:
            todo.append(t)
    log(f"valuation inputs: {len(ts)} companies, {len(res)} cached, {len(todo)} to fetch ...")
    yh = Yahoo()
    fails = []
    with cf.ThreadPoolExecutor(max_workers=4) as pool:  # SEC and Yahoo stay globally rate-limited inside net.py
        futs = [pool.submit(company, yh, t) for t in todo]
        for i, fut in enumerate(cf.as_completed(futs)):
            tk, v, err = fut.result()
            if v is None:
                fails.append((tk, err))
            else:
                res[tk] = v
            if (i + 1) % 25 == 0:
                log(f"  {i + 1}/{len(todo)}")
    n_fcf = sum(1 for v in res.values() if v.get("fy"))
    n_cons = sum(1 for v in res.values() if ((v.get("cons") or {}).get("+1y") or {}).get("n", 0) >= 3)
    log(f"valuation inputs: {len(res)} companies, {n_fcf} with annual FCF, {n_cons} with >=3-analyst +1y revenue consensus, {len(fails)} failed")
    for tk, err in fails[:10]:
        log(f"  failed {tk}: {err.encode('ascii', 'replace').decode()}")
    payload = {"version": 1, "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "raw_fetched_at": raw["fetched_at"],
               "rf": rf, "by": res, "failed": [t for t, _ in fails],
               "sources": {"sec": "https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json",
                           "yahoo_ts": "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/",
                           "yahoo_trend": "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=earningsTrend"}}
    path = os.path.join(DATA, f"valuation_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    log(f"valuation snapshot saved: {path}")
    if fails and len(fails) > len(ts) // 4:
        sys.exit(1)  # mostly failed: let the weekly job report it; the site keeps the previous valuation file


def latest_file():
    """(path, payload) of the newest valuation file, or (None, None)."""
    files = sorted(glob.glob(os.path.join(DATA, "valuation_*.json")), key=os.path.getmtime, reverse=True)
    if not files:
        return None, None
    with open(files[0], encoding="utf-8") as f:
        return files[0], json.load(f)


def latest():
    return latest_file()[1]


# ====================================================================================== engine mirror
# Exact mirror of the page engine in templates/dashboard.html (dcfEngine / dcfReverse / riEngine / dcfRampPath / dcfBasis /
# dcfPrep / dcfValue), including the FCF-margin basis rules (auto / avg3 / fy / ttm / ramp) and the one-off guard. Display only:
# nothing here feeds score, factors, level, PEG tiers or the basket score. Keep both sides in sync (radar/ledger.py reuses this).
# Inputs: `e` = one dashboard entry (data["entries"][i]), `V` = data["val"]["by"][ticker], `fx` = data["fx"], `seg` = the segment
# config of the entry (sectors.json). Rates are decimals inside the engine; parameters P are in percent like the page.
VAL_DEF = {"erp": 5, "gT": 2.5, "r_amp": 1, "g_amp": 30, "m_amp": 20, "roe_amp": 20, "g_cap": 40, "g_floor": -10, "min_analysts": 3,
           "mbasis": "auto", "ramp_n": 5, "ramp_t": 15, **(VC.get("defaults") or {})}
DCF_GAP = 0.005      # terminal growth stays > 0.5pt below each scenario's discount rate
MB_GAP = 0.10        # |TTM - average| above this -> "TTM 与历史差距大" tag
BASES = ("auto", "avg3", "fy", "ttm", "ramp")


def _p(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def _pos(v):
    return v is not None and v > 0


def dcf_engine(R0, m, g0, gT, r, netCash=0.0, shares=None, years=10, ramp=None):
    """dcfEngine. Growth fades linearly g0 (year 1) -> gT (year N). ramp = {"ms", "N"}: m_t = ms + (m - ms) * min(t / N, 1)
    (m = steady state); else m constant. TV = FCF_N * (1 + gT) / (r - gT)."""
    N, nc = years, netCash or 0
    R, pv, f = R0, 0.0, 0.0
    for t in range(1, N + 1):
        g = g0 + (gT - g0) * (t - 1) / (N - 1) if N > 1 else g0
        R *= 1 + g
        f = R * (ramp["ms"] + (m - ramp["ms"]) * min(t / ramp["N"], 1) if ramp else m)
        pv += f / (1 + r) ** t
    tv = f * (1 + gT) / (r - gT)
    pvTv = tv / (1 + r) ** N
    ev = pv + pvTv
    return {"pvFcf": pv, "tv": tv, "pvTv": pvTv, "ev": ev, "equity": ev + nc, "perShare": (ev + nc) / shares if shares else None,
            "revN": R, "fcfN": f}


def dcf_reverse(inp, price, key="g0", lo=-0.30, hi=1.00):
    """dcfReverse: bisection on [lo, hi] for inp[key] so that perShare = price -> {ok, x} | {ok: False, side: lo | hi | nan}."""
    def f(x):
        v = dcf_engine(**{**inp, key: x})["perShare"]
        return float("nan") if v is None else v - price
    a, b = lo, hi
    fa, fb = f(a), f(b)
    if fa != fa or fb != fb or abs(fa) == float("inf") or abs(fb) == float("inf"):
        return {"ok": False, "side": "nan"}
    if fa > 0 and fb > 0:
        return {"ok": False, "side": "lo"}
    if fa < 0 and fb < 0:
        return {"ok": False, "side": "hi"}
    i = 0
    while i < 200 and b - a > 1e-12:
        c = (a + b) / 2
        fc = f(c)
        if (fc < 0) == (fa < 0):
            a, fa = c, fc
        else:
            b = c
        i += 1
    return {"ok": True, "x": (a + b) / 2}


def ri_engine(roe, gT, r, bvps):
    pb = (roe - gT) / (r - gT)
    return {"pb": pb, "perShare": pb * bvps}


def fxc(v, frm, to, fx):
    if v is None:
        return None
    frm, to = frm or "USD", to or "USD"
    if frm == to:
        return v
    a, b = (fx or {}).get(frm), (fx or {}).get(to)
    return v / a * b if a and b else None


def margin_path(m_start, T, N, years=10):
    """dcfRampPath: margins for years 1..years, m_t = m_start + (T - m_start) * min(t / N, 1)."""
    return [m_start + (T - m_start) * min(t / N, 1) for t in range(1, years + 1)]


def dcf_prep(e, V, fx, seg, have_val=True):
    """dcfPrep: parameter-independent inputs of one entry in the share-price currency (incl. margin candidates and the guard)."""
    d = e["d"]
    ccy, price = d.get("ccy") or "USD", d.get("price")
    I = {"ccy": ccy, "price": price}
    fin = e.get("profile") == "financial"

    def na(why):
        I.update(mode="na", why=why)
        return I
    if not have_val:
        return na("还没有现金流估值数据（运行 python -m radar valuation）")
    if not fin and seg.get("peg") is False:
        return na("REIT 需 FFO/AFFO，暂不估值")
    if not price or price <= 0:
        return na("缺股价")
    impl = d["mcap"] * fx[ccy] / price if d.get("mcap") and (fx or {}).get(ccy) else None
    filed = ((V or {}).get("sh") or {}).get("v") or ((V or {}).get("sh_dil") or {}).get("v") or None
    if filed and (not impl or abs(filed / impl - 1) <= 0.25):
        I.update(shares=filed, sh_src="filed")
    elif impl:
        I.update(shares=impl, sh_src="mcap/price")
    else:
        return na("缺股数")
    if fin:
        if d.get("roe") is None:
            return na("没有 ROE（TTM）数据，剩余收益法算不了")
        eq = fxc(d.get("equity"), "USD", ccy, fx)
        if eq is None:
            return na("缺股东权益数据")
        if eq <= 0:
            return na("股东权益为负")
        I.update(mode="ri", roe=d["roe"] / 100, eq=eq, bvps=eq / I["shares"], pbNow=price / (eq / I["shares"]))
        return I
    if not V or not (V.get("fy") or []):
        return na((V or {}).get("why") or ("海外公司现金流数据未取到" if e.get("market") != "US" else "没有取到经营现金流 / 资本开支数据"))
    rc = V.get("ccy") or "USD"
    if rc != ccy and not ((fx or {}).get(rc) and (fx or {}).get(ccy)):
        return na(f"报表币种 {rc} 与股价币种 {ccy} 不同，缺汇率")

    def row(x):
        end, rev, ocf, capex = x[0], x[1], x[2], x[3]
        return {"end": end, "rev": rev, "m": (ocf - capex) / rev if _pos(rev) else None}
    mh = [row(x) for x in V["fy"]]
    ttm = row(V["ttm"]) if V.get("ttm") else None
    want = 5 if e.get("cyclical") else 3
    use = [x for x in mh if x["m"] is not None][-want:]
    if not use:
        return na("年度营收缺失，算不出 FCF 利润率")
    l3 = mh[-3:]
    I.update(mAvg=sum(x["m"] for x in use) / len(use), m0n=len(use), cyc=bool(e.get("cyclical")), mFy=mh[-1]["m"], mFyEnd=mh[-1]["end"],
             mTtm=ttm["m"] if ttm and ttm["m"] is not None else None, mL3=[x["m"] for x in l3],
             mUp=len(l3) == 3 and all(x["m"] is not None for x in l3) and l3[0]["m"] < l3[1]["m"] < l3[2]["m"],
             mPrev=mh[-2]["m"] if len(mh) >= 2 else None, guard=[])
    last_fy = mh[-1]["end"]
    if ttm and _pos(ttm["rev"]):
        I["R0"] = fxc(ttm["rev"], rc, ccy, fx)
    elif _pos(V.get("rev_ttm")) and V.get("rev_ttm_end") is not None and V["rev_ttm_end"] >= last_fy:
        I["R0"] = fxc(V["rev_ttm"], rc, ccy, fx)
    elif _pos(e.get("rev_ttm")):
        I["R0"] = fxc(e["rev_ttm"], "USD", ccy, fx)
    else:
        I["R0"] = fxc(mh[-1]["rev"], rc, ccy, fx)
    if not _pos(I["R0"]):
        return na("缺营收")
    bs = V.get("bs")
    I["netCash"] = fxc(bs["cash"] - bs["debt"], rc, ccy, fx) if bs else 0
    c = V.get("cons") or {}
    a, b, mn = c.get("+1y"), c.get("0y"), VAL_DEF["min_analysts"]
    I["gCons"] = ({"g": a["avg"] / b["avg"] - 1, "n": a.get("n")} if a and b and _pos(b.get("avg")) else
                  {"g": b["avg"] / b["year_ago"] - 1, "n": b.get("n")} if b and _pos(b.get("year_ago")) else None)
    if a and b and (a.get("n") or 0) >= mn and _pos(b.get("avg")):
        I.update(g0raw=a["avg"] / b["avg"] - 1, gkind="cons", gn=a.get("n"))
    elif b and (b.get("n") or 0) >= mn and _pos(b.get("year_ago")):
        I.update(g0raw=b["avg"] / b["year_ago"] - 1, gkind="cons", gn=b.get("n"))
    elif V.get("cagr3") is not None:
        I.update(g0raw=V["cagr3"] / 100, gkind="cagr")
    elif V.get("ttm_yoy") is not None:
        I.update(g0raw=V["ttm_yoy"] / 100, gkind="yoy")
    elif e.get("yoy") is not None:
        I.update(g0raw=e["yoy"] / 100, gkind="yoy")
    else:
        return na("没有可用的营收增速")
    I["guard"] = one_off_guard(I, mh[-1], ttm, a, b, ((e.get("mkt") or {}).get("eps_est") or {}), mn)
    I["mode"] = "fcf"
    return I


def one_off_guard(I, last_fy, ttm, rev1, rev0, eps_est, mn):
    """Rule-based one-off screen (only when a TTM margin exists) -> list of reasons (Chinese, as on the card).
    (a) TTM revenue > 1.5 x latest FY revenue; (b) consensus revenue +1y vs 0y < -30%; (c) consensus EPS +1y < 0.7 x 0y (both > 0);
    (b) and (c) need >= mn analysts in both years."""
    out = []
    if I.get("mTtm") is None:
        return out
    ok = lambda x, y: (x.get("n") or 0) >= mn and (y.get("n") or 0) >= mn  # noqa: E731
    if _pos(last_fy["rev"]) and ttm["rev"] > 1.5 * last_fy["rev"]:
        out.append(f"TTM 营收是最近财年的 {ttm['rev'] / last_fy['rev']:.2f} 倍（> 1.5 倍）")
    if rev1 and rev0 and ok(rev1, rev0) and _pos(rev0.get("avg")) and rev1["avg"] / rev0["avg"] - 1 < -0.30:
        out.append(f"一致预期下一财年营收 {_p(rev1['avg'] / rev0['avg'] - 1)}（{rev1.get('n')} 家，< −30%）")
    ea, eb = eps_est.get("+1y"), eps_est.get("0y")
    if ea and eb and ok(ea, eb) and _pos(ea.get("avg")) and _pos(eb.get("avg")) and ea["avg"] < eb["avg"] * 0.7:
        out.append(f"一致预期 EPS 下一财年 {ea['avg']:.2f} 不到本财年 {eb['avg']:.2f} 的 70%（{ea.get('n')} 家）")
    return out


def choose_basis(I, mode, N, T):
    """dcfBasis. mode: auto | avg3 | fy | ttm | ramp; N ramp years; T steady-state margin (decimal).
    -> {key: avg3 | fy | ttm | ramp, m0 (steady state T for ramp), ramp: {ms, N} | None, why, fb, [ms, msSrc, N]}"""
    avgL = f"近 {I['m0n']} 年平均"
    ttm_ok = I["mTtm"] is not None and not I["guard"]

    def pick(key, why, fb=False):
        return {"key": key, "m0": I["mTtm"] if key == "ttm" else I["mFy"] if key == "fy" else I["mAvg"], "ramp": None, "why": why, "fb": fb}

    def ramp(why):
        ms = I["mTtm"] if ttm_ok else I["mFy"]
        src = "TTM" if ttm_ok else "最近财年" + ("（没有 TTM）" if I["mTtm"] is None else "（TTM 因一次性收入未用）")
        return {"key": "ramp", "m0": T, "ramp": {"ms": ms, "N": N}, "ms": ms, "msSrc": src, "N": N, "fb": False,
                "why": f"{why}；起点 {_p(ms)}（{src}），第 {N} 年达到 {_p(T)}（用户设定，不是数据得出），之后不变，终值按第 10 年"}
    can_ramp = ttm_ok or I["mFy"] is not None
    l3 = I["mL3"]
    tr = " → ".join(_p(v) for v in l3) if len(l3) == 3 and all(v is not None for v in l3) else ""
    fy_up = I["mFy"] is not None and I["mPrev"] is not None and I["mFy"] > I["mPrev"]
    if mode == "auto":
        if I["cyc"]:
            return pick("avg3", f"自动 → {avgL}：周期性环节不用 TTM 和爬坡（可能是周期高点的现金流）")
        if ttm_ok and I["mTtm"] > I["mAvg"] and I["mUp"]:
            return pick("ttm", f"自动 → TTM：TTM {_p(I['mTtm'])} 高于{avgL} {_p(I['mAvg'])}，且近两个财年逐年改善（{tr}）")
        if not I["mAvg"] > 0 and fy_up:
            return ramp(f"自动 → 爬坡：{avgL} {_p(I['mAvg'])} ≤ 0，最近财年改善（{_p(I['mPrev'])} → {_p(I['mFy'])}）")
        why = ("没有 TTM 现金流数据" if I["mTtm"] is None else f"TTM {_p(I['mTtm'])} 疑似含一次性收入，不用" if not ttm_ok
               else f"TTM {_p(I['mTtm'])} 不高于{avgL} {_p(I['mAvg'])}" if not I["mTtm"] > I["mAvg"]
               else f"TTM {_p(I['mTtm'])} 高于{avgL} {_p(I['mAvg'])}，但" + (f"近两个财年没有逐年改善（{tr}）" if tr else "财年不足 3 年，判断不了趋势"))
        if not I["mAvg"] > 0:
            why += "；平均 ≤ 0 且最近财年没有改善" + ("（缺上一财年）" if I["mPrev"] is None else f"（{_p(I['mPrev'])} → {_p(I['mFy'])}）") + "，不做爬坡"
        return pick("avg3", f"自动 → {avgL}：{why}")
    if mode == "ttm":
        return (pick("ttm", "TTM（手动选择）" + ("；注意：疑似含一次性收入" if I["guard"] else "")) if I["mTtm"] is not None
                else pick("avg3", f"所选口径 TTM 缺数据，回退到{avgL}", True))
    if mode == "fy":
        return pick("fy", f"最近财年 FY {I['mFyEnd']}（手动选择）") if I["mFy"] is not None else pick("avg3", f"所选口径“最近财年”缺数据，回退到{avgL}", True)
    if mode == "ramp":
        return ramp("爬坡（手动选择）") if can_ramp else pick("avg3", f"所选口径“爬坡”缺起点利润率（TTM 和最近财年都没有），回退到{avgL}", True)
    return pick("avg3", f"{avgL}（手动选择）")


def default_params(val=None):
    """The page defaults (percent); rf from the valuation payload (data["val"] or the valuation file)."""
    rf = (val or {}).get("rf") or {}
    return {"rf": rf.get("value"), **{k: VAL_DEF[k] for k in ("erp", "gT", "r_amp", "g_amp", "m_amp", "roe_amp", "g_cap", "mbasis", "ramp_n", "ramp_t")}, "ov": {}}


def dcf_case(I, P, ticker=None):
    """dcfValue: parameter-dependent result for prepared inputs I. P = default_params(...) or the same keys (percent);
    P["ov"][ticker] = {"basis", "N", "T" (percent)} overrides the basis for that ticker. Returns mode (na | ri | fcf), sc (bear /
    base / bull with perShare), base, implied (g0 or ROE), revM (implied margin; ramp: steady-state T), revG, basis, m0, neg, warn."""
    o = {"mode": I["mode"], "why": I.get("why"), "warn": [], "implied": None, "baseDiff": None}
    if I["mode"] == "na":
        return o
    if P.get("rf") is None:
        o.update(mode="na", why="无风险利率未取到，请在“估值参数”里手动填写")
        return o
    rf = P["rf"] / 100
    r, ra = rf + P["erp"] / 100, P["r_amp"] / 100
    warn = []
    gB = P["gT"] / 100
    if gB > rf:
        gB = rf
        warn.append("gT>rf")

    def gFor(rr):
        if rr - gB > DCF_GAP:
            return gB
        if "gT_squeezed" not in warn:
            warn.append("gT_squeezed")
        return rr - DCF_GAP - 0.0001
    S = [("bear", r + ra), ("base", r), ("bull", r - ra)]
    o.update(r=r, rf=rf, gT=gFor(r), price=I["price"])
    if I["mode"] == "ri":
        ar = P["roe_amp"] / 100
        sc = []
        for k, rr in S:
            roe = I["roe"] if k == "base" else I["roe"] + (1 if k == "bull" else -1) * abs(I["roe"]) * ar
            gt = gFor(rr)
            x = ri_engine(roe, gt, rr, I["bvps"])
            sc.append({"k": k, "r": rr, "roe": roe, "gT": gt, **x, "diff": x["perShare"] / I["price"] - 1})
        o.update(sc=sc, base=sc[1], baseDiff=sc[1]["diff"], implied=o["gT"] + I["pbNow"] * (r - o["gT"]), warn=warn)
        return o
    lo, hi = VAL_DEF["g_floor"] / 100, P["g_cap"] / 100
    g0 = min(hi, max(lo, I["g0raw"]))
    o.update(g0=g0, capped=g0 != I["g0raw"])
    ov = (P.get("ov") or {}).get(ticker) if ticker else None
    B = choose_basis(I, ov["basis"] if ov else P["mbasis"], ov["N"] if ov else P["ramp_n"], (ov["T"] if ov else P["ramp_t"]) / 100)
    o.update(basis=B, m0=B["m0"], ov=bool(ov))
    base = {"R0": I["R0"], "m": B["m0"], "ramp": B["ramp"], "g0": g0, "gT": o["gT"], "r": r, "netCash": I["netCash"], "shares": I["shares"]}
    o["revM"] = dcf_reverse(base, I["price"], "m", -1, 1)
    if B["m0"] <= 0:
        o.update(neg=True, warn=warn)
        return o
    ag, am = P["g_amp"] / 100, P["m_amp"] / 100
    sc = []
    for k, rr in S:
        s = 0 if k == "base" else 1 if k == "bull" else -1
        g, m, gt = g0 + s * abs(g0) * ag, B["m0"] * (1 + s * am), gFor(rr)
        x = dcf_engine(**{**base, "r": rr, "g0": g, "m": m, "gT": gt})
        sc.append({"k": k, "r": rr, "g0": g, "m": m, "gT": gt, **x, "diff": x["perShare"] / I["price"] - 1})
    o.update(sc=sc, base=sc[1], baseDiff=sc[1]["diff"], neg=False, warn=warn)
    o["revG"] = dcf_reverse(base, I["price"], "g0", -0.30, 1.00)
    o["implied"] = o["revG"]["x"] if o["revG"]["ok"] else None
    return o


def seg_cfg(data, e):
    s = next((s for s in data["sectors"] if s["id"] == e["sector"]), None)
    return next((g for g in (s or {}).get("segments", []) if g["id"] == e["segment"]), {}) if s else {}


def dcf_entry(e, data, P=None):
    """Convenience: prep + case for one dashboard entry of the built data model (radar.build.model()); default params when P is None."""
    val = data.get("val")
    I = dcf_prep(e, ((val or {}).get("by") or {}).get(e["ticker"]), data.get("fx") or {}, seg_cfg(data, e), have_val=bool(val))
    return I, dcf_case(I, P or default_params(val), e["ticker"])
