"""1-year weekly closing prices for the 交集 view (display only; never used by scores, levels, the ledger or the basket score).

    python -m radar px    Yahoo chart (range 1y, daily, 1-day cache) for every value-channel company that is also in a
                          sector screen (the 交集 candidates) -> data/px_<ts>.json, then rebuild

Leaders already carry a 1-year daily series in the raw snapshot (collect.collect_leaders); it is reused without a request.
Yahoo closes are split-adjusted, not dividend-adjusted. Output keeps the last close of every calendar week.
"""
import datetime as dt
import glob
import json
import os

from .paths import DATA


def log(msg):
    print(msg, flush=True)


def weekly(px):
    """[[date, close], ...] (oldest first) -> {"d": [...], "c": [...]} with the last close of each ISO week; None if empty."""
    wk = {}
    for d, c in px or []:
        if c is not None:
            y, w, _ = dt.date.fromisoformat(d).isocalendar()
            wk[(y, w)] = (d, c)
    if not wk:
        return None
    xs = [wk[k] for k in sorted(wk)]
    return {"d": [x[0] for x in xs], "c": [round(x[1], 4) for x in xs]}


def tickers(raw, qdoc):
    """交集 candidates: value-channel tickers that are a US candidate or a leader in the raw snapshot."""
    vc = {r["ticker"] for r in (qdoc or {}).get("rows") or [] if r.get("ticker")}
    scr = {c.get("ticker") for c in raw.get("candidates") or [] if c.get("ticker")} | set((raw.get("leaders") or {}).keys())
    return sorted(vc & scr)


def run():
    from .build import latest_raw
    from .net import Yahoo
    from . import quality as Q
    raw = latest_raw()
    qdoc, _ = Q.latest()
    ts = tickers(raw, qdoc)
    leaders = raw.get("leaders") or {}
    yh, items, n_net, fails = Yahoo(), {}, 0, []
    for t in ts:
        px = (leaders.get(t) or {}).get("px")
        src = "raw"
        if not px:
            c = yh.chart(t, "1y") or {}
            px, src, n_net = c.get("px"), "yahoo", n_net + 1
        w = weekly(px)
        if w:
            items[t] = {**w, "src": src}
        else:
            fails.append(t)
    t0 = dt.datetime.now()
    path = os.path.join(DATA, f"px_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"fetched_at": t0.isoformat(timespec="seconds"), "n": len(ts), "items": items, "failed": fails}, f)
    os.replace(path + ".tmp", path)
    log(f"saved {os.path.relpath(path, DATA)}: tickers={len(ts)} ok={len(items)} yahoo_requests={n_net} failed={fails[:10]}")
    return path


def latest():
    fs = sorted(glob.glob(os.path.join(DATA, "px_*.json")))
    if not fs:
        return None
    with open(fs[-1], encoding="utf-8") as f:
        return json.load(f)
