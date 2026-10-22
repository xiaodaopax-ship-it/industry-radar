"""Market data and non-US fundamentals from Yahoo Finance, normalised to USD."""
import datetime as dt

from .net import raw
from .paths import CFG

MODULES = "price,summaryDetail,financialData,defaultKeyStatistics,earningsHistory,earningsTrend,calendarEvents,assetProfile"
TS_Q = ["quarterlyTotalRevenue", "quarterlyGrossProfit", "quarterlyOperatingIncome", "quarterlyNetIncome",
        "quarterlyDilutedAverageShares", "quarterlyCashAndCashEquivalents"]
TS_A = [t.replace("quarterly", "annual") for t in TS_Q]


def fx_rates(yh):
    """Local currency units per 1 USD."""
    rates = {"USD": 1.0}
    for ccy, sym in CFG["intl"]["fx"].items():
        c = yh.chart(sym, "5d")
        p = (c or {}).get("meta", {}).get("price")
        if p:
            rates[ccy] = 1 / p if sym.startswith(ccy + "USD") else p  # EURUSD=X quotes USD per EUR
    return rates


def usd(v, ccy, fx):
    if v is None:
        return None
    r = fx.get(ccy or "USD")
    return v / r if r else None


def market(yh, symbol, fx):
    r = yh.summary(symbol, MODULES)
    if not r:
        return None
    p, sd, f, k = (r.get(x) or {} for x in ("price", "summaryDetail", "financialData", "defaultKeyStatistics"))
    ccy = p.get("currency") or "USD"
    hist = [{"end": (h.get("quarter") or {}).get("fmt"), "actual": raw(h, "epsActual"), "est": raw(h, "epsEstimate")}
            for h in (r.get("earningsHistory") or {}).get("history", [])]
    trend = {t["period"]: t for t in (r.get("earningsTrend") or {}).get("trend", [])}
    nq = trend.get("0q") or {}
    price = raw(p, "regularMarketPrice")
    ed = ((r.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
    ap = r.get("assetProfile") or {}
    summary = first_sentence(ap.get("longBusinessSummary") or "")
    return {
        "name": p.get("longName") or p.get("shortName"), "currency": ccy,
        "price": price, "mcap": usd(raw(p, "marketCap"), ccy, fx),
        "hi52": raw(sd, "fiftyTwoWeekHigh"), "lo52": raw(sd, "fiftyTwoWeekLow"),
        "analysts": raw(f, "numberOfAnalystOpinions"), "target": raw(f, "targetMedianPrice"), "rec": f.get("recommendationKey"),
        "insiders": raw(k, "heldPercentInsiders"), "institutions": raw(k, "heldPercentInstitutions"),
        "eps_hist": [h for h in hist if h["actual"] is not None],
        "next_q": {"end": nq.get("endDate"), "eps": raw(nq.get("earningsEstimate"), "avg"),
                   "rev": usd(raw(nq.get("revenueEstimate"), "avg"), ccy, fx), "n": raw(nq.get("revenueEstimate"), "numberOfAnalysts")} if nq else None,
        "next_date": dt.datetime.fromtimestamp(ed[0]["raw"], dt.timezone.utc).strftime("%Y-%m-%d") if ed else None,
        "eps_est": eps_estimates(trend, price, ccy, fx),
        "eps_g5y": round(raw(trend["+5y"], "growth") * 100, 1) if raw(trend.get("+5y"), "growth") is not None else None,  # informational
        "profile": {"industry": ap.get("industry"), "sector": ap.get("sector"), "country": ap.get("country"),
                    "employees": ap.get("fullTimeEmployees"), "website": ap.get("website"),
                    "summary": summary},
    }


def eps_estimates(trend, price, ccy, fx):
    """Consensus EPS for the current (0y) and next (+1y) fiscal year from earningsTrend.

    Values stay in the estimate currency (earningsCurrency, usually the reporting currency). yield_pct is the 0y
    estimate as a share of the share price, both converted to USD, so a too-small base can be flagged in any currency.
    """
    out = {}
    for k in ("0y", "+1y"):
        t = trend.get(k)
        e = (t or {}).get("earningsEstimate") or {}
        if raw(e, "avg") is None:
            continue
        ec = e.get("earningsCurrency") or ccy
        eu, pu = usd(raw(e, "avg"), ec, fx), usd(price, ccy, fx)
        out[k] = {"avg": raw(e, "avg"), "n": raw(e, "numberOfAnalysts"), "end_date": t.get("endDate"), "ccy": ec,
                  "year_ago": raw(e, "yearAgoEps"), "yield_pct": round(eu / pu * 100, 2) if eu is not None and pu else None}
    return out or None


FIN_TS = ["PretaxIncome", "StockholdersEquity", "TotalEquityGrossMinorityInterest", "CreditLossesProvision", "ProvisionForDoubtfulAccounts"]


def financial_fields(yh, symbol, fx, f):
    """Pretax income, equity and credit-loss provision for financial-profile leaders, aligned with fundamentals() periods.

    A separate request, so the cached time series of every other company stays exactly as before.
    Yahoo reports CreditLossesProvision as a negative number (an expense); ProvisionForDoubtfulAccounts as positive.
    """
    pre = "quarterly" if f.get("basis") == "quarter" else "annual"
    ts = yh.timeseries(symbol, [pre + k for k in FIN_TS], years=3 if pre == "quarterly" else 4)
    by = {k: {x["date"]: x["val"] for x in _series(ts, pre + k, fx)} for k in FIN_TS}
    q0 = f["q_end"]
    dates = sorted({d for s in by.values() for d in s})
    ly = next((d for d in reversed(dates) if 330 <= _days(d, q0) <= 400), None)
    out = {}
    for key, src in (("pretax", "PretaxIncome"), ("equity", "StockholdersEquity")):
        if by[src].get(q0) is not None:
            out[key], out[key + "_ly"], out[key + "_tag"] = by[src][q0], by[src].get(ly), "yahoo:" + src
    if by["TotalEquityGrossMinorityInterest"].get(q0) is not None:
        out["equity_total"] = by["TotalEquityGrossMinorityInterest"][q0]
    cl, pda = by["CreditLossesProvision"], by["ProvisionForDoubtfulAccounts"]
    if cl.get(q0) is not None:
        out["prov"], out["prov_ly"], out["prov_tag"] = -cl[q0], (-cl[ly] if cl.get(ly) is not None else None), "yahoo:CreditLossesProvision"
    elif pda.get(q0) is not None:
        out["prov"], out["prov_ly"], out["prov_tag"] = pda[q0], pda.get(ly), "yahoo:ProvisionForDoubtfulAccounts"
    return out


def first_sentence(text, min_len=60, max_len=300):
    """First real sentence; skips periods in 'Inc.', 'Co.', 'Ltd.' and similar by requiring a minimum length."""
    text = " ".join(text.replace("\xa0", " ").split())
    i = text.find(". ", min_len)
    return (text[: i + 1] if 0 < i < max_len else text[:max_len]).strip()


def _series(ts, key, fx):
    return [{"date": x["date"], "val": usd(x["val"], x["ccy"], fx)} for x in ts.get(key, []) if x["val"] is not None]


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def fundamentals(yh, symbol, fx):
    """Same fields as the SEC rows. Uses quarterly data when available, otherwise annual (e.g. Japan)."""
    ts = yh.timeseries(symbol, TS_Q)
    basis, pre = "quarter", "quarterly"
    if len(ts.get("quarterlyTotalRevenue", [])) < 5:
        ts, basis, pre = yh.timeseries(symbol, TS_A, years=4), "annual", "annual"
    rev = _series(ts, pre + "TotalRevenue", fx)
    if len(rev) < 2:
        return None
    by = lambda key: {x["date"]: x["val"] for x in _series(ts, pre + key, fx)}
    gp, op, ni, sh, cash = by("GrossProfit"), by("OperatingIncome"), by("NetIncome"), by("DilutedAverageShares"), by("CashAndCashEquivalents")
    sh = {x["date"]: x["val"] for x in ts.get(pre + "DilutedAverageShares", [])}  # shares are not money
    q0 = rev[-1]
    ly = next((x for x in reversed(rev[:-1]) if 330 <= _days(x["date"], q0["date"]) <= 400), None)
    if not ly or not ly["val"] or ly["val"] <= 0:
        return None
    pct = lambda a, b: round(a / b * 100, 1) if a is not None and b else None
    if basis == "quarter":
        last4 = rev[-4:]
        ttm = sum(x["val"] for x in last4) if len(last4) == 4 and _days(last4[0]["date"], last4[-1]["date"]) <= 300 else None
        ni4 = [ni.get(x["date"]) for x in last4]
        ni_ttm = sum(ni4) if ttm and None not in ni4 else None
    else:
        ttm, ni_ttm = q0["val"], ni.get(q0["date"])
    # year-ago net income on the same basis: only annual data reaches back far enough (Yahoo gives ~5 quarters,
    # a year-ago TTM needs 8), so quarterly-basis rows have no historical PEG fallback
    ni_ly = ni.get(ly["date"]) if basis == "annual" else None
    s0, sly = sh.get(q0["date"]), sh.get(ly["date"])
    return {"q_end": q0["date"], "basis": basis, "rev_q": q0["val"], "rev_q_ly": ly["val"],
            "yoy": round((q0["val"] / ly["val"] - 1) * 100, 1), "yoy_prev": None,
            "rev_ttm": ttm, "ni_ttm": ni_ttm, "ni_ttm_ly": ni_ly,
            "gm": pct(gp.get(q0["date"]), q0["val"]), "gm_ly": pct(gp.get(ly["date"]), ly["val"]),
            "opm": pct(op.get(q0["date"]), q0["val"]), "opm_ly": pct(op.get(ly["date"]), ly["val"]),
            "dilution": round((s0 / sly - 1) * 100, 1) if s0 and sly else None, "cash": cash.get(q0["date"])}
