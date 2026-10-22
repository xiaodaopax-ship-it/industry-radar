"""US universe from SEC EDGAR: XBRL frames (every 10-Q/10-K filer), submissions, Form 4."""
import concurrent.futures as cf
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
            "rev_fin": {k: x for k, x in alt.items() if not k.startswith("_")},
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
