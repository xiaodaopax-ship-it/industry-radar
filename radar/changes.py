"""本周变化 (display only): which tickers entered / left each view since the previous data date.

At every build the members of each view are stored under the data date (raw snapshot fetch date) in
data/membership_us.json (last 26 dates kept; a rebuild of the same date replaces that date). The page shows the
difference between the latest date and the one before it. Never used by scores, levels, the ledger or the basket score.

Views (same rules as the page defaults):
  vc  价值通道            every value-channel row (standard + financial)
  vu  价值 × 低估          value channel, not cyclical, price implies growth >= 5pt below the 4-year revenue CAGR
                          (financial: implied ROE >= 3pt below the 5-year median ROE); does not require the growth screen
  x   交集                in a sector screen AND the value channel AND default-parameter base value >= +15% above the price
                          (latest valuation-ledger record), not cyclical
"""
import json
import os

from .paths import DATA

# 价值 × 低估 thresholds (also the page defaults, embedded as D.vu_rules)
VU_RULES = {"ig_gap_max": -5.0, "iroe_gap_max": -3.0, "x_gap_min": 0.15, "drop_yoy": -10.0}
LABELS = {"x": "交集（成长 × 价值 × 低估）", "vu": "价值 × 低估", "vc": "价值通道"}
PATH = os.path.join(DATA, "membership_us.json")
KEEP = 26


def is_vu(r, yoy=None):
    """Page defaults of the 价值 × 低估 view: not cyclical, latest-quarter revenue YoY > -10% (when known), and the gap."""
    if r.get("cyclical") or (yoy is not None and yoy <= VU_RULES["drop_yoy"]):
        return False
    if (r.get("group") or "std") == "fin":
        return r.get("iroe") is not None and r.get("roe_med") is not None and r["iroe"] - r["roe_med"] <= VU_RULES["iroe_gap_max"]
    if r.get("ig_gap") is None:
        return r.get("igs") == "lo"          # price implies a revenue decline below -30% a year
    return r["ig_gap"] <= VU_RULES["ig_gap_max"]


def sets(data):
    vc = (data.get("vc") or {}).get("rows") or []
    led = data.get("ledger") or {}
    by, lk = led.get("by") or {}, led.get("latest")
    ent = {}
    for e in data.get("entries") or []:
        ent.setdefault(e["ticker"], e)
    x = {}
    for r in vc:
        e, recs = ent.get(r["ticker"]), by.get(r["ticker"]) or []
        rec = recs[-1] if recs and recs[-1].get("k") == lk else None
        if e and rec and rec.get("gap") is not None and rec["gap"] >= VU_RULES["x_gap_min"] and not (r.get("cyclical") or e.get("cyclical")):
            x[r["ticker"]] = r.get("name") or e.get("name") or ""
    L = data.get("lookup") or {}
    ti, yi = (L.get("cols") or []).index("ticker") if "ticker" in (L.get("cols") or []) else None, \
        (L.get("cols") or []).index("yoy") if "yoy" in (L.get("cols") or []) else None
    yoy = {row[ti]: row[yi] for row in L.get("rows") or []} if ti is not None and yi is not None else {}
    return {"x": x, "vu": {r["ticker"]: r.get("name") or "" for r in vc if is_vu(r, yoy.get(r["ticker"]))}, "vc": {r["ticker"]: r.get("name") or "" for r in vc}}


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"keys": []}


def diff(doc, key, cur):
    prev = [k for k in doc.get("keys") or [] if k["k"] < key]
    p = prev[-1] if prev else None
    out = {"cur": key, "prev": p["k"] if p else None, "labels": LABELS, "views": {}}
    for name, members in cur.items():
        old = (p or {}).get("sets", {}).get(name)
        out["views"][name] = {"n": len(members), "n_prev": None if old is None else len(old),
                              "in": [] if old is None else sorted([t, members[t]] for t in members if t not in old),
                              "out": [] if old is None else sorted([t, old[t]] for t in old if t not in members)}
    return out


def update(data, key, write=True, path=PATH):
    """Store this data date's members and return the page payload (difference with the previous data date)."""
    cur = sets(data)
    doc = _load(path)
    keys = [k for k in doc.get("keys") or [] if k["k"] != key] + [{"k": key, "sets": cur}]
    doc["keys"] = sorted(keys, key=lambda k: k["k"])[-KEEP:]
    if write:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    return diff(doc, key, cur)
