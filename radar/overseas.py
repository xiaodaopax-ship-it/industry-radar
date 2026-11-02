"""海外对照 (display only): a small fixed watch list outside the US screen, e.g. Korean memory makers and Korean
semiconductor ETFs, shown next to their US peers. Never part of the US screen, factors, levels, PEG, the ledger or the
basket score; config markets.us_only stays true.

    python -m radar overseas   Yahoo chart (1y daily) + quoteSummary (price, 52-week range, consensus EPS, next earnings)
                               for OVERSEAS -> data/overseas_<ts>.json, then rebuild

Prices are in the listing currency (KRW for .KS); forward P/E = price / consensus EPS in the same currency.
Yahoo closes are split-adjusted, not dividend-adjusted; ETF and index rows have no EPS.
"""
import datetime as dt
import glob
import json
import os

from .paths import DATA
from .px import weekly

# (symbol, display name, kind, US peer shown for reference)
OVERSEAS = [
    {"group": "韩国内存", "items": [
        ("005930.KS", "三星电子", "stock", "MU"),
        ("000660.KS", "SK 海力士", "stock", "MU"),
        ("091160.KS", "KODEX 半导体 ETF", "etf", "SOXX"),
        ("091230.KS", "TIGER 半导体 ETF", "etf", "SOXX"),
        ("EWY", "iShares MSCI 韩国 ETF（美股上市）", "etf", None),
        ("^KS11", "KOSPI 指数", "index", None),
        ("KRW=X", "美元 / 韩元", "fx", None),
    ]},
]
PEERS = ["MU", "SOXX"]   # US reference rows, also fetched so the table compares like windows


def log(msg):
    print(msg, flush=True)


def _stats(px):
    """Trailing returns from daily closes [[date, close], ...] (oldest first)."""
    if not px or len(px) < 30:
        return None
    last = px[-1][1]
    b = lambda n: px[-1 - n][1] if len(px) > n else None
    r = lambda x: round((last / x - 1) * 100, 1) if x else None
    hi = max(px, key=lambda p: p[1])
    lo = min(px, key=lambda p: p[1])
    return {"last": last, "date": px[-1][0], "r1w": r(b(5)), "r1m": r(b(21)), "r3m": r(b(63)), "r6m": r(b(126)), "r1y": r(px[0][1]),
            "hi": hi[1], "hi_date": hi[0], "lo": lo[1], "lo_date": lo[0], "off_hi": r(hi[1])}


def run():
    from .net import Yahoo
    from . import yh as Y
    yh = Yahoo()
    syms = [(s, n, k, p, g["group"]) for g in OVERSEAS for s, n, k, p in g["items"]] + [(s, s, "peer", None, "参照") for s in PEERS]
    items, fails = {}, []
    for s, name, kind, peer, grp in syms:
        c = yh.chart(s, "1y") or {}
        px = c.get("px") or []
        st = _stats(px)
        if not st:
            fails.append(s)
            continue
        m = (Y.market(yh, s, {"USD": 1.0}) or {}) if kind in ("stock", "peer") else {}
        eps = m.get("eps_est") or {}
        items[s] = {"name": name, "kind": kind, "peer": peer, "group": grp, "ccy": (c.get("meta") or {}).get("ccy") or m.get("currency"),
                    **st, "wk": weekly(px),
                    "hi52": m.get("hi52"), "lo52": m.get("lo52"), "target": m.get("target"), "analysts": m.get("analysts"),
                    "next_date": m.get("next_date"),
                    "eps0": (eps.get("0y") or {}).get("avg"), "eps0_end": (eps.get("0y") or {}).get("end_date"),
                    "eps1": (eps.get("+1y") or {}).get("avg"), "eps1_end": (eps.get("+1y") or {}).get("end_date"),
                    "eps_ly": (eps.get("0y") or {}).get("year_ago")}
    t0 = dt.datetime.now()
    path = os.path.join(DATA, f"overseas_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"fetched_at": t0.isoformat(timespec="seconds"), "groups": [g["group"] for g in OVERSEAS], "items": items, "failed": fails},
                  f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    log(f"saved {os.path.relpath(path, DATA)}: ok={len(items)} failed={fails}")
    return path


def latest():
    fs = sorted(glob.glob(os.path.join(DATA, "overseas_*.json")))
    if not fs:
        return None
    with open(fs[-1], encoding="utf-8") as f:
        return json.load(f)
