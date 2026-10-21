"""Automatic evidence extraction for the qualitative layer.

US companies (SEC filings):
  - latest 10-K: business overview, customer-concentration sentences, competition paragraph,
    employee count, risk-factor headings
  - R&D expense as % of revenue for the last fiscal years (XBRL companyfacts)
  - executive changes: 8-K item 5.02 in the last 365 days; links to 10-K / 10-Q / DEF 14A
Non-US companies (Yahoo profile): business summary, officers, employees.

This only collects quotes and links; it never writes judgements. Output: data/research_auto_<date>.json.
    python -m radar research
"""
import datetime as dt
import glob
import html
import json
import os
import re

from .classify import sectors
from .net import Yahoo, sec
from .paths import CACHE, CFG, DATA

_CUST = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s?%\s+of\s+(?:our\s+|the\s+company'?s\s+)?(?:total\s+|net\s+|consolidated\s+)*(?:revenue|revenues|sales|net sales)", re.I)


def _cached_json(name, url, days):
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and dt.datetime.now().timestamp() - os.path.getmtime(path) < days * 86400:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    d = sec(url)
    if d is not None:
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
    return d


def ticker_map():
    d = _cached_json("company_tickers.json", "https://www.sec.gov/files/company_tickers.json", 7) or {}
    return {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}


def _doc_text(cik, acc, doc):
    """Download a filing document once (filings never change) and return (html, text)."""
    path = os.path.join(CACHE, "docs", f"{acc}.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    else:
        body = sec(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}", as_json=False)
        if body is None:
            return "", ""
        raw = body.decode("utf-8", errors="ignore")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    # non-breaking spaces between "Item" and the number break the section regexes
    raw = re.sub(r"&#160;|&nbsp;|&#xa0;|&#xA0;|\xa0", " ", raw)
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return raw, t


def _section(text, start_pat, end_pat, min_len=3000):
    """Body of a 10-K item: skip the table-of-contents hit by requiring a long gap before the next item."""
    best = ""
    for m in re.finditer(start_pat, text, re.I):
        nxt = re.search(end_pat, text[m.end():], re.I)
        body = text[m.end(): m.end() + (nxt.start() if nxt else 200000)]
        if len(body) > len(best):
            best = body
        if len(best) >= min_len:
            break
    return best


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", text.replace("\n", " ")) if 40 < len(s.strip()) < 600]


def _risk_headings(raw):
    """Risk-factor headings are usually bold or italic runs inside Item 1A."""
    raw = html.unescape(raw)  # entities such as &#8217; would otherwise hide "Management's"
    tag = r"(?:\s|</?[^>]+>)*"  # any mix of whitespace and tags between words
    starts = [m.start() for m in re.finditer(rf"(?i)item{tag}1a{tag}[.:\-–—]?{tag}risk{tag}factors", raw)]
    if not starts:
        return []
    seg = ""
    end = re.compile(rf"(?i)item{tag}1b\b|item{tag}1c\b|item{tag}2{tag}[.:\-–—]?{tag}properties|unresolved{tag}staff{tag}comments")
    stop = re.compile(rf"(?i)item{tag}7{tag}[.:\-–—]?{tag}management|management['’]s{tag}discussion{tag}and{tag}analysis")
    for s in starts:  # the real section is the long one, not the table of contents
        e = end.search(raw, s + 200)
        cand = raw[s: e.start() if e else s + 400000]
        st = stop.search(cand, 200)  # never run into MD&A when the 1B / Item 2 marker is missing
        cand = cand[: st.start()] if st else cand
        if len(cand) > len(seg):
            seg = cand
    pats = [r"(?is)<(?:b|strong)\b[^>]*>(.*?)</(?:b|strong)>",
            r"(?is)<span[^>]*font-weight:\s*(?:bold|[6-9]00)[^>]*>(.*?)</span>",
            r"(?is)<(?:p|div)[^>]*font-weight:\s*(?:bold|[6-9]00)[^>]*>(.*?)</(?:p|div)>",
            r"(?is)<span[^>]*font-style:\s*italic[^>]*>(.*?)</span>"]
    out, seen = [], set()
    for p in pats:
        for m in re.finditer(p, seg):
            t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split())
            noise = re.search(r"(?i)report of independent|consolidated|critical accounting|internal control|accounting pronouncement|table of contents|^note\b|refer to note", t)
            if 50 <= len(t) <= 320 and t[0].isupper() and t.lower() not in seen and not noise and not t.lower().startswith(("item ", "risks related", "risk factors")):
                seen.add(t.lower())
                out.append(t)
        if len(out) >= 6:
            break
    # real risk headings describe a possible harm; this drops stray bold section titles
    out = [t for t in out if not t.endswith(":") and re.search(
        r"(?i)\b(may|could|might|risks?|depend|adverse|fail|subject to|loss|harm|unable|volatil|competi|cannot|rely|uncertain|exposed)", t)]
    return out[:12] if len(out) >= 3 else []  # a couple of stray bold lines are noise, not headings


def _rd_pct(cik):
    facts = _cached_json(f"facts_{cik}.json", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", 7)
    g = ((facts or {}).get("facts") or {}).get("us-gaap") or {}

    def annual(tag):
        out = {}
        for f in ((g.get(tag) or {}).get("units") or {}).get("USD", []):
            if f.get("form") == "10-K" and f.get("fp") == "FY" and "start" in f:
                days = (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
                if 350 <= days <= 380:
                    out[f["end"]] = f["val"]
        return out

    rd = annual("ResearchAndDevelopmentExpense") or annual("ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost")
    rev = {}
    for t in CFG["revenue_tags"]:
        for k, v in annual(t).items():
            rev.setdefault(k, v)
    rows = [{"fy_end": k, "rd": rd[k], "rev": rev[k], "pct": round(rd[k] / rev[k] * 100, 1)} for k in sorted(rd) if rev.get(k)]
    return rows[-3:]


def us_company(ticker, cik):
    sub = _cached_json(f"subfull_{cik}.json", f"https://data.sec.gov/submissions/CIK{cik:010d}.json", 3) or {}
    rec = (sub.get("filings") or {}).get("recent") or {}
    f = [{"form": rec["form"][i], "date": rec["filingDate"][i], "acc": rec["accessionNumber"][i], "doc": rec["primaryDocument"][i],
          "items": rec.get("items", [""] * len(rec["form"]))[i]} for i in range(len(rec.get("form", [])))]
    url = lambda x: f"https://www.sec.gov/Archives/edgar/data/{cik}/{x['acc'].replace('-', '')}/{x['doc']}"
    latest = lambda forms: next((x for x in f if x["form"] in forms), None)
    # foreign private issuers (TSM, ASML) file 20-F annual reports instead of 10-K
    k10, q10, proxy = latest(("10-K", "20-F", "40-F")), latest(("10-Q", "6-K")), latest(("DEF 14A",))
    since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    out = {"ticker": ticker, "cik": cik, "market": "US",
           "links": {k: {"date": v["date"], "url": url(v)} for k, v in (("10-K", k10), ("10-Q", q10), ("DEF 14A", proxy)) if v},
           "exec_changes": [{"date": x["date"], "url": url(x)} for x in f if x["form"] == "8-K" and "5.02" in (x["items"] or "") and x["date"] >= since][:8],
           "rd": _rd_pct(cik)}
    if k10:
        raw, text = _doc_text(cik, k10["acc"], k10["doc"])
        if k10["form"] == "10-K":
            biz = _section(text, r"item\s*1\s*[.:\-–—]?\s*business\b", r"item\s*1a\s*[.:\-–—]?\s*risk\s+factors")
        else:  # 20-F: Item 4 "Information on the Company", Item 3.D "Risk Factors"
            biz = _section(text, r"item\s*4\s*[.:\-–—]?\s*information\s+on\s+the\s+company", r"item\s*4a\s*[.:\-–—]?|item\s*5\s*[.:\-–—]?\s*operating")
        paras = [p.strip() for p in biz.split("\n") if len(p.strip()) > 150]
        out["business"] = " ".join(paras[:2])[:900]
        sents = _sentences(text)
        out["customers"] = list(dict.fromkeys(s for s in sents if _CUST.search(s) and re.search(r"customer|distributor|client|partner|payer|medicare", s, re.I)))[:4]
        comp = re.search(r"\n\s*Competition\s*\n(.{200,1500}?)\n", biz, re.S)
        out["competition"] = " ".join(comp.group(1).split())[:700] if comp else ""
        emp = next((s for s in sents if re.search(r"\b(?:approximately|about|had|employed)\s+[\d,]{2,}\s+(?:full-time\s+)?(?:employees|people)", s, re.I)), "")
        out["employees"] = emp[:300]
        out["risk_headings"] = _risk_headings(raw)
        out["filing"] = {"form": k10["form"], "date": k10["date"]}
    return out


def intl_company(yh, ticker):
    r = yh.summary(ticker, "assetProfile") or {}
    ap = r.get("assetProfile") or {}
    return {"ticker": ticker, "market": "INTL",
            "business": " ".join((ap.get("longBusinessSummary") or "").split())[:900],
            "employees": ap.get("fullTimeEmployees"), "website": ap.get("website"),
            "officers": [{"name": o.get("name"), "title": o.get("title"), "age": o.get("age")} for o in (ap.get("companyOfficers") or [])[:6]],
            "links": {"Yahoo": {"date": "", "url": f"https://finance.yahoo.com/quote/{ticker}/profile"}}}


def targets():
    """Research-priority pools from the latest build plus all configured leaders."""
    site = os.path.join(os.path.dirname(DATA), "site", "index.html")
    pool = set()
    if os.path.exists(site):
        with open(site, encoding="utf-8") as f:
            m = re.search(r"const D = (\{.*?\});\n", f.read(), re.S)
        if m:
            pool = {e["ticker"] for e in json.loads(m.group(1))["entries"] if e.get("pool")}
    leaders = {t for s in sectors() for g in s["segments"] for t in g.get("leaders", [])}
    manual = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(os.path.dirname(DATA), "config", "research", "*.json"))}
    return sorted(pool | leaders | manual)


def run():
    tick = ticker_map()
    yh = None
    out = {}
    syms = targets()
    print(f"research: extracting evidence for {len(syms)} companies ...", flush=True)
    for i, t in enumerate(syms):
        try:
            if "." not in t and t.upper() in tick:
                out[t] = us_company(t, tick[t.upper()])
                if not out[t].get("business"):  # unusual 10-K layout (e.g. no "Item 1. Business" heading)
                    yh = yh or Yahoo()
                    out[t]["business"] = intl_company(yh, t)["business"]
                    out[t]["business_src"] = "yahoo"
            else:
                yh = yh or Yahoo()
                out[t] = intl_company(yh, t)
        except Exception as exc:  # one bad filing must not stop the batch
            out[t] = {"ticker": t, "error": str(exc)[:200]}
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(syms)}", flush=True)
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"research_auto_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "companies": out}, f, ensure_ascii=False)
    ok = sum(1 for v in out.values() if v.get("business"))
    print(f"research auto saved: {path} ({ok}/{len(out)} with business text)")


def latest_auto():
    files = sorted(glob.glob(os.path.join(DATA, "research_auto_*.json")), key=os.path.getmtime)
    if not files:
        return {}
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f).get("companies", {})


def manual():
    out = {}
    for p in glob.glob(os.path.join(os.path.dirname(DATA), "config", "research", "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            out[d.get("ticker") or os.path.basename(p)[:-5]] = d
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: skipped {os.path.basename(p)} ({exc})")
    return out
