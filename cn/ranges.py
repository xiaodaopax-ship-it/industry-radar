"""52-week high / low on a forward-adjusted (前复权, qfq) basis for the ② screen rows (display only).

    python -m cn ranges [N]   latest snapshot -> data/cn/ranges_<ts>.json (N = only the first N codes in order)

Order: evaluation-card stocks, ④ research priority, valuation shortlist, then the rest of the ② screen by revenue
growth, so a partial run still covers the rows that matter most. Serial, cached (em.fetch_json, ttl 1 day), polite
(em rate limiter + an extra sleep per code), resumable (items of the latest ranges file with the same quote date and
window are reused; a checkpoint file is written every 100 codes).

Source per code:
  1. push2his daily kline fqt=1 (intraday high/low, qfq) - only when a probe at the start of the run succeeds
     (push2his dropped almost every connection during verification on 2026-10-22);
  2. otherwise Eastmoney datacenter: RPT_VALUEANALYSIS_DET daily CLOSE_PRICE (unadjusted) for the window, adjusted to
     qfq with the RPT_SHAREBONUS_DET ex-dates in the window: for every bar before an ex-date,
     P' = (P - cash per share) / (1 + 送转 per share). Closes only, so hi52 / lo52 are close-to-close extremes.
     Verified to match an independent qfq close series to <= 1e-5 relative for 600519 / 301396 / 688808 / 300475.
     配股 (rights issues) are not in RPT_SHAREBONUS_DET and are not adjusted.
Each item records its basis ('qfq_hl' or 'qfq_close'). Nothing here is used in any score, label or rank.
stdout is English only.
"""
import datetime as dt
import glob
import json
import os
import time
import urllib.parse

from . import em

ROOT = em.ROOT
DATA_DIR = os.path.join(ROOT, "data", "cn")
RANGE_RULES = {
    "weeks": 52,               # window = (quote date - 52 weeks, quote date]
    "pre_days": 30,            # extra days fetched before the window to tell "listed recently" from a long suspension
    "short_gap_days": 14,      # first bar > window start + 14 days and nothing before => listed < 52 weeks
    "stale_days": 3,           # last bar older than the quote date by > 3 calendar days => suspended / stale
    "mismatch_pct": 0.5,       # last qfq close vs snapshot price differ by > 0.5% => basis flag
    "sleep": 0.2,              # extra pause per code on top of em's 2 req/s limiter
    "max_consecutive_fail": 8,  # stop (and save what we have) after this many codes fail in a row
    "ttl": 1.0,                # cache lifetime, days
    "checkpoint_every": 100,
}


def log(msg):
    print(msg, flush=True)


def latest_ranges():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "ranges_*.json")))
    return files[-1] if files else None


def load_latest():
    p = latest_ranges()
    if not p:
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), p
    except (OSError, ValueError) as e:
        log(f"WARNING: {os.path.basename(p)} unreadable ({type(e).__name__}); ranges ignored")
        return None, p


def range_codes(res):
    """Card stocks, priority, shortlist, then the remaining screen rows by revenue growth (deduplicated)."""
    from .cards import card_codes
    out = card_codes(res) + [p["code"] for p in res["priority"]] + list(res["shortlist"])
    out += [r["code"] for r in sorted(res["screen"], key=lambda r: -(r["rev_yoy"] or 0))]
    return list(dict.fromkeys(out))


def _vh_query(code, beg, end):
    return {"reportName": "RPT_VALUEANALYSIS_DET", "columns": "TRADE_DATE,CLOSE_PRICE", "pageSize": em.DC_MAX_PAGE,
            "pageNumber": 1, "source": "WEB", "client": "WEB",
            "filter": f"(SECURITY_CODE=\"{code}\")(TRADE_DATE>='{beg}')(TRADE_DATE<='{end}')",
            "sortColumns": "TRADE_DATE", "sortTypes": "1"}


def derived_qfq_closes(code, beg, end, events, ttl):
    """(bars, ok, src_url): unadjusted daily closes from RPT_VALUEANALYSIS_DET adjusted to qfq with `events`
    (ex-date <= end). bars = [{date, close, high, low}] with high = low = close."""
    q = _vh_query(code, beg, end)
    url = em.DC_URL + "?" + urllib.parse.urlencode(q)
    rows, ok = em.dc_report(q["reportName"], q["columns"], q["filter"], q["sortColumns"], q["sortTypes"],
                            ttl=ttl, label=f"ranges {code}", warn_truncated=False)
    ev = [(e["ex_date"], (e.get("cash_per10") or 0) / 10, (e.get("bonus_it_per10") or 0) / 10)
          for e in events if e.get("ex_date") and e["ex_date"] <= end]
    bars = []
    for r in rows:
        d, p = em.day(r.get("TRADE_DATE")), em.num(r.get("CLOSE_PRICE"))
        if not d or p is None:
            continue
        for x, cash, bonus in ev:
            if d < x:
                p = (p - cash) / (1 + bonus)
        bars.append({"date": d, "close": p, "high": p, "low": p})
    return bars, ok, url


def bonus_events(beg, end, ttl):
    """Market-wide implemented dividend / 送转 events with an ex-date in [beg, end], grouped by code."""
    rows, ok = em.dc_report("RPT_SHAREBONUS_DET", "SECURITY_CODE,EX_DIVIDEND_DATE,BONUS_IT_RATIO,PRETAX_BONUS_RMB",
                            f"(EX_DIVIDEND_DATE>='{beg}')(EX_DIVIDEND_DATE<='{end}')", "EX_DIVIDEND_DATE,SECURITY_CODE", "1,1",
                            ttl=ttl, label="ranges/bonus")
    out = {}
    for r in rows:
        c = r.get("SECURITY_CODE")
        if em.is_a_code(c) and em.day(r.get("EX_DIVIDEND_DATE")):
            out.setdefault(c, []).append({"ex_date": em.day(r.get("EX_DIVIDEND_DATE")),
                                          "bonus_it_per10": em.num(r.get("BONUS_IT_RATIO")),
                                          "cash_per10": em.num(r.get("PRETAX_BONUS_RMB"))})
    return out, ok


def summarize(bars, start, as_of, basis, src_url, price=None):
    """hi52 / lo52 item from bars (oldest first) covering [start - pre_days, as_of]. Pure function (tested in smoke)."""
    R = RANGE_RULES
    pre = [b for b in bars if b["date"] < start]
    win = [b for b in bars if start <= b["date"] <= as_of and b.get("high") is not None and b.get("low") is not None]
    it = {"hi52": None, "lo52": None, "hi_date": None, "lo_date": None, "n_days": len(win), "first_date": None,
          "last_date": None, "as_of": as_of, "window_start": start, "basis": basis, "last_close": None,
          "short_history": False, "stale": False, "basis_mismatch": False, "src_url": src_url}
    if not win:
        return it
    hb = max(win, key=lambda b: (b["high"], b["date"]))
    lb = min(win, key=lambda b: (b["low"], b["date"]))
    it.update({"hi52": round(hb["high"], 4), "lo52": round(lb["low"], 4), "hi_date": hb["date"], "lo_date": lb["date"],
               "first_date": win[0]["date"], "last_date": win[-1]["date"],
               "last_close": None if win[-1].get("close") is None else round(win[-1]["close"], 4)})
    d0, d1, a = (dt.date.fromisoformat(x) for x in (start, win[0]["date"], as_of))
    it["short_history"] = not pre and (d1 - d0).days > R["short_gap_days"]
    it["stale"] = (a - dt.date.fromisoformat(win[-1]["date"])).days > R["stale_days"]
    if price and it["last_close"] and not it["stale"]:
        it["basis_mismatch"] = abs(it["last_close"] / price - 1) * 100 > R["mismatch_pct"]
    return it


def fetch_ranges(limit=None, snap_path=None):
    from .build import analyze
    from .cards import latest_snap
    R = RANGE_RULES
    snap_path = snap_path or latest_snap()
    if not snap_path:
        log("ERROR: no data/cn/snap_*.json found; run `python -m cn fetch` first")
        return None
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    res = analyze(snap)
    Q = {q["code"]: q for q in snap["quotes"]}
    as_of = next((q.get("as_of") for q in snap["quotes"] if q.get("as_of")), None) or snap["today"]
    as_of = as_of[:10]
    start = (dt.date.fromisoformat(as_of) - dt.timedelta(weeks=R["weeks"]) + dt.timedelta(days=1)).isoformat()
    beg = (dt.date.fromisoformat(start) - dt.timedelta(days=R["pre_days"])).isoformat()
    codes = range_codes(res)
    if limit:
        codes = codes[:limit]
    t0 = dt.datetime.now(em.CST)
    path = os.path.join(DATA_DIR, f"ranges_{t0.strftime('%Y%m%d_%H%M%S')}.json")

    prev, prev_path = load_latest()
    reuse = {}
    if prev and prev.get("as_of") == as_of and prev.get("window_start") == start:
        reuse = {c: it for c, it in (prev.get("items") or {}).items() if it.get("hi52") is not None}
    log(f"ranges: {len(codes)} codes (cards+priority+shortlist first), window {start}..{as_of} (quote date), "
        f"reusable from {os.path.basename(prev_path) if reuse else 'none'}: {sum(1 for c in codes if c in reuse)}")

    events, ev_ok = bonus_events(beg, as_of, R["ttl"])
    log(f"  dividend/bonus-share events: {sum(len(v) for v in events.values())} rows, {len(events)} codes, ok={ev_ok}")
    if not ev_ok:
        log("ERROR: dividend / 送转 events incomplete; cannot build a qfq basis safely")
        return None

    # push2his probe (intraday high/low); it dropped nearly every connection during verification
    probe = [c for c in codes[:3]]
    use_kline = bool(probe) and all(em.daily_kline_qfq(c, beg, as_of, ttl=R["ttl"], quiet=True)[1] for c in probe)
    log(f"  push2his kline probe on {len(probe)} codes: {'ok -> intraday qfq high/low' if use_kline else 'failed -> datacenter closes adjusted to qfq'}")

    items, fails, consec, stopped, net_before = {}, [], 0, False, em.STATS["network"]

    def save(complete):
        n_ok = sum(1 for it in items.values() if it.get("hi52") is not None)
        bases = sorted({it["basis"] for it in items.values() if it.get("hi52") is not None})
        doc = {"fetched_at": t0.strftime("%Y-%m-%d %H:%M"), "basis": "qfq", "item_bases": bases, "as_of": as_of,
               "window_start": start, "snap": os.path.basename(snap_path), "rules": R, "complete": complete,
               "requested": len(codes), "ok": n_ok, "failed": fails, "stopped_early": stopped,
               "push2his_used": use_kline, "items": items, "http": dict(em.STATS)}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path)
        return n_ok

    for i, code in enumerate(codes, 1):
        if code in reuse:
            items[code] = reuse[code]
            continue
        price = (Q.get(code) or {}).get("price")
        it = None
        if use_kline:
            bars, ok = em.daily_kline_qfq(code, beg, as_of, ttl=R["ttl"], quiet=True)
            if ok and bars:
                it = summarize(bars, start, as_of, "qfq_hl", em.kline_url(code, beg, as_of), price)
        if it is None:
            bars, ok, url = derived_qfq_closes(code, beg, as_of, events.get(code, []), R["ttl"])
            it = summarize(bars, start, as_of, "qfq_close", url, price) if ok else None
        if it is None or it["hi52"] is None:
            fails.append(code)
            consec += 1
            items[code] = it or {"hi52": None, "lo52": None, "as_of": as_of, "basis": None}
            if consec >= R["max_consecutive_fail"]:
                stopped = True
                log(f"  stopping after {consec} consecutive failures at {i}/{len(codes)} (rate limit?)")
                break
        else:
            consec = 0
            items[code] = it
        if em.STATS["network"] > net_before:
            time.sleep(R["sleep"])
            net_before = em.STATS["network"]
        if i % R["checkpoint_every"] == 0:
            n = save(False)
            log(f"  ranges {i}/{len(codes)} ok={n} failed={len(fails)}")
    n = save(not stopped)
    flags = {k: sum(1 for it in items.values() if it.get(k)) for k in ("short_history", "stale", "basis_mismatch")}
    log(f"saved {os.path.relpath(path, ROOT)}: ok={n}/{len(codes)} failed={len(fails)} flags={flags} http={em.STATS}")
    return path


def attach(rows, doc, quote_date):
    """Add price-range display fields to ② screen rows in place (display only; called after all scoring)."""
    items = (doc or {}).get("items") or {}
    for r in rows:
        it = items.get(r["code"]) or {}
        p, hi, lo = r.get("price"), it.get("hi52"), it.get("lo52")
        ok = p is not None and hi is not None and lo is not None
        r["rng"] = None if not it.get("basis") else {
            "hi52": hi, "lo52": lo, "hi_date": it.get("hi_date"), "lo_date": it.get("lo_date"), "n_days": it.get("n_days"),
            "first_date": it.get("first_date"), "last_date": it.get("last_date"), "as_of": it.get("as_of"),
            "basis": it.get("basis"), "short": bool(it.get("short_history")), "stale": bool(it.get("stale")),
            "mismatch": bool(it.get("basis_mismatch")), "last_close": it.get("last_close"),
            "date_ok": it.get("as_of") == quote_date,
            "outside": bool(ok and not (lo - 1e-6 <= p <= hi + 1e-6)), "flat": bool(ok and hi <= lo)}
        r["pos52"] = round(100 * (p - lo) / (hi - lo), 1) if ok and hi > lo else None
        r["off_hi"] = round(100 * (p / hi - 1), 1) if ok and hi > 0 else None
        r["off_lo"] = round(100 * (p / lo - 1), 1) if ok and lo > 0 else None
