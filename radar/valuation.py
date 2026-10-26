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


def latest():
    files = sorted(glob.glob(os.path.join(DATA, "valuation_*.json")), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    with open(files[0], encoding="utf-8") as f:
        return json.load(f)
