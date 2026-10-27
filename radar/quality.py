"""价值通道 (value channel) for US filers: a second entry to the funnel that does NOT require revenue growth.
Research tooling, not investment advice. It lists companies with long-run quality (5 calendar-aligned fiscal years of ROE,
free cash flow, cash conversion, moderate leverage) and ranks them on quality + valuation yields. It does not change the
growth screen, factors, levels, the PEG screen, the ledger or the comparison-basket score.

    python -m radar quality    SEC XBRL frames (every filer, ~100 requests) + submissions + Yahoo market data for the
                               passers -> data/quality_<ts>.json, then rebuild

Sources:
  SEC XBRL frames API (https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json): one value per filer per
  calendar-aligned period. Annual durations CY{y} (a ~365-day fiscal year assigned to the calendar year it overlaps most) for
  revenue (config revenue_tags, first tag wins), NetIncomeLoss, NetCashProvidedByUsedInOperatingActivities(+ContinuingOperations),
  PaymentsToAcquirePropertyPlantAndEquipment / PaymentsToAcquireProductiveAssets, PaymentsOfDividends(+CommonStock),
  PaymentsForRepurchaseOfCommonStock; instants CY{y}Q4I (closest to Dec 31) for StockholdersEquity (y-1 .. y, for average
  equity), Goodwill, CashAndCashEquivalentsAtCarryingValue, LongTermDebt (else LongTermDebtNoncurrent + LongTermDebtCurrent)
  + ShortTermBorrowings + CommercialPaper. Frames for closed years are cached 30 days in data/cache/frames/.
  SEC submissions (cached sub_{cik}.json, 7 days): ticker, exchange, SIC. Yahoo quoteSummary (radar/yh.market, 1-day cache):
  price and market cap, only for companies that pass the fundamental rules.
Rules (VALUE_RULES): latest fiscal-year revenue >= $200M and net income > 0; ROE (NI / average parent equity; years with
  equity <= 0 skipped) in >= 4 of 5 years, median >= 12%, minimum >= 5%; FCF (OCF - capex) > 0 in >= 4 of 5 years; 5-year
  sum OCF / sum NI >= 0.8; revenue FY(y) >= FY(y-4); net debt / mean FCF <= 4 years; listed on Nasdaq / NYSE / NYSE American;
  not financial (SIC 60-67: banks, insurers, brokers, REITs need other models). Missing capex tag in every year counts as
  capex 0 (flagged). Goodwill / (equity + debt) > 50% is flagged, not a rule.
  Ranking (non-cyclical passers): equal-weight mean of in-channel percentiles of median ROE, minimum ROE, cash conversion,
  FCF yield (5-year mean FCF / market cap), earnings yield (latest FY NI / market cap) and shareholder yield (latest FY
  dividends + buybacks / market cap; buybacks are gross, stock compensation is not netted). Cyclical: SIC major groups of
  radar/heat.CYCLICAL_MAJOR or a cyclical segment in config/sectors.json; listed, not ranked.
  Display only: reverse DCF "现价隐含增速" (radar/valuation.dcf_reverse, the page engine): R0 = latest FY revenue, m = mean FCF
  margin of the last 3 years (> 0), r = rf + 5%, gT = min(2.5%, rf), net cash = cash - debt, shares = mcap / price;
  compared with the 4-year revenue CAGR. A description of what the price assumes, not a signal.
Limits: frames align fiscal years to calendar years (a fiscal year ending in Feb counts for the previous calendar year);
  restated values are the latest filed; companies that switch revenue tags can show a gap; stdout is English only.
"""
import datetime as dt
import glob
import json
import os
import statistics

from .net import sec
from .paths import CACHE, CFG, DATA

VALUE_RULES = {"n_fy": 5, "rev_min": 2e8, "roe_years_min": 4, "roe_median_min": 12.0, "roe_min_min": 5.0, "fcf_pos_min": 4,
               "conv_min": 0.8, "nd_fcf_max": 4.0, "goodwill_warn_pct": 50.0, "erp": 5.0, "gT": 2.5}
# Leverage is judged as net debt / mean FCF (years of free cash flow to repay net debt), not debt / equity: buybacks shrink
# book equity of many US companies (some have negative equity), which makes debt / equity misleading. Goodwill is shown and
# flagged (goodwill / (equity + debt) > goodwill_warn_pct), not a hard rule. ROE is skipped in years with equity <= 0.
FDIR = os.path.join(CACHE, "frames")
REV = CFG["revenue_tags"]
DUR = {"rev": REV, "ni": ["NetIncomeLoss"],
       "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
       "cx": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
       "div": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"], "bb": ["PaymentsForRepurchaseOfCommonStock"]}
INST = {"eq": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "gw": ["Goodwill"], "cash": ["CashAndCashEquivalentsAtCarryingValue"],
        "ltd": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", "DebtInstrumentCarryingAmount"],
        "ltdn": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermNotesPayable", "SeniorNotes"],
        "ltdc": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "NotesPayableCurrent"],
        "stb": ["ShortTermBorrowings"], "cp": ["CommercialPaper"]}
INST_ALL_YEARS = ("eq",)            # the other instants are only needed for the latest year


def log(msg):
    print(msg, flush=True)


def years_for(today=None):
    """Last 5 complete calendar years (CY frames of the current year are still filling in)."""
    y = (today or dt.date.today()).year - 1
    return [str(x) for x in range(y - VALUE_RULES["n_fy"] + 1, y + 1)]


def frame(tag, period, unit="USD"):
    """{cik: val} for one frame, cached on disk (30 days for closed periods)."""
    os.makedirs(FDIR, exist_ok=True)
    path = os.path.join(FDIR, f"{tag}_{unit}_{period}.json")
    if os.path.exists(path):
        age = (dt.datetime.now().timestamp() - os.path.getmtime(path)) / 86400
        if age < 30:
            with open(path, encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
    d = sec(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json")
    out = {r["cik"]: [r["val"], r.get("entityName")] for r in (d or {}).get("data", [])}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def merged(tags, period):
    out = {}
    for t in tags:
        for cik, (v, nm) in frame(t, period).items():
            out.setdefault(cik, (v, nm))
    return out


def fetch_frames(years):
    """{cik: {"name", year: {rev, ni, ocf, cx, div, bb, eq, eq_prev?, gw?, cash?, debt?}}} plus request count."""
    items, n = {}, 0
    for y in years:
        for k, tags in DUR.items():
            for cik, (v, nm) in merged(tags, f"CY{y}").items():
                it = items.setdefault(cik, {"name": nm})
                it.setdefault(y, {})[k] = v
            n += len(tags)
    eq_years = [str(int(years[0]) - 1)] + years
    for y in eq_years:
        for q in ("Q4I", "Q3I"):            # year-end equity; Q3 when no instant is assigned to Q4
            for cik, (v, _) in merged(INST["eq"], f"CY{y}{q}").items():
                if cik in items:
                    items[cik].setdefault("eq", {}).setdefault(y, v)
            n += len(INST["eq"])
    last = years[-1]
    for k in ("gw", "cash", "ltd", "ltdn", "ltdc", "stb", "cp"):
        for cik, (v, _) in merged(INST[k], f"CY{last}Q4I").items():
            if cik in items:
                items[cik].setdefault("bs", {})[k] = v
        n += 1
    return items, n


def _r(x, d=2):
    return None if x is None else round(x, d)


def metrics(it, years):
    """Multi-year quality metrics of one filer (fractions as %, money in USD)."""
    ys = [it.get(y) or {} for y in years]
    eq = it.get("eq") or {}
    roe = []
    for y, d in zip(years, ys):
        e1, e0 = eq.get(y), eq.get(str(int(y) - 1))
        # year-end equity <= 0: no ROE that year; a non-positive opening equity is not averaged in
        base = None if e1 is None or e1 <= 0 else (e1 + e0) / 2 if e0 is not None and e0 > 0 else e1
        roe.append(100 * d["ni"] / base if d.get("ni") is not None and base and base > 0 else None)
    rv = [x for x in roe if x is not None]
    no_cx = all(d.get("cx") is None for d in ys)
    fcf = [None if d.get("ocf") is None or (d.get("cx") is None and not no_cx) else d["ocf"] - (d.get("cx") or 0) for d in ys]
    fv = [x for x in fcf if x is not None]
    pair = [(d["ocf"], d["ni"]) for d in ys if d.get("ocf") is not None and d.get("ni") is not None]
    sn = sum(b for _, b in pair)
    r0, ra = ys[-1].get("rev"), ys[0].get("rev")
    cagr = ((r0 / ra) ** (1 / (len(years) - 1)) - 1) * 100 if r0 and ra and r0 > 0 and ra > 0 else None
    bs = it.get("bs") or {}
    ltd = bs.get("ltd") if bs.get("ltd") is not None else (bs.get("ltdn") or 0) + (bs.get("ltdc") or 0)
    debt = (ltd or 0) + (bs.get("stb") or 0) + (bs.get("cp") or 0)
    e_last = eq.get(years[-1])
    mg = [(d["ocf"] - (d.get("cx") or 0)) / d["rev"] for d in ys[-3:]
          if d.get("ocf") is not None and (d.get("cx") is not None or no_cx) and (d.get("rev") or 0) > 0]
    fa = statistics.mean(fv) if fv else None
    net_cash = bs["cash"] - debt if bs.get("cash") is not None else None
    nd = -net_cash if net_cash is not None else debt
    cap = (e_last or 0) + debt
    return {"roe": roe, "n_roe": len(rv), "roe_med": statistics.median(rv) if rv else None, "roe_min": min(rv) if rv else None,
            "fcf": fcf, "fcf_pos": sum(1 for x in fv if x > 0), "n_fcf": len(fv), "fcf_avg": fa,
            "conv": sum(a for a, _ in pair) / sn if sn > 0 else None, "cagr": cagr, "r0": r0, "ni0": ys[-1].get("ni"),
            "de": debt / e_last if e_last and e_last > 0 else None,
            "nd_fcf": (nd / fa if nd > 0 else 0.0) if fa and fa > 0 else None,
            "gw_pct": 100 * (bs.get("gw") or 0) / cap if cap > 0 else None,
            "net_cash": net_cash, "no_cx": no_cx,
            "m3": statistics.mean(mg) if mg else None, "div": ys[-1].get("div") or 0.0, "bb": ys[-1].get("bb") or 0.0}


def fails(m):
    R, w = VALUE_RULES, []
    if not m["r0"] or m["r0"] < R["rev_min"]:
        w.append("small")
    if m["ni0"] is None or m["ni0"] <= 0:
        w.append("np")
    if m["n_roe"] < R["roe_years_min"]:
        w.append("roe_n")
    else:
        if m["roe_med"] < R["roe_median_min"]:
            w.append("roe_med")
        if m["roe_min"] < R["roe_min_min"]:
            w.append("roe_min")
    if m["n_fcf"] < R["fcf_pos_min"] or m["fcf_pos"] < R["fcf_pos_min"]:
        w.append("fcf")
    if m["conv"] is None or m["conv"] < R["conv_min"]:
        w.append("conv")
    if m["cagr"] is None or m["cagr"] < 0:
        w.append("shrink")
    if m["nd_fcf"] is None or m["nd_fcf"] > R["nd_fcf_max"]:
        w.append("lev")
    return w


def _cyclical_sics():
    from .classify import sectors
    return {sic for s in sectors() for g in s["segments"] if g.get("cyclical") for sic in g.get("sic", [])}


def run():
    from . import us, yh as Y
    from .heat import CYCLICAL_MAJOR, FINANCIAL_MAJOR
    from .net import Yahoo
    from .valuation import latest as val_latest
    years = years_for()
    t0 = dt.datetime.now()
    log(f"quality: SEC frames CY{years[0]}..CY{years[-1]} (all filers)")
    items, nreq = fetch_frames(years)
    log(f"  frames: {nreq} frame queries, filers with data: {len(items)}")
    passed, why = {}, {}
    for cik, it in items.items():
        m = metrics(it, years)
        w = fails(m)
        if w:
            why[cik] = w
        else:
            passed[cik] = m
    log(f"  fundamental rules: pass {len(passed)} / {len(items)}; submissions for the passers ...")
    cyc_sic = _cyclical_sics()
    rows, listed = [], {}
    for cik, m in passed.items():
        sub = us.submissions(cik)
        ex = [e for e in sub.get("exchanges", []) if e in CFG["screen"]["exchanges"]]
        sic = sub.get("sic")
        if not (ex and sub.get("tickers")):
            why[cik] = ["exchange"]
            continue
        tk = sub["tickers"][sub["exchanges"].index(ex[0])]
        if sic and f"{sic:04d}"[:2] in FINANCIAL_MAJOR:
            why[cik] = ["financial"]
            listed[cik] = (tk, sic, sub.get("sic_desc"))
            continue
        listed[cik] = (tk, sic, sub.get("sic_desc"))
        rows.append({"cik": cik, "ticker": tk, "name": items[cik].get("name"), "sic": sic, "sic_desc": sub.get("sic_desc"),
                     "exchange": ex[0], "cyclical": bool(sic and (f"{sic:04d}"[:2] in CYCLICAL_MAJOR or sic in cyc_sic)), "_m": m})
    log(f"  listed, non-financial passers: {len(rows)}; Yahoo market data ...")
    yh = Yahoo()
    fx = {"USD": 1.0}
    V = val_latest() or {}
    rf = (V.get("rf") or {}).get("value")
    from .valuation import dcf_reverse
    n_mk = 0
    for i, r in enumerate(rows):
        mk = Y.market(yh, r["ticker"], fx) or {}
        m = r.pop("_m")
        mc, px = mk.get("mcap"), mk.get("price")
        n_mk += 1 if mc else 0
        ig = igs = None
        if rf is not None and mc and px and m["m3"] and m["m3"] > 0 and m["net_cash"] is not None and m["r0"]:
            rr, gT = (rf + VALUE_RULES["erp"]) / 100, min(VALUE_RULES["gT"], rf) / 100
            o = dcf_reverse({"R0": m["r0"], "m": m["m3"], "g0": 0.0, "gT": gT, "r": rr, "netCash": m["net_cash"], "shares": mc / px}, px)
            ig, igs = (o["x"] * 100, "ok") if o["ok"] else (None, o["side"])
        r.update({"price": px, "mcap": mc, "roe": [_r(x, 1) for x in m["roe"]], "roe_med": _r(m["roe_med"], 1), "roe_min": _r(m["roe_min"], 1),
                  "conv": _r(m["conv"]), "fcf_pos": m["fcf_pos"], "n_fcf": m["n_fcf"], "no_cx": m["no_cx"], "cagr": _r(m["cagr"], 1),
                  "de": _r(m["de"]), "nd_fcf": _r(m["nd_fcf"], 1), "gw_pct": _r(m["gw_pct"], 1),
                  "gw_warn": bool(m["gw_pct"] is not None and m["gw_pct"] > VALUE_RULES["goodwill_warn_pct"]), "rev": m["r0"], "ni": m["ni0"],
                  "fcfy": _r(100 * m["fcf_avg"] / mc) if mc and m["fcf_avg"] is not None else None,
                  "ey": _r(100 * m["ni0"] / mc) if mc else None, "dy": _r(100 * m["div"] / mc) if mc else None,
                  "shy": _r(100 * (m["div"] + m["bb"]) / mc) if mc else None,
                  "ig": _r(ig, 1), "igs": igs, "ig_gap": _r(ig - m["cagr"], 1) if ig is not None and m["cagr"] is not None else None,
                  "analysts": mk.get("analysts"), "target": mk.get("target")})
        if (i + 1) % 100 == 0:
            log(f"  market {i + 1}/{len(rows)}")
    # tickers of every filer with data (for the lookup's "not in the channel" reasons): only already-cached submissions
    tick = {}
    for cik in items:
        p = os.path.join(CACHE, f"sub_{cik}.json")
        if cik in listed:
            tick[listed[cik][0]] = cik
        elif os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    s = json.load(f)
                if s.get("tickers"):
                    tick[s["tickers"][0]] = cik
            except (OSError, ValueError):
                pass
    doc = {"version": 1, "fetched_at": t0.isoformat(timespec="seconds"), "years": years, "rules": VALUE_RULES, "rf": rf,
           "n_filers": len(items), "rows": rows, "why": {str(k): v for k, v in why.items()}, "ticker_cik": tick,
           "n_market": n_mk}
    path = os.path.join(DATA, f"quality_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    log(f"saved {os.path.relpath(path, DATA)}: filers={len(items)} passers={len(rows)} with_mcap={n_mk} rf={rf}")
    return path


def latest():
    fs = sorted(glob.glob(os.path.join(DATA, "quality_*.json")))
    if not fs:
        return None, None
    try:
        with open(fs[-1], encoding="utf-8") as f:
            return json.load(f), fs[-1]
    except (OSError, ValueError):
        return None, fs[-1]


LOOKUP_COLS = ["ticker", "name", "sic", "sic_desc", "rev_ttm", "yoy", "gm", "opm", "why", "seg"]


def lookup(entries, heat):
    """全市场速查 rows from the heat snapshot (every SEC filer with comparable quarterly revenue and a ticker): the reasons a
    filer is not in the growth screen, re-derived with the same classify rules (memberships / thresholds / passes).
    Reason keys: screen (in the screen), no_seg (SIC / name matches no configured segment), excluded (sector exclude rule),
    size (TTM revenue outside the segment range), prior_q (year-ago quarter below the minimum), growth (YoY below the
    minimum or above the sanity cap), other (passes the rules but not listed: exchange, market-cap cap or missing market data)."""
    from .classify import _excluded, _segment, is_us, passes, profile, sectors, thresholds, view
    secs = sectors()
    have = {}
    for e in entries:
        have.setdefault(e["ticker"], e)
    rows = []
    for h in (heat or {}).get("rows", []):
        t = h.get("ticker")
        if not t or not is_us(t):
            continue
        r = {**h, "ticker": t}
        seg_txt, why = "", []
        if t in have:
            why = ["screen"]
            e = have[t]
            seg_txt = f"{e['sector']}.{e['segment']}"
        else:
            hits = []
            for s in secs:
                g, _ = _segment(s, t, h.get("name"), h.get("sic"))
                if g:
                    hits.append((s, g))
            if not hits:
                why = ["no_seg"]
            else:
                seg_txt = ",".join(f"{s['id']}.{g['id']}" for s, g in hits)
                best = None
                for s, g in hits:
                    if _excluded(s, t, h.get("name")):
                        w = ["excluded"]
                    else:
                        T, v = thresholds(s, g), view(r, profile(g))
                        if passes(v, T):
                            w = ["other"]
                        else:
                            w = []
                            if not v.get("rev_ttm") or not T["ttm_revenue_min"] <= v["rev_ttm"] <= T["ttm_revenue_max"]:
                                w.append("size")
                            if (v.get("rev_q_ly") or 0) < T["prior_quarter_revenue_min"]:
                                w.append("prior_q")
                            if v.get("yoy") is None or not T["yoy_growth_min"] <= v["yoy"] <= T["yoy_growth_max_sanity"]:
                                w.append("growth")
                    if best is None or len(w) < len(best):
                        best = w                     # the segment where the company comes closest
                why = best or ["other"]
        rows.append([t, h.get("name"), h.get("sic"), h.get("sic_desc"), _r(h["rev_ttm"] / 1e6, 1) if h.get("rev_ttm") else None,
                     h.get("yoy"), h.get("gm"), h.get("opm"), ",".join(why), seg_txt])
    seen = {r[0] for r in rows}
    for t, e in have.items():           # entries without a heat row (e.g. financial-tag revenue only, leaders)
        if t not in seen and is_us(t):
            rows.append([t, e.get("name") or t, None, None, None, None, None, None, "screen",
                         f"{e['sector']}.{e['segment']}"])
    cnt = {}
    for r in rows:
        for k in r[8].split(","):
            cnt[k] = cnt.get(k, 0) + 1
    return {"cols": LOOKUP_COLS, "rows": rows, "counts": cnt, "unit": "rev_ttm in USD millions",
            "periods": (heat or {}).get("periods"), "fetched_at": (heat or {}).get("fetched_at")}


def channel(entries, doc, path=None):
    """Page payload: rows (passers with mcap) ranked inside the channel; why-codes per ticker for the lookup."""
    from .classify import is_us
    if not doc:
        return {"file": None}
    ent = {}
    for e in entries:
        ent.setdefault(e["ticker"], e)
    from .heat import CYCLICAL_MAJOR
    rows = [dict(r) for r in doc["rows"] if r.get("mcap") and is_us(r["ticker"])]
    for r in rows:
        e = ent.get(r["ticker"])
        r["in_screen"] = bool(e)
        # cyclical: commodity / freight SIC major groups, or the company's own segment in the sector screens is cyclical
        # (a whole SIC code is not used: e.g. 3674 holds both memory makers and fabless designers)
        r["cyclical"] = bool(r.get("sic") and f"{r['sic']:04d}"[:2] in CYCLICAL_MAJOR) or bool(e and e.get("cyclical"))
        if e:
            r["sector"], r["kind"], r["segment"] = e["sector"], e["kind"], e["segment"]
        r.pop("cik", None)
    rk = [r for r in rows if not r["cyclical"]]
    keys = ("roe_med", "roe_min", "conv", "fcfy", "ey", "shy")

    def pct(vals):
        items = sorted((v, k) for k, v in vals.items() if v is not None)
        n, out, i = len(items), {}, 0
        while i < n:
            j = i
            while j + 1 < n and items[j + 1][0] == items[i][0]:
                j += 1
            for t in range(i, j + 1):
                out[items[t][1]] = round(100.0 * ((i + j) / 2) / (n - 1), 1) if n > 1 else 50.0
            i = j + 1
        return out
    pc = {k: pct({r["ticker"]: r[k] for r in rk}) for k in keys}
    for r in rk:
        f = {k: pc[k].get(r["ticker"]) for k in keys}
        av = [v for v in f.values() if v is not None]
        r["f"], r["score"] = f, round(statistics.mean(av), 1) if av else None
    rk.sort(key=lambda r: -(r["score"] or 0))
    for i, r in enumerate(rk, 1):
        r["rank"] = i
    rows.sort(key=lambda r: (r["cyclical"], r.get("rank") or 9999, -(r["roe_med"] or 0)))
    why_t = {}
    tmap = dict(doc.get("ticker_cik", {}))
    try:   # SEC company_tickers.json (cached by research.ticker_map): ticker -> CIK for every listed filer
        with open(os.path.join(CACHE, "company_tickers.json"), encoding="utf-8") as f:
            for x in json.load(f).values():
                tmap.setdefault(x["ticker"], x["cik_str"])
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    for t, cik in tmap.items():
        w = doc["why"].get(str(cik))
        if w is not None:
            why_t[t] = ",".join(w)
    for r in rows:
        why_t[r["ticker"]] = ""
    cnt = {}
    for w in doc["why"].values():
        for k in w:
            cnt[k] = cnt.get(k, 0) + 1
    return {"file": os.path.basename(path) if path else None, "fetched_at": doc.get("fetched_at"), "years": doc["years"],
            "rules": doc["rules"], "rf": doc.get("rf"), "rows": rows, "n_pass": len(rows), "n_ranked": len(rk),
            "n_filers": doc.get("n_filers"), "counts": cnt, "why": why_t}
