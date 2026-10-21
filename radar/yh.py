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
    ed = ((r.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
    ap = r.get("assetProfile") or {}
    summary = first_sentence(ap.get("longBusinessSummary") or "")
    return {
        "name": p.get("longName") or p.get("shortName"), "currency": ccy,
        "price": raw(p, "regularMarketPrice"), "mcap": usd(raw(p, "marketCap"), ccy, fx),
        "hi52": raw(sd, "fiftyTwoWeekHigh"), "lo52": raw(sd, "fiftyTwoWeekLow"),
        "analysts": raw(f, "numberOfAnalystOpinions"), "target": raw(f, "targetMedianPrice"), "rec": f.get("recommendationKey"),
        "insiders": raw(k, "heldPercentInsiders"), "institutions": raw(k, "heldPercentInstitutions"),
        "eps_hist": [h for h in hist if h["actual"] is not None],
        "next_q": {"end": nq.get("endDate"), "eps": raw(nq.get("earningsEstimate"), "avg"),
                   "rev": usd(raw(nq.get("revenueEstimate"), "avg"), ccy, fx), "n": raw(nq.get("revenueEstimate"), "numberOfAnalysts")} if nq else None,
        "next_date": dt.datetime.fromtimestamp(ed[0]["raw"], dt.timezone.utc).strftime("%Y-%m-%d") if ed else None,
        "profile": {"industry": ap.get("industry"), "sector": ap.get("sector"), "country": ap.get("country"),
                    "employees": ap.get("fullTimeEmployees"), "website": ap.get("website"),
                    "summary": summary},
    }


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
    s0, sly = sh.get(q0["date"]), sh.get(ly["date"])
    return {"q_end": q0["date"], "basis": basis, "rev_q": q0["val"], "rev_q_ly": ly["val"],
            "yoy": round((q0["val"] / ly["val"] - 1) * 100, 1), "yoy_prev": None,
            "rev_ttm": ttm, "ni_ttm": ni_ttm,
            "gm": pct(gp.get(q0["date"]), q0["val"]), "gm_ly": pct(gp.get(ly["date"]), ly["val"]),
            "opm": pct(op.get(q0["date"]), q0["val"]), "opm_ly": pct(op.get(ly["date"]), ly["val"]),
            "dilution": round((s0 / sly - 1) * 100, 1) if s0 and sly else None, "cash": cash.get(q0["date"])}
