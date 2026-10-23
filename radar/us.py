"""US universe from SEC EDGAR: XBRL frames (every 10-Q/10-K filer), submissions, Form 4."""
import concurrent.futures as cf
import datetime as dt
import functools
import json
import os
import time
import xml.etree.ElementTree as ET

from .net import sec
from .paths import CACHE, CFG


def periods(cur):
    y, q = int(cur[2:6]), int(cur[-1])
    pq = (y, q - 1) if q > 1 else (y - 1, 4)
    lab = lambda yy, qq: f"CY{yy}Q{qq}"
    return {"q0": lab(y, q), "q1": lab(*pq), "q0_ly": lab(y - 1, q), "q1_ly": lab(pq[0] - 1, pq[1]), "fy": f"CY{y - 1}"}


@functools.lru_cache(maxsize=None)  # the same frame is requested by several tag lists in one run
def _frame(tag, period, unit="USD"):
    d = sec(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json")
    return {row["cik"]: row for row in (d or {}).get("data", [])}


def _merged(tags, period):
    out = {}
    for t in tags:  # earlier tags take precedence
        for cik, row in _frame(t, period).items():
            if cik not in out:
                out[cik] = {**row, "tag": t}
    return out


SHARES = "WeightedAverageNumberOfDilutedSharesOutstanding"


def _qlab(y, q, back=0):
    i = y * 4 + q - 1 - back
    return i // 4, i % 4 + 1


def _ni_ttm_frames(y, q):
    """Net income of the 4 calendar quarters ending CY{y}Q{q} from frames: annual CY{y-1} + quarters 1..q of y - quarters 1..q of y-1.

    Fourth quarters are rarely filed on their own (they sit inside the 10-K, so CYxxxxQ4 frames cover only ~1/5 of filers);
    the annual frame stands in for them, exactly like ni_ttm in fundamentals(). Returns [(frame label, sign)].
    """
    if q == 4:
        return [(f"CY{y}", 1)]
    return [(f"CY{y - 1}", 1)] + [(f"CY{y}Q{i}", 1) for i in range(1, q + 1)] + [(f"CY{y - 1}Q{i}", -1) for i in range(1, q + 1)]


def _ni_check(fr, cik, year):
    """Annual vs quarterly consistency of one fiscal year: implied Q4 = annual - Q1 - Q2 - Q3 (frames). Catches unit / tag errors."""
    a = fr[f"CY{year}"].get(cik, {}).get("val")
    qs = [fr[f"CY{year}Q{i}"].get(cik, {}).get("val") for i in (1, 2, 3)]
    if a is None or None in qs:
        return None
    q4, s3 = a - sum(qs), sum(qs)
    # a large positive implied Q4 is common and real (tax valuation allowance releases, seasonality); an annual figure far
    # below three profitable quarters is the typical signature of a wrong tag / unit (e.g. SCHW's annual NetIncomeLoss)
    if s3 > 0 and q4 < -0.75 * s3:
        return f"CY{year} 年度净利润 {a / 1e6:,.0f} 百万与前三季合计 {s3 / 1e6:,.0f} 百万不衔接（推算第四季度 {q4 / 1e6:,.0f} 百万）"
    return None


def _avg_shares(vals):
    """Average diluted shares of a TTM window; drops values off by > 3x from the median (thousands-vs-units tagging errors)."""
    vals = [x for x in vals if x]
    if not vals:
        return None, 0
    med = sorted(vals)[len(vals) // 2]
    good = [x for x in vals if med / 3 <= x <= med * 3]
    return (sum(good) / len(good) if len(good) >= 3 else None), len(vals) - len(good)


def hist_eps(ciks=None):
    """Two TTM windows (ending at the current quarter and one year earlier) of net income and average diluted shares.

    Used for the historical PEG (TTM diluted EPS growth). Frames: NetIncomeLoss annual CY{y-2}, CY{y-1} and quarters 1-3 of both
    years plus the current year's quarters; WeightedAverageNumberOfDilutedSharesOutstanding for the 8 quarters (Q4 replaced by the
    annual weighted average when the Q4 frame is missing). One dict per CIK; ciks=None returns every filer with any NI frame.
    """
    y, q = (int(x) for x in (CFG["current_quarter"][2:6], CFG["current_quarter"][-1]))
    wins = {"cur": (y, q), "ly": (y - 1, q)}
    labs = {p for w in wins.values() for p, _ in _ni_ttm_frames(*w)} | {f"CY{yy}Q{i}" for yy in (y - 2, y - 1) for i in (1, 2, 3)} | {f"CY{y - 2}", f"CY{y - 1}"}
    fr = {p: _frame("NetIncomeLoss", p) for p in sorted(labs)}
    qtrs = {k: [_qlab(*w, back=b) for b in range(4)] for k, w in wins.items()}
    slabs = {f"CY{a}Q{b}" for v in qtrs.values() for a, b in v} | {f"CY{a}" for v in qtrs.values() for a, b in v if b == 4}
    sh = {p: _frame(SHARES, p, "shares") for p in sorted(slabs)}
    out = {}
    for cik in (ciks if ciks is not None else set().union(*fr.values())):
        h = {"src": "frames", "miss": []}
        for k, w in wins.items():
            parts = _ni_ttm_frames(*w)
            vals = [fr[p].get(cik, {}).get("val") for p, _ in parts]
            miss = [p for (p, _), x in zip(parts, vals) if x is None]
            h["ni" if k == "cur" else "ni_ly"] = None if miss else sum(s * x for (_, s), x in zip(parts, vals))
            h["miss"] += miss
            svals = []
            for a, b in qtrs[k]:
                x = sh[f"CY{a}Q{b}"].get(cik, {}).get("val")
                svals.append(x if x is not None or b != 4 else sh[f"CY{a}"].get(cik, {}).get("val"))
            avg, dropped = _avg_shares(svals)
            h["sh" if k == "cur" else "sh_ly"] = avg
            if dropped:
                h["sh_dropped"] = h.get("sh_dropped", 0) + dropped
        odd = [t for t in (_ni_check(fr, cik, y - 1), _ni_check(fr, cik, y - 2)) if t]
        if odd:
            h["odd"] = "；".join(odd)
        out[cik] = h
    return out


def _concept(cik, tag, unit):
    d = sec(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json")
    facts = {}
    for f in ((d or {}).get("units") or {}).get(unit, []):
        if f.get("start") and f.get("end"):
            facts[(f["start"], f["end"])] = f["val"]  # later filings come later in the list: restated values win
    return facts


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def concept_hist(cik):
    """Fallback for leaders missing from frames: TTM net income from companyconcept facts (annual + YTD - prior YTD).

    The current window ends at the latest period end between 100 days before and 50 days after the end of the current
    calendar quarter (a filer whose latest 10-Q is not in XBRL yet uses the quarter before, flagged "stale"); the year-ago
    window ends ~365 days earlier. NetIncomeLoss first; if that cannot fill both windows, NetIncomeLoss merged with ProfitLoss
    for the missing periods (some filers moved to ProfitLoss, e.g. AVGO since FY2025; ProfitLoss includes NCI).
    Shares: the 3-month diluted weighted averages inside each window (annual figure fills gaps).
    """
    y, q = int(CFG["current_quarter"][2:6]), int(CFG["current_quarter"][-1])
    cal_end = dt.date(y, q * 3, 30 if q in (2, 3) else 31).isoformat()
    a, b = _concept(cik, "NetIncomeLoss", "USD"), _concept(cik, "ProfitLoss", "USD")
    if not a and not b:
        return {"src": "concept", "why": "SEC 没有 us-gaap 净利润数据（可能是 20-F / 40-F 申报人，用 IFRS）"}
    sh = _concept(cik, SHARES, "shares")
    best = None
    for tag, ni in (("NetIncomeLoss", a), ("NetIncomeLoss+ProfitLoss", {**b, **a})):
        if not ni:
            continue
        h = _concept_windows(ni, sh, cal_end)
        h["tag"] = tag
        if h.get("ni") is not None and h.get("ni_ly") is not None:
            return h
        best = best if best and best.get("ni") is not None else h
    return best


def _concept_windows(ni, sh, cal_end):
    def ttm(end):
        dur = {s: v for (s, e), v in ni.items() if e == end}
        for s, v in dur.items():
            if 350 <= _days(s, end) <= 380:
                return v
        ytd = [(s, v) for s, v in dur.items() if 80 <= _days(s, end) <= 290]
        if not ytd:
            return None
        s0, v0 = min(ytd)  # the longest year-to-date figure
        n = _days(s0, end)
        fy = next((v for (s, e), v in ni.items() if abs(_days(e, s0)) <= 7 and 350 <= _days(s, e) <= 380), None)
        prev = next((v for (s, e), v in ni.items() if abs(_days(e, end) - 365) <= 10 and abs(_days(s, e) - n) <= 10), None)
        return None if fy is None or prev is None else fy + v0 - prev

    def shares(end):
        lo = dt.date.fromisoformat(end) - dt.timedelta(days=345)
        qs = sorted({e: v for (s, e), v in sh.items() if 80 <= _days(s, e) <= 100 and lo.isoformat() <= e <= end}.items())
        vals = [v for _, v in qs][-4:]
        if len(vals) < 4:
            vals += [v for (s, e), v in sh.items() if 350 <= _days(s, e) <= 380 and lo.isoformat() <= e <= end][:4 - len(vals)]
        return _avg_shares(vals)[0]

    ends = sorted({e for _, e in ni if -100 <= _days(cal_end, e) <= 50})
    if not ends:
        return {"src": "concept", "why": f"SEC 申报里没有截至 {CFG['current_quarter']} 附近的净利润"}
    end = ends[-1]
    ly_end = next((e for _, e in sorted(ni, key=lambda k: k[1]) if abs(_days(e, end) - 365) <= 15), None)
    h = {"src": "concept", "end": end, "stale": _days(end, cal_end) > 50, "ni": ttm(end), "ni_ly": ttm(ly_end) if ly_end else None,
         "sh": shares(end), "sh_ly": shares(ly_end) if ly_end else None, "miss": []}
    if h["ni"] is None or h["ni_ly"] is None:
        h["miss"] = ["companyconcept " + ("当期" if h["ni"] is None else "一年前") + " TTM"]
    return h


def _financial(P, rev, ni, v, ttm):
    """Extra fields for the financial evaluation profile (brokers, exchanges, lenders).

    Revenue uses a different tag precedence: brokers and lenders often tag only their fee income with
    RevenueFromContractWithCustomer..., while the total (incl. net interest) is in RevenuesNetOfInterestExpense
    or Revenues. The alternative revenue is kept in r["rev_fin"] and only used for financial-profile segments,
    so standard-profile results do not change.
    """
    F = CFG["financial_tags"]
    frev = {k: _merged(F["revenue"], p) for k, p in P.items()}
    pre = {k: _merged(F["pretax"], P[k]) for k in ("q0", "q0_ly")}
    eq = {k: _merged(F["equity"], P[k] + "I") for k in ("q0", "q0_ly")}
    prov = {k: _merged(F["provision"], P[k]) for k in ("q0", "q0_ly")}
    nii = _merged(F["net_interest"], P["q0"])
    eq_total = _frame("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", P["q0"] + "I")
    out = {}
    for cik in set(rev["q0"]) | set(frev["q0"]):
        f = {}
        r0 = frev["q0"].get(cik)
        q0, q0ly, q1, q1ly = (v(frev[k], cik) for k in ("q0", "q0_ly", "q1", "q1_ly"))
        if r0 and q0ly and q0ly > 0:
            f["_alt"] = {"rev_q": q0, "rev_q_ly": q0ly, "yoy": round((q0 / q0ly - 1) * 100, 1),
                         "yoy_prev": round((q1 / q1ly - 1) * 100, 1) if q1 and q1ly and q1ly > 0 else None,
                         "rev_ttm": ttm(frev, cik), "rev_tag": r0["tag"], "q_end": r0["end"],
                         "_name": r0["entityName"], "_loc": r0.get("loc")}
        for key, src in (("pretax", pre), ("prov", prov)):
            if v(src["q0"], cik) is not None:
                f[key], f[key + "_ly"], f[key + "_tag"] = v(src["q0"], cik), v(src["q0_ly"], cik), src["q0"][cik]["tag"]
        if v(eq["q0"], cik) is not None:
            f["equity"], f["equity_ly"], f["equity_tag"] = v(eq["q0"], cik), v(eq["q0_ly"], cik), eq["q0"][cik]["tag"]
        if v(eq_total, cik) is not None:
            f["equity_total"] = v(eq_total, cik)  # incl. non-controlling interests: flags IBKR-style structures
        if v(nii, cik) is not None:
            f["nii"] = v(nii, cik)
        if v(ni["q0"], cik) is not None and v(ni["q1"], cik) is not None:
            f["ni_q2"] = v(ni["q0"], cik) + v(ni["q1"], cik)  # last two quarters, a sanity check on the TTM figure
        out[cik] = f
    return out


def fundamentals(financial=False):
    """One row per US filer with comparable quarterly revenue. All money in USD.

    financial=True also pulls the fields used by the financial profile (pretax income, equity, credit-loss
    provision, alternative revenue) and adds filers that only have revenue under the financial tag list;
    those rows carry rev_q = None, so standard-profile screens skip them.
    """
    P = periods(CFG["current_quarter"])
    rev = {k: _merged(CFG["revenue_tags"], p) for k, p in P.items()}
    ni = {k: _frame("NetIncomeLoss", p) for k, p in P.items()}
    gp = {k: _frame("GrossProfit", P[k]) for k in ("q0", "q0_ly")}
    op = {k: _frame("OperatingIncomeLoss", P[k]) for k in ("q0", "q0_ly")}
    sh = {k: _frame("WeightedAverageNumberOfDilutedSharesOutstanding", P[k], "shares") for k in ("q0", "q0_ly")}
    cash = _frame("CashAndCashEquivalentsAtCarryingValue", P["q0"] + "I")
    v = lambda d, cik: d.get(cik, {}).get("val")
    pct = lambda a, b: round(a / b * 100, 1) if a is not None and b else None

    def ttm(src, cik):
        parts = [v(src[k], cik) for k in ("fy", "q0", "q1", "q0_ly", "q1_ly")]
        return None if None in parts else parts[0] + parts[1] + parts[2] - parts[3] - parts[4]

    hist = hist_eps()
    rows = []
    for cik, r0 in rev["q0"].items():
        q0, q0ly, q1, q1ly = r0["val"], v(rev["q0_ly"], cik), v(rev["q1"], cik), v(rev["q1_ly"], cik)
        if not q0ly or q0ly <= 0:
            continue
        s0, sly = v(sh["q0"], cik), v(sh["q0_ly"], cik)
        rows.append({
            "market": "US", "cik": cik, "name": r0["entityName"], "loc": r0.get("loc"), "q_end": r0["end"], "basis": "quarter",
            "rev_q": q0, "rev_q_ly": q0ly, "yoy": round((q0 / q0ly - 1) * 100, 1),
            "yoy_prev": round((q1 / q1ly - 1) * 100, 1) if q1 and q1ly and q1ly > 0 else None,
            "rev_ttm": ttm(rev, cik), "ni_ttm": ttm(ni, cik),
            "gm": pct(v(gp["q0"], cik), q0), "gm_ly": pct(v(gp["q0_ly"], cik), q0ly),
            "opm": pct(v(op["q0"], cik), q0), "opm_ly": pct(v(op["q0_ly"], cik), q0ly),
            "dilution": round((s0 / sly - 1) * 100, 1) if s0 and sly else None, "cash": v(cash, cik),
            "eps_h": hist.get(cik),
        })
    if not financial:
        return P, rows
    fin = _financial(P, rev, ni, v, ttm)
    have = set()
    for r in rows:
        have.add(r["cik"])
        f = dict(fin.get(r["cik"], {}))
        alt = f.pop("_alt", None)
        if alt and (alt["rev_q"], alt["rev_q_ly"], alt["rev_ttm"]) != (r["rev_q"], r["rev_q_ly"], r["rev_ttm"]):
            f["rev_fin"] = {k: x for k, x in alt.items() if not k.startswith("_")}
        r.update(f)
    for cik, f in fin.items():  # filers whose revenue exists only under the financial tag list (e.g. RevenuesNetOfInterestExpense)
        alt = f.get("_alt")
        if cik in have or not alt:
            continue
        sh0, shly = v(sh["q0"], cik), v(sh["q0_ly"], cik)
        rows.append({
            "market": "US", "cik": cik, "name": alt["_name"], "loc": alt["_loc"], "q_end": alt["q_end"], "basis": "quarter",
            "rev_q": None, "rev_q_ly": None, "yoy": None, "yoy_prev": None, "rev_ttm": None, "ni_ttm": ttm(ni, cik),
            "gm": None, "gm_ly": None, "opm": pct(v(op["q0"], cik), alt["rev_q"]), "opm_ly": pct(v(op["q0_ly"], cik), alt["rev_q_ly"]),
            "dilution": round((sh0 / shly - 1) * 100, 1) if sh0 and shly else None, "cash": v(cash, cik),
            "rev_fin": {k: x for k, x in alt.items() if not k.startswith("_")}, "eps_h": hist.get(cik),
            **{k: x for k, x in f.items() if k != "_alt"}})
    return P, rows


def submissions(cik):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"sub_{cik}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CFG["cache_days"]["submissions"] * 86400:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    d = sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json") or {}
    rec = d.get("filings", {}).get("recent", {})
    keep = {"sic": int(d["sic"]) if d.get("sic") else None, "sic_desc": d.get("sicDescription"),
            "tickers": d.get("tickers", []), "exchanges": d.get("exchanges", []),
            "form4": [{"date": rec["filingDate"][i], "acc": rec["accessionNumber"][i], "doc": rec["primaryDocument"][i]}
                      for i, f in enumerate(rec.get("form", [])) if f == "4"][:60]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keep, f)
    return keep


def _parse_form4(cik, f):
    """Open-market (P / S) rows of one Form 4. Filings never change, so results are cached by accession."""
    path = os.path.join(CACHE, f"f4_{f['acc']}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['acc'].replace('-', '')}/{f['doc'].split('/')[-1]}"
    try:
        root = ET.fromstring(sec(url, as_json=False))
    except Exception:
        return None  # not cached: retried next run
    owner = (root.findtext("reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    txs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
        if code not in ("P", "S"):
            continue
        try:
            n = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
            px = float(tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        txs.append({"date": f["date"], "owner": owner, "code": code, "shares": n, "price": px})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(txs, fh)
    return txs


def _insider(cik, filings, since):
    recent = [x for x in filings if x["date"] >= since]
    out = {"buys": 0, "sells": 0, "buy_val": 0.0, "sell_val": 0.0, "rows": [],
           "checked": min(len(recent), CFG["form4"]["max_filings_per_company"]), "total": len(recent)}
    for f in recent[:CFG["form4"]["max_filings_per_company"]]:
        for t in _parse_form4(cik, f) or []:
            k = "buy" if t["code"] == "P" else "sell"
            out[k + "s"] += 1
            out[k + "_val"] += t["shares"] * t["price"]
            out["rows"].append(t)
    out["rows"] = out["rows"][:8]
    out["buy_val"], out["sell_val"] = round(out["buy_val"]), round(out["sell_val"])
    return out


def insiders(rows, since, log):
    os.makedirs(CACHE, exist_ok=True)
    with cf.ThreadPoolExecutor(max_workers=6) as pool:  # still globally rate-limited inside sec()
        futs = {pool.submit(_insider, r["cik"], r.pop("_form4"), since): r for r in rows}
        for i, fut in enumerate(cf.as_completed(futs)):
            futs[fut]["insider"] = fut.result()
            if (i + 1) % 25 == 0:
                log(f"  form4 {i + 1}/{len(rows)}")
