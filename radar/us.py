"""US universe from SEC EDGAR: XBRL frames (every 10-Q/10-K filer), submissions, Form 4."""
import concurrent.futures as cf
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


def _frame(tag, period, unit="USD"):
    d = sec(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json")
    return {row["cik"]: row for row in (d or {}).get("data", [])}


def _merged(tags, period):
    out = {}
    for t in tags:  # earlier tags take precedence
        for cik, row in _frame(t, period).items():
            out.setdefault(cik, row)
    return out


def fundamentals():
    """One row per US filer with comparable quarterly revenue. All money in USD."""
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
