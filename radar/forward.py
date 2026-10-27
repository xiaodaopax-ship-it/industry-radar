"""Forward tracking of the valuation-ledger tags (前向跟踪): what the price did AFTER a ledger record carried a tag.

    python -m radar forward     US ledger -> data/ledger/us_forward.json (all samples + aggregates), print a summary

Display / research only. Nothing here feeds the score, factors, level, PEG tiers, labels or the comparison-basket score, and the
result is not used to change any tag threshold automatically.

Method (identical for both markets; cn/forward.py reuses evaluate() with the A-share ledger):
  * A sample = one ledger record (company, start date d0, price p0, the tags of that record). Tags are recomputed from the
    record itself with the CURRENT thresholds (ledger.tags on the compact record); a record's tags only use that record and its
    stored change against the previous record, so no later information enters the start of a sample.
  * Forward price = the price of a LATER ledger record of the same company whose date is within the horizon window
    (target d0 + 7*h days, accepted from target - tol_lo to target + tol_hi; the closest one wins). No re-fetch, no backfill:
    valuations cannot be rebuilt for past dates without using today's financials (look-ahead), so history starts with the first
    ledger run and every horizon fills in as the weekly runs accumulate.
  * Return = p1 / p0 - 1, price only (dividends not included; prices are not split / 送转 adjusted). A sample is excluded when the
    share count moved by more than the split guard between the two records (likely split / 送转 / large issuance) or a price is
    missing / non-positive / in a different currency. Excluded samples are counted by reason, never silently dropped.
  * Excess vs the same start date: return minus the median return of ALL valid samples with the same start date and horizon
    (equal-weight universe of this ledger, not an index); sector excess uses the same-sector median when the sector has at least
    `min_peers` valid samples on that date, else no sector excess for that sample.
  * Aggregates per (tag, horizon) plus the baseline "全部样本": n samples, distinct companies, distinct start dates, median return,
    median excess, median sector excess, share of samples with excess > 0, 25th / 75th percentile of excess.
    `few` = fewer than min_starts start dates or min_companies companies: shown, but marked 样本不足.
  * Samples of consecutive weekly start dates overlap for long horizons and the same company appears many times, so they are
    NOT independent; no significance test is done. Many tags x horizons are compared at once, so some differences will look
    large by chance. This describes what happened, it does not show that a rule works.
"""
import datetime as dt
import statistics

RULES = {"horizons": [1, 4, 13, 26, 52], "tol_lo_days": 2, "tol_hi_frac": 0.25, "tol_hi_min_days": 5,
         "split_guard": 1.25, "min_peers": 5, "min_starts": 4, "min_companies": 20}
NOTE = ("前向跟踪只描述打标签之后股价实际怎么走（不含分红、价格未复权），样本彼此重叠、不独立，未做显著性检验；"
        "同时比较很多标签和期限，部分差异会是偶然。不是买卖信号，也不说明规则有效。")


def _d(s):
    return dt.date.fromisoformat(str(s)[:10])


def _q(xs, p):
    """Percentile by linear interpolation (xs sorted)."""
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _r(v, d=5):
    return None if v is None else round(v, d)


def window(h, R=RULES):
    """(min, max) accepted days after d0 for horizon h weeks."""
    t = 7 * h
    return t - R["tol_lo_days"], t + max(R["tol_hi_min_days"], round(t * R["tol_hi_frac"]))


def evaluate(series, R=None, bench=None):
    """series: {id: [point, ...]} with point = {"d": "YYYY-MM-DD", "p": price, "sh": shares or None, "ccy": str or None,
    "grp": sector, "tags": [text, ...], "run": run id}; points of one id in time order (one per start date: callers dedupe).
    bench: optional fn(d0, d1) -> benchmark return over the same dates (e.g. SPY) or None.
    Returns {"rules", "as_of", "first", "samples", "agg", "excluded", "pending", "bench"}."""
    R = {**RULES, **(R or {})}
    H = R["horizons"]
    pts = {k: sorted(v, key=lambda x: x["d"]) for k, v in series.items() if v}
    all_d = [p["d"] for v in pts.values() for p in v]
    if not all_d:
        return {"rules": R, "as_of": None, "first": None, "samples": [], "agg": [], "excluded": {}, "pending": {}, "bench": {}, "note": NOTE}
    as_of, first = max(all_d), min(all_d)
    samples, excl = [], {}
    for k, v in pts.items():
        for i, a in enumerate(v):
            d0 = _d(a["d"])
            for h in H:
                lo, hi = window(h, R)
                best = None
                for b in v[i + 1:]:
                    n = (_d(b["d"]) - d0).days
                    if n > hi:
                        break
                    if n >= lo and (best is None or abs(n - 7 * h) < abs(best[0] - 7 * h)):
                        best = (n, b)
                if best is None:
                    continue                       # not yet observable (or a gap in the weekly runs)
                n, b = best
                why = None
                if not a.get("p") or not b.get("p") or a["p"] <= 0 or b["p"] <= 0:
                    why = "缺价格"
                elif a.get("ccy") and b.get("ccy") and a["ccy"] != b["ccy"]:
                    why = "币种不同"
                elif a.get("sh") and b.get("sh") and not (1 / R["split_guard"] <= b["sh"] / a["sh"] <= R["split_guard"]):
                    why = "股本变化过大（疑似拆股/送转/增发，价格未复权）"
                if why:
                    excl.setdefault(str(h), {}).setdefault(why, 0)
                    excl[str(h)][why] += 1
                    continue
                samples.append({"id": k, "h": h, "d0": a["d"], "d1": b["d"], "days": n, "grp": a.get("grp"), "tags": list(a.get("tags") or []),
                                "ret": b["p"] / a["p"] - 1, "bm": bench(a["d"], b["d"]) if bench else None})
    # excess vs the same start date (universe and sector medians)
    by_start, by_sec = {}, {}
    for s in samples:
        by_start.setdefault((s["h"], s["d0"]), []).append(s["ret"])
        by_sec.setdefault((s["h"], s["d0"], s["grp"]), []).append(s["ret"])
    for s in samples:
        s["ex"] = s["ret"] - statistics.median(by_start[(s["h"], s["d0"])])
        peers = by_sec[(s["h"], s["d0"], s["grp"])]
        s["sex"] = s["ret"] - statistics.median(peers) if len(peers) >= R["min_peers"] else None
    # aggregates
    groups = {}
    for s in samples:
        for t in ["全部样本"] + s["tags"]:
            groups.setdefault((t, s["h"]), []).append(s)
    agg = []
    for (t, h), ss in groups.items():
        ex = sorted(s["ex"] for s in ss)
        sx = [s["sex"] for s in ss if s["sex"] is not None]
        nu, nr = len({s["id"] for s in ss}), len({s["d0"] for s in ss})
        bm = [s["bm"] for s in ss if s["bm"] is not None]
        agg.append({"tag": t, "h": h, "n": len(ss), "nu": nu, "nr": nr, "days": statistics.median(s["days"] for s in ss),
                    "med": _r(statistics.median(s["ret"] for s in ss)), "mex": _r(statistics.median(ex)),
                    "msx": _r(statistics.median(sx)) if sx else None, "nsx": len(sx),
                    "up": _r(sum(1 for x in ex if x > 0) / len(ex), 4), "p25": _r(_q(ex, 0.25)), "p75": _r(_q(ex, 0.75)),
                    "bm": _r(statistics.median(bm)) if bm else None,
                    "few": nr < R["min_starts"] or nu < R["min_companies"]})
    agg.sort(key=lambda x: (x["h"], x["tag"] != "全部样本", -x["n"]))
    # when each horizon gets its first sample (earliest start + window low end), for the page's "pending" text
    pending = {str(h): (_d(first) + dt.timedelta(days=window(h, R)[0])).isoformat() for h in H
               if not any(s["h"] == h for s in samples)}
    return {"rules": R, "as_of": as_of, "first": first, "n_starts": len(set(all_d)), "samples": samples, "agg": agg,
            "excluded": excl, "pending": pending, "note": NOTE}


def page(res):
    """What the dashboard embeds: aggregates only (samples stay in the data file)."""
    if not res:
        return None
    return {k: res.get(k) for k in ("rules", "as_of", "first", "n_starts", "agg", "excluded", "pending", "note")} | {
        "n_samples": len(res.get("samples") or [])}


# ---------------------------------------------------------------- US adapter
def us_series(rows):
    """US ledger rows -> evaluate() series. Start date = px_date (the price date). Two runs on the same price date: the later wins."""
    from . import ledger as L
    by = {}
    for r in rows:
        d = r.get("px_date") or r.get("date")
        if not d:
            continue
        by.setdefault(r["ticker"], {})[d] = {"d": d, "p": r.get("price"), "sh": r.get("shares"), "ccy": r.get("ccy"), "grp": r.get("sector"),
                                              "tags": L.tags(L._compact(r)), "run": r["val_file"] + "|" + r["raw_file"]}
    return {t: list(v.values()) for t, v in by.items()}


def us_bench(raw=None):
    """SPY return between two dates from the (latest) raw file's 1y daily closes (None outside its window)."""
    from .paths import CFG
    if raw is None:
        from .build import latest_raw_file
        try:
            _, raw = latest_raw_file()
        except Exception:  # noqa: BLE001 - no raw file: no benchmark column
            return None, None
    sym = (CFG.get("market_benchmark") or {}).get("symbol", "SPY")
    px = (raw.get("benchmarks") or {}).get(sym) or []
    if len(px) < 2:
        return None, sym
    px = sorted((str(d)[:10], c) for d, c in px if c)

    def at(d):
        """Close on the last trading day on or before d (None if d is before the series)."""
        best = None
        for x, c in px:
            if x > d:
                break
            best = c
        return best

    def fn(d0, d1):
        if d1 > px[-1][0]:
            return None
        a, b = at(d0), at(d1)
        return b / a - 1 if a and b else None
    return fn, sym


def us_run(path=None, write=True, raw=None):
    import json
    import os
    from . import ledger as L
    from .paths import CFG
    rows = L.read(path)
    fn, sym = us_bench(raw)
    res = evaluate(us_series(rows), (CFG.get("valuation", {}).get("ledger", {}).get("forward") or {}), bench=fn)
    res["bench_symbol"] = sym
    if write:
        out = os.path.join(L.LDIR, "us_forward.json")
        os.makedirs(L.LDIR, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({**res, "computed_at": dt.datetime.now().isoformat(timespec="seconds"), "ledger": os.path.basename(path or L.LEDGER)},
                      f, ensure_ascii=False)
    return res


def embed_us(path=None, raw=None):
    res = us_run(path, write=False, raw=raw)
    p = page(res)
    if p is not None:
        p["bench_symbol"] = res.get("bench_symbol")
    return p


def summary(res):
    """English one-paragraph summary for stdout."""
    base = {a["h"]: a for a in res["agg"] if a["tag"] == "全部样本"}
    hs = ", ".join(f"{h}w n={base[h]['n']}" for h in sorted(base)) or "none yet"
    pend = ", ".join(f"{h}w from {d}" for h, d in res["pending"].items())
    ex = sum(sum(v.values()) for v in res["excluded"].values())
    return (f"forward: {res.get('n_starts', 0)} start dates ({res['first']} .. {res['as_of']}), samples={len(res['samples'])} "
            f"excluded={ex}; horizons with data: {hs}" + (f"; first samples expected: {pend}" if pend else ""))
