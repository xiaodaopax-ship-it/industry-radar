"""Milestone tracker for single companies (display only; never used by scores, levels, the ledger or the basket score).

    python -m radar milestones   SEC filings for every company in WATCH -> data/milestones_<ts>.json, then rebuild

Per quarter, from primary SEC documents only:
  * 10-Q / 10-K inline XBRL: segment revenue and segment operating income (the segment member in WATCH, operating-segment
    context, three-month duration; the 10-K fourth quarter = fiscal year minus the three reported quarters), total revenue;
  * 10-Q / 10-K text: the segment's external revenue sentence ("External revenue was $X million in Q3 2026, ... due to ...");
  * 8-K item 2.02 earnings release (exhibit 99): reported revenue, GAAP / non-GAAP gross margin, and the guidance for the next
    quarter (revenue range, GAAP / non-GAAP gross margin, GAAP / non-GAAP EPS);
  * 10-Q / 10-K text: sentences that mention the milestone keywords (process nodes, external customers, risk production ...),
    so the page can show which statements are new compared with the previous filing.
Filings are immutable, so each parsed filing is cached forever in data/cache/ms/<accession>.json.
Regex parsing of filing text can miss values when the wording changes; missing values are shown as "—", never guessed.
stdout is English only.
"""
import datetime as dt
import glob
import hashlib
import html as H
import json
import os
import re
import urllib.request

from .paths import CACHE, DATA

WATCH = {
    "INTC": {"cik": 50863, "name": "Intel", "segment": "intc:IntelFoundryMember", "segment_cn": "Intel 代工（Intel Foundry）",
             "keywords": r"Intel 18A|18A-P|Intel 14A|external (?:foundry )?customers?|design wins?|risk production|high[- ]volume (?:production|manufacturing)|Panther Lake|Clearwater Forest|Fab 52|committed demand|process design kit|PDK",
             # accounting / financial-statement sentences that mention a keyword but are not milestones
             "exclude": r"Intersegment revenue|Operating (?:loss|income) was|Gross profit|period charges|product profit|Altera|revenue (?:was|increased|decreased)|totaled \$|inventory|depreciation|impairment|fair value|Note \d|basis on which we report",
             "since": "2024-01-01"},
}
MDIR = os.path.join(CACHE, "ms")


def log(msg):
    print(msg, flush=True)


def _ua():
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise SystemExit("set SEC_USER_AGENT (e.g. 'industry-radar you@mail.com') before running")
    return {"User-Agent": ua}


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_ua()), timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def _text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", h)
    t = H.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", t))


def _num(s):
    try:
        return float(s.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def xbrl_facts(h):
    """[(name, start, end, members, value)] from inline XBRL (values in units, sign applied)."""
    ctx = {}
    for m in re.finditer(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', h, re.S):
        body = m.group(2)
        mem = tuple(sorted(re.findall(r'<xbrldi:explicitMember dimension="[^"]+">([^<]+)</xbrldi:explicitMember>', body)))
        s, e = re.search(r"<xbrli:startDate>([^<]+)", body), re.search(r"<xbrli:endDate>([^<]+)", body)
        ctx[m.group(1)] = (s.group(1) if s else None, e.group(1) if e else None, mem)
    att = lambda tag, a: (re.search(a + r'="([^"]*)"', tag) or [None, None])[1]
    out = []
    for m in re.finditer(r"(<ix:nonFraction\b[^>]*>)(.*?)</ix:nonFraction>", h, re.S):
        tag = m.group(1)
        v = _num(re.sub(r"<[^>]+>", "", m.group(2)))
        c = ctx.get(att(tag, "contextRef"))
        if v is None or not c or not c[0]:
            continue
        v *= 10 ** int(att(tag, "scale") or 0)
        if att(tag, "sign") == "-":
            v = -v
        out.append((att(tag, "name"), c[0], c[1], c[2], v))
    return out


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def parse_periodic(h, seg, kw, report_date, exclude=None):
    """Segment revenue / operating income (3-month, or fiscal year for a 10-K), total revenue, external revenue, milestone sentences."""
    facts = xbrl_facts(h)
    pick = lambda name, mem, ln: next((v for n, s, e, m, v in facts if n == name and e == report_date and m == mem and ln(_days(s, e))), None)
    q = lambda d: 80 <= d <= 100
    y = lambda d: 350 <= d <= 380
    sm = tuple(sorted((seg, "us-gaap:OperatingSegmentsMember")))
    revtags = ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues")
    out = {}
    for lab, ln in (("q", q), ("fy", y)):
        out[lab] = {"seg_rev": next((pick(t, sm, ln) for t in revtags if pick(t, sm, ln) is not None), None),
                    "seg_op": pick("us-gaap:OperatingIncomeLoss", sm, ln),
                    "rev": next((pick(t, (), ln) for t in revtags if pick(t, (), ln) is not None), None),
                    "gp": pick("us-gaap:GrossProfit", (), ln)}
    t = _text(h)
    m = re.search(r"External revenue was \$([\d.,]+) (million|billion) in (Q\d \d{4}|\d{4})[^.]*\.", t)
    out["ext"] = None if not m else {"v": _num(m.group(1)) * (1e6 if m.group(2) == "million" else 1e9), "period": m.group(3),
                                     "text": m.group(0).strip()[:400]}
    sents = re.split(r"(?<=[.;])\s+(?=[A-Z])", t)
    rk = re.compile(kw)
    keep, seen = [], set()
    for s in sents:
        s = " ".join(s.split())
        if 60 <= len(s) <= 700 and rk.search(s) and not re.search(r"forward-looking|could cause|risk factors|we cannot assure", s, re.I) \
                and not (exclude and re.search(exclude, s)):
            k = re.sub(r"\d", "#", s.lower())
            if k not in seen:
                seen.add(k)
                keep.append(s)
    out["sentences"] = keep[:40]
    return out


def parse_release(t):
    """Reported revenue, GAAP / non-GAAP gross margin and next-quarter guidance from an earnings release (plain text)."""
    r = {}
    m = re.search(r"[Qq]uarter revenue (?:was|of) \$([\d.]+) billion", t)
    r["rev"] = _num(m.group(1)) * 1e9 if m else None
    m = re.search(r"Gross margin ([\d.]+)% ([\d.]+)% (?:up|down|flat)[^%]*?(?:ppts?)? ([\d.]+)% ([\d.]+)%", t)
    r["gm_gaap"], r["gm_ng"] = (_num(m.group(1)), _num(m.group(3))) if m else (None, None)
    m = re.search(r"[Ff]orecasting (\w+)-quarter (\d{4}) revenue of \$([\d.]+) billion to \$([\d.]+) billion", t)
    r["guide"] = None
    if m:
        g = {"q": m.group(1), "year": m.group(2), "lo": _num(m.group(3)) * 1e9, "hi": _num(m.group(4)) * 1e9}
        i = t.find("Business Outlook")
        seg = t[i:i + 1500] if i >= 0 else ""
        mm = re.search(r"Gross margin ([\d.]+)% ([\d.]+)%", seg)
        g["gm_gaap"], g["gm_ng"] = (_num(mm.group(1)), _num(mm.group(2))) if mm else (None, None)
        me = re.search(r"Per Share[^\n$]*?\$ ?\(?(-?[\d.]+)\)?\s+\$ ?\(?(-?[\d.]+)\)?", seg)
        g["eps_gaap"], g["eps_ng"] = (_num(me.group(1).rstrip(".")), _num(me.group(2).rstrip("."))) if me else (None, None)
        r["guide"] = g
    return r


def _cached(acc, fn):
    os.makedirs(MDIR, exist_ok=True)
    p = os.path.join(MDIR, f"{acc}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    v = fn()
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)
    return v


def company(ticker, w):
    cik = w["cik"]
    sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))["filings"]["recent"]
    rows = [{k: sub[k][i] for k in ("form", "filingDate", "accessionNumber", "primaryDocument", "reportDate", "items")}
            for i in range(len(sub["form"])) if sub["filingDate"][i] >= w["since"]]
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}"
    per = []
    for r in [r for r in rows if r["form"] in ("10-Q", "10-K")]:
        acc = r["accessionNumber"].replace("-", "")
        url = f"{base}/{acc}/{r['primaryDocument']}"
        # cache key includes the keyword / exclude patterns, so editing them re-parses the filings
        tag = hashlib.sha1((w["keywords"] + "|" + (w.get("exclude") or "")).encode()).hexdigest()[:8]
        p = _cached(f"{acc}_{tag}", lambda: parse_periodic(_get(url), w["segment"], w["keywords"], r["reportDate"], w.get("exclude")))
        per.append({"form": r["form"], "filed": r["filingDate"], "end": r["reportDate"], "url": url, **p})
    rel = []
    for r in [r for r in rows if r["form"] == "8-K" and "2.02" in (r.get("items") or "")]:
        acc = r["accessionNumber"].replace("-", "")

        def load(acc=acc):
            idx = json.loads(_get(f"{base}/{acc}/index.json"))["directory"]["item"]
            htm = [x for x in idx if x["name"].lower().endswith(".htm") and not x["name"].lower().startswith(("index", r["primaryDocument"].lower()))]
            ex = [x["name"] for x in htm if re.search(r"ex-?99|99-?1|earnings|release", x["name"], re.I)] or \
                 [x["name"] for x in sorted(htm, key=lambda x: -int(x.get("size") or 0))]
            if not ex:
                return {"doc": None}
            return {"doc": f"{base}/{acc}/{ex[0]}", **parse_release(_text(_get(f"{base}/{acc}/{ex[0]}")))}
        rel.append({"filed": r["filingDate"], **_cached(acc + "_rel", load)})
    per.sort(key=lambda x: x["end"])
    rel.sort(key=lambda x: x["filed"])
    # one row per fiscal quarter; the 10-K quarter = fiscal year minus the three 10-Q quarters of the same fiscal year
    quarters = []
    for i, p in enumerate(per):
        if p["form"] == "10-Q":
            v = dict(p["q"])
        else:
            prev = [x for x in per[max(0, i - 3):i] if x["form"] == "10-Q"]
            v = {}
            for k in ("seg_rev", "seg_op", "rev", "gp"):
                fy, qs = p["fy"].get(k), [x["q"].get(k) for x in prev]
                v[k] = fy - sum(qs) if fy is not None and len(qs) == 3 and None not in qs else None
        # the release filed for this quarter: the last 8-K 2.02 filed after the period end and on or before the report filing
        r = next((x for x in reversed(rel) if p["end"] < x["filed"] <= p["filed"]), None)
        g_prev = next((x.get("guide") for x in reversed(rel) if x["filed"] <= p["end"] and x.get("guide")), None)
        ext = p.get("ext") or {}
        quarters.append({"end": p["end"], "form": p["form"], "filed": p["filed"], "url": p["url"], "release": (r or {}).get("doc"),
                         "rev": v.get("rev") or (r or {}).get("rev"), "seg_rev": v.get("seg_rev"), "seg_op": v.get("seg_op"),
                         "gm_gaap": (r or {}).get("gm_gaap") or (round(v["gp"] / v["rev"] * 100, 1) if v.get("gp") and v.get("rev") else None),
                         "gm_ng": (r or {}).get("gm_ng"), "ext": ext.get("v") if ext.get("period", "").startswith("Q") or p["form"] == "10-Q" else None,
                         "ext_text": ext.get("text"), "guide_prev": g_prev, "guide_next": (r or {}).get("guide"),
                         "sentences": p.get("sentences") or []})
    # statements never seen in any earlier filing (digits and quarter labels ignored, so updated numbers do not count as new;
    # comparing with every earlier filing keeps 10-K wording that repeats 10-Q wording from showing up as new)
    norm = lambda s: re.sub(r"\b(q[1-4]|ytd|first|second|third|fourth|nine|six|three)\b", "", re.sub(r"[\d$,.%]", "", s.lower())).split()
    seen = set()
    for i, q in enumerate(quarters):
        keys = [" ".join(norm(s)) for s in q["sentences"]]
        q["new"] = [s for s, k in zip(q["sentences"], keys) if k not in seen] if i else []
        seen.update(keys)
    return {"ticker": ticker, "name": w["name"], "segment_cn": w["segment_cn"], "quarters": quarters}


def run():
    t0 = dt.datetime.now()
    out = {}
    for t, w in WATCH.items():
        try:
            out[t] = company(t, w)
            q = out[t]["quarters"]
            log(f"{t}: {len(q)} quarters ({q[0]['end'] if q else '-'} .. {q[-1]['end'] if q else '-'}), "
                f"latest seg_rev={q[-1].get('seg_rev') if q else None} seg_op={q[-1].get('seg_op') if q else None} ext={q[-1].get('ext') if q else None}")
        except Exception as e:  # noqa: BLE001
            log(f"{t}: failed ({type(e).__name__}: {e})")
    path = os.path.join(DATA, f"milestones_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"fetched_at": t0.isoformat(timespec="seconds"), "items": out}, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    log(f"saved {os.path.relpath(path, DATA)}")
    return path


def latest():
    fs = sorted(glob.glob(os.path.join(DATA, "milestones_*.json")))
    if not fs:
        return None
    with open(fs[-1], encoding="utf-8") as f:
        return json.load(f)
