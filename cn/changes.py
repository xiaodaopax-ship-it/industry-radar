"""本周变化 (display only): which codes entered / left each view since the previous quote date.

At every build the members of each view are stored under the snapshot quote date in data/cn/membership_cn.json
(last 26 dates kept; a rebuild of the same date replaces that date). The page shows the difference between the latest
date and the one before it. Never used by ranks, labels, ⚠ counts or the 对比池 score.

Views (same rules as the page defaults):
  x   交集         ② ∩ value channel ∩ default-parameter base value >= +15% above the price (latest ledger record), not
                   cyclical, not 交易驱动, no recent profit drop (cn/cards.CROSS_GAP_MIN)
  vu  价值 × 低估   value channel, not cyclical, no recent profit drop, price implies growth >= 5pt below the 4-year
                   revenue CAGR; does not require the ② growth screen
  lt  长期红利（10 年） / dv 红利筛选 / vc 价值通道   every row of the view
"""
import json
import os

from . import em

VU_RULES = {"ig_gap_max": -5.0, "x_gap_min": 0.15}
LABELS = {"x": "交集（成长 × 价值 × 低估）", "vu": "价值 × 低估", "lt": "长期红利（10 年）", "dv": "红利筛选", "vc": "价值通道"}
PATH = os.path.join(em.ROOT, "data", "cn", "membership_cn.json")
KEEP = 26


def is_vu(r):
    """Page defaults of the 价值 × 低估 view: not cyclical, no recent profit drop, and the gap (or implied growth < -30%)."""
    if r.get("cyclical") or r.get("recent_drop"):
        return False
    return r["ig_gap"] <= VU_RULES["ig_gap_max"] if r.get("ig_gap") is not None else r.get("igs") == "below"


def sets(payload, screen):
    from . import ledger as lmod
    vc = (payload.get("value") or {}).get("rows") or []
    last = {}
    try:
        for x in lmod.load():
            last[x["code"]] = x
    except Exception:  # noqa: BLE001 - no ledger yet: the 交集 set is empty
        last = {}
    by = {r["code"]: r for r in screen}
    cross = {}
    for v in vc:
        s, x = by.get(v["code"]), last.get(v["code"])
        if s and x and x.get("status") == "ok" and x.get("gap_base") is not None and x["gap_base"] >= VU_RULES["x_gap_min"] \
                and not (v.get("cyclical") or s.get("cyclical")) and s.get("label") != "偏博弈" and not v.get("recent_drop"):
            cross[v["code"]] = v.get("name") or ""
    nm = lambda rows: {r["code"]: r.get("name") or "" for r in rows}
    return {"x": cross, "vu": nm([r for r in vc if is_vu(r)]), "lt": nm((payload.get("longdiv") or {}).get("rows") or []),
            "dv": nm((payload.get("dividend") or {}).get("rows") or []), "vc": nm(vc)}


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
                              "in": [] if old is None else sorted([c, members[c]] for c in members if c not in old),
                              "out": [] if old is None else sorted([c, old[c]] for c in old if c not in members)}
    return out


def update(payload, screen, key, write=True, path=PATH):
    cur = sets(payload, screen)
    doc = _load(path)
    keys = [k for k in doc.get("keys") or [] if k["k"] != key] + [{"k": key, "sets": cur}]
    doc["keys"] = sorted(keys, key=lambda k: k["k"])[-KEEP:]
    if write:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    return diff(doc, key, cur)
