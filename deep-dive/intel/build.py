"""Build the Intel company-level signal tracker.

Fetches public data (Yahoo daily prices, SEC EDGAR filings / XBRL / Form 4),
merges it with the hand-curated events.json, computes per-event market
reaction, and renders index.html from template.html.

Usage:  python build.py            (fetch live data)
        python build.py --offline  (rebuild from the latest raw snapshot)

Standard library only. stdout is English; all Chinese text lives in files.
"""
import datetime as dt
import glob
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")


def load_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


CFG = load_json("config.json")
SEC_HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT") or CFG["sec_user_agent"]}
# pages are written next to the main dashboard so the relative link from site/index.html works
SITE_DIR = os.path.join(ROOT, "..", "..", "site", "deep-dive", "intel")
WEB_HEADERS = {"User-Agent": "Mozilla/5.0"}


def http_get(url, headers, as_json=True, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            return json.loads(body) if as_json else body
        except Exception as exc:  # network hiccups: retry with backoff
            if attempt == retries - 1:
                raise RuntimeError(f"GET failed: {url} ({exc})") from exc
            time.sleep(1.5 * (attempt + 1))


def sec_get(url, as_json=True):
    time.sleep(0.15)  # stay well under SEC's 10 requests/second limit
    return http_get(url, SEC_HEADERS, as_json)


# ---------------------------------------------------------------- prices
def fetch_prices(symbol, start):
    p1 = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp()) - 14 * 86400
    p2 = int(time.time()) + 86400
    sym = urllib.request.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d"
    res = http_get(url, WEB_HEADERS)["chart"]["result"][0]
    off = res["meta"]["gmtoffset"]
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for ts, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        day = dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).strftime("%Y-%m-%d")
        out.append([day, round(c, 2)])
    return out


# ---------------------------------------------------------------- consensus estimates
def fetch_estimates(symbol, since):
    """Consensus estimates, EPS surprise history, price targets and rating
    changes from Yahoo quoteSummary (needs a session cookie + crumb).
    Returns None on failure so the build still succeeds."""
    import http.cookiejar
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.addheaders = [("User-Agent", WEB_HEADERS["User-Agent"])]
        try:
            opener.open("https://fc.yahoo.com", timeout=15)  # sets the session cookie; a 404 here is expected
        except Exception:
            pass
        crumb = opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode()
        mods = "earningsTrend,earningsHistory,financialData,recommendationTrend,upgradeDowngradeHistory"
        url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.request.quote(symbol)}"
               f"?modules={mods}&crumb={urllib.request.quote(crumb)}")
        r = json.load(opener.open(url, timeout=20))["quoteSummary"]["result"][0]
    except Exception as exc:
        print(f"WARNING: estimates unavailable ({exc})")
        return None

    raw = lambda d, k: (d.get(k) or {}).get("raw") if isinstance(d.get(k), dict) else d.get(k)
    est = lambda e: {k: raw(e, k) for k in ("avg", "low", "high", "numberOfAnalysts", "yearAgoEps", "yearAgoRevenue")}
    out = {"quarters": [], "history": [], "target": {}, "rec": {}, "ratings": []}
    for t in r["earningsTrend"]["trend"]:
        if t["period"] in ("0q", "+1q"):
            out["quarters"].append({"period": t["period"], "end": t["endDate"],
                                    "eps": est(t["earningsEstimate"]), "rev": est(t["revenueEstimate"])})
    for h in r["earningsHistory"]["history"]:
        out["history"].append({"end": (h.get("quarter") or {}).get("fmt"), "actual": raw(h, "epsActual"),
                               "estimate": raw(h, "epsEstimate"), "surprise": raw(h, "surprisePercent")})
    f = r["financialData"]
    out["target"] = {k: raw(f, k) for k in ("targetMeanPrice", "targetMedianPrice", "targetLowPrice",
                                             "targetHighPrice", "numberOfAnalystOpinions", "recommendationKey")}
    out["rec"] = r["recommendationTrend"]["trend"][0]
    for x in r["upgradeDowngradeHistory"]["history"]:
        day = dt.datetime.fromtimestamp(x["epochGradeDate"], dt.timezone.utc).strftime("%Y-%m-%d")
        if day >= since:
            out["ratings"].append({"date": day, "firm": x["firm"], "from": x.get("fromGrade"), "to": x.get("toGrade"),
                                   "action": x.get("action"), "pt_action": x.get("priceTargetAction"),
                                   "pt": x.get("currentPriceTarget"), "pt_prior": x.get("priorPriceTarget")})
    out["source"] = "Yahoo Finance quoteSummary"
    return out


# ---------------------------------------------------------------- EDGAR filings
def fetch_filings(cik, since, forms):
    rec = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")["filings"]["recent"]
    rows = []
    for i, form in enumerate(rec["form"]):
        if rec["filingDate"][i] < since or form not in forms:
            continue
        acc = rec["accessionNumber"][i]
        rows.append({
            "date": rec["filingDate"][i],
            "accept": rec["acceptanceDateTime"][i],
            "form": form,
            "items": rec["items"][i],
            "acc": acc,
            "doc": rec["primaryDocument"][i],
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{rec['primaryDocument'][i]}",
        })
    return rows


FORM4_CODES = {"P": "公开市场买入", "S": "公开市场卖出", "A": "授予", "F": "扣股缴税", "M": "行权 / 归属",
               "G": "赠与", "D": "返还发行人", "C": "转换", "J": "其他", "W": "继承"}


def parse_form4(cik, filing):
    """Return transaction rows from a Form 4 raw XML."""
    raw_doc = filing["doc"].split("/")[-1]  # strip the xsl rendering prefix
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{filing['acc'].replace('-', '')}/{raw_doc}"
    root = ET.fromstring(sec_get(url, as_json=False))
    txt = lambda node, path: (node.findtext(path) or "").strip()
    owner = txt(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    title = txt(rel, "officerTitle") if rel is not None else ""
    if not title and rel is not None and txt(rel, "isDirector") in ("1", "true"):
        title = "Director"
    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = txt(tx, "transactionCoding/transactionCode")
        shares = txt(tx, "transactionAmounts/transactionShares/value")
        price = txt(tx, "transactionAmounts/transactionPricePerShare/value")
        rows.append({
            "filed": filing["date"], "trade": txt(tx, "transactionDate/value"),
            "owner": owner, "title": title, "code": code, "code_cn": FORM4_CODES.get(code, code),
            "ad": txt(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "shares": float(shares) if shares else None,
            "price": float(price) if price else None,
            "url": filing["url"],
        })
    return rows


# ---------------------------------------------------------------- XBRL quarterly series
def quarter_label(end):
    d = dt.date.fromisoformat(end)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarterly_series(cik, tags):
    """Quarterly values for the tag with the most recent data (companies switch
    tags over time). Cash-flow items are YTD in 10-Qs, so single quarters are
    derived from consecutive cumulative values."""
    best = (None, [])
    for tag, series in _series_per_tag(cik, tags):
        if series and (not best[1] or series[-1]["end"] > best[1][-1]["end"]):
            best = (tag, series)
    return best


def _series_per_tag(cik, tags):
    for tag in tags:
        try:
            facts = sec_get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json")["units"]["USD"]
        except RuntimeError:
            continue
        facts = [f for f in facts if f.get("form") in ("10-Q", "10-K", "10-Q/A", "10-K/A") and "start" in f]
        latest = {}
        for f in facts:  # keep the most recently filed value for each period
            key = (f["start"], f["end"])
            if key not in latest or f["filed"] > latest[key]["filed"]:
                latest[key] = f
        days = lambda s, e: (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
        q = {}
        for (s, e), f in latest.items():
            if 80 <= days(s, e) <= 100:
                q[e] = f["val"]
        by_start = {}
        for (s, e), f in latest.items():
            by_start.setdefault(s, []).append((e, f["val"]))
        for s, lst in by_start.items():
            lst.sort()
            for (e0, v0), (e1, v1) in zip(lst, lst[1:]):
                if 80 <= days(e0, e1) <= 100 and e1 not in q:
                    q[e1] = v1 - v0
        yield tag, [{"end": e, "q": quarter_label(e), "val": v} for e, v in sorted(q.items())]


# ---------------------------------------------------------------- market reaction
def reactions(events, px, bench):
    days = [d for d, _ in px]
    pmap, bmap = dict(px), dict(bench)

    def ret(series, i0, i1):
        a, b = series.get(days[i0]), series.get(days[i1])
        return None if a is None or b is None else round((b / a - 1) * 100, 2)

    for ev in events:
        i = next((k for k, d in enumerate(days) if d >= ev["react"]), None)
        ev["react_day"] = days[i] if i is not None else None
        ev["r1"] = ev["b1"] = ev["x1"] = ev["x3"] = None
        if i is None or i == 0:
            continue
        ev["r1"], ev["b1"] = ret(pmap, i - 1, i), ret(bmap, i - 1, i)
        if ev["r1"] is not None and ev["b1"] is not None:
            ev["x1"] = round(ev["r1"] - ev["b1"], 2)
        if i + 2 < len(days):
            r3, b3 = ret(pmap, i - 1, i + 2), ret(bmap, i - 1, i + 2)
            if r3 is not None and b3 is not None:
                ev["x3"] = round(r3 - b3, 2)
    return events


# ---------------------------------------------------------------- main
def fetch_all():
    start = CFG["window_start"]
    raw = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    print("fetching prices ...")
    raw["px"] = fetch_prices(CFG["ticker"], start)
    raw["bench"] = fetch_prices(CFG["benchmark"], start)
    print("fetching consensus estimates ...")
    raw["estimates"] = fetch_estimates(CFG["ticker"], start)
    print("fetching EDGAR filings ...")
    raw["filings"] = fetch_filings(CFG["cik"], start, set(CFG["sec_forms"]))
    raw["form4"] = []
    for f in raw["filings"]:
        if f["form"] == "4":
            raw["form4"] += parse_form4(CFG["cik"], f)
    print("fetching XBRL financials ...")
    fin = {}
    for key, tags in [("revenue", CFG["revenue_tags"]), ("gross", ["GrossProfit"]), ("capex", CFG["capex_tags"])]:
        fin[key] = quarterly_series(CFG["cik"], tags)[1]
    raw["fin"] = fin
    raw["hyper"] = []
    for h in CFG["hyperscalers"]:
        tag, series = quarterly_series(h["cik"], CFG["capex_tags"])
        raw["hyper"].append({"name": h["name"], "tag": tag, "series": series})
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"raw_{dt.date.today():%Y%m%d}.json")
    if os.path.exists(path):  # snapshots are never overwritten
        path = path.replace(".json", f"_{dt.datetime.now():%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    print(f"raw snapshot saved: {os.path.relpath(path, ROOT)}")
    return raw


def load_latest_raw():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "raw_*.json")), key=os.path.getmtime)
    if not files:
        sys.exit("no raw snapshot found; run without --offline first")
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def build(raw):
    ev = load_json("events.json")
    start = CFG["window_start"]
    px = [p for p in raw["px"] if p[0] >= start]
    # keep one prior day so the first event in the window still has a base close
    full_px, full_b = raw["px"], raw["bench"]
    common = sorted(set(d for d, _ in full_px) & set(d for d, _ in full_b))
    full_px = [p for p in full_px if p[0] in common]
    full_b = [p for p in full_b if p[0] in common]

    groups = {"company": ev["company_events"], "third": ev["third_party_events"], "industry": ev["industry_events"]}
    for k, lst in groups.items():
        reactions(lst, full_px, full_b)

    # Intel quarterly financials table
    fin = raw["fin"]
    rev = {r["end"]: r for r in fin["revenue"]}
    gross = {r["end"]: r["val"] for r in fin["gross"]}
    capex = {r["q"]: r["val"] for r in fin["capex"]}
    quarters = []
    for end in sorted(rev)[-CFG["financial_quarters"]:]:
        r = rev[end]
        quarters.append({"q": r["q"], "end": end, "rev": r["val"], "gross": gross.get(end),
                         "gm": round(gross[end] / r["val"] * 100, 1) if end in gross else None,
                         "capex": capex.get(r["q"])})

    # hyperscaler capex by calendar quarter
    hyper_q = {}
    for h in raw["hyper"]:
        for s in h["series"]:
            hyper_q.setdefault(s["q"], {})[h["name"]] = s["val"]
    names = [h["name"] for h in raw["hyper"]]
    complete = sorted(q for q, v in hyper_q.items() if all(n in v for n in names))
    hyper_rows = [{"q": q, "by": hyper_q[q], "total": sum(hyper_q[q].values())} for q in complete[-6:]]

    data = {
        "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fetched_at": raw["fetched_at"],
        "ticker": CFG["ticker"], "benchmark_name": CFG["benchmark_name"],
        "px": px, "bench": [b for b in raw["bench"] if b[0] >= start],
        "company": groups["company"], "third": groups["third"], "industry": groups["industry"],
        "indicators": ev["indicators"], "calendar": ev["calendar"],
        "filings": [f for f in raw["filings"] if f["form"] != "4"],
        "form4": raw["form4"], "quarters": quarters,
        "hyper": hyper_rows, "hyper_names": names,
        "estimates": raw.get("estimates"),
        "guidance": ev.get("guidance", []), "beats": ev.get("beats", []),
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    for tpl_name, out_name in [("template.html", "index.html"), ("analysis_template.html", "analysis.html")]:
        with open(os.path.join(ROOT, tpl_name), encoding="utf-8") as f:
            tpl = f.read()
        os.makedirs(SITE_DIR, exist_ok=True)
        with open(os.path.join(SITE_DIR, out_name), "w", encoding="utf-8") as f:
            f.write(tpl.replace("/*__DATA__*/null", blob))
    print(f"index.html + analysis.html written: {len(px)} trading days, {len(data['filings'])} filings, "
          f"{len(data['form4'])} form4 rows, {len(quarters)} quarters, {len(hyper_rows)} hyperscaler quarters")


if __name__ == "__main__":
    build(load_latest_raw() if "--offline" in sys.argv else fetch_all())
