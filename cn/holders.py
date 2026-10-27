"""Tracked well-known holders in the top-10 float shareholders (前十大流通股东), last 5 quarterly report dates.
Display only: nothing here enters the ③ priority score, the 价值 / 博弈 label, warn_n or the 对比池 bkScore.

    python -m cn holders    -> data/cn/holders_<ts>.json

Source: Eastmoney datacenter RPT_F10_EH_FREEHOLDERS (the same report cn/em.float_holders() reads per code), queried
MARKET-WIDE: one paged query per holder rule x report date (filter HOLDER_NAME="..." for exact rules, HOLDER_TYPE="..."
for prefix rules - the datacenter rejects `like` filters with code 9501 "不支持like查询" - then matched locally), plus one
query per report date with HOLDER_RANK=1 to know which companies disclosed a top-10 list for that date. About 110
requests in total instead of 1236 x 5 per-code requests.
Verified fields (2026-10-23): SECURITY_CODE, SECURITY_NAME_ABBR, END_DATE, UPDATE_DATE, HOLDER_RANK, HOLDER_NAME,
HOLDER_TYPE (e.g. 全国社保基金 / 基本养老基金 / 其它), HOLDER_NEWTYPE (e.g. 社保 / 国家队 / QFII / 其他), SHARES_TYPE (A股 / H股),
HOLD_NUM shares, FREE_HOLDNUM_RATIO % of float A shares, HOLD_NUM_CHANGE (signed share delta as text, or "新进" / "不变"),
HOLDNUM_CHANGE_NAME (新进 / 增加 / 减少 / 不变), CHANGE_RATIO % change of the holding.

Not used: RPT_MUTUAL_HOLDSTOCKNORTH_STA (northbound Stock Connect holdings) still answers but only holds ONE date per
code (2026-09-30 on 2026-10-23, no history), so it cannot give a 12-month change. HKEX stopped daily per-stock
northbound disclosure in Aug 2024.

Periods: the last 5 quarter ends whose statutory disclosure deadline has passed (Q1 04-30, H1 08-31, Q3 10-31,
FY 04-30 next year) = 4 quarter-over-quarter steps, about 12 months. Later quarter ends with some disclosures are
fetched as `partial_periods` (with the number of companies that disclosed) and shown separately; statuses use full
periods only. 送转: share counts of earlier periods are multiplied by (1 + 送转 per share) for every ex-date in
(period, latest] (RPT_SHAREBONUS_DET, market-wide, same as cn/ranges.py); if that query fails, float_pct is compared.

Cached (em.fetch_json ttl: 7 days for full periods, 0.5 day for partial ones), polite (em 2 req/s limiter + a pause
between queries), resumable (finished pages come from the cache on a re-run; a checkpoint file is written after
every query). Order is per query, not per code: every query is market-wide, so a partial run has no per-code order.
stdout is English only.
"""
import datetime as dt
import glob
import json
import os
import time

from . import em

ROOT = em.ROOT
DATA_DIR = os.path.join(ROOT, "data", "cn")
REPORT = "RPT_F10_EH_FREEHOLDERS"
COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,END_DATE,UPDATE_DATE,HOLDER_RANK,HOLDER_NAME,HOLDER_TYPE,HOLDER_NEWTYPE,"
        "SHARES_TYPE,HOLD_NUM,FREE_HOLDNUM_RATIO,HOLD_NUM_CHANGE,HOLDNUM_CHANGE_NAME,CHANGE_RATIO")

# Holder rules. match: list of (mode, text) with mode exact | prefix | contains; a name matches a rule when any
# pattern matches and no `exclude_contains` text occurs in it. `query` is the server-side filter used to fetch the
# candidates (exact names are queried by name; prefix rules by HOLDER_TYPE because `like` is not supported).
# Several matching rows of one company and period (e.g. several 社保 portfolios) are summed into one entry.
HOLDER_RULES = {
    "hkscc": {"label": "香港中央结算有限公司", "short": "北向",
              "match": [("exact", "香港中央结算有限公司")], "exclude_contains": [],
              "query": '(HOLDER_NAME="香港中央结算有限公司")',
              "note": "陆股通（北向）名义持有人，汇总所有通过沪股通 / 深股通持股的境外投资者，含指数被动资金，不代表单一机构判断。"
                      "精确匹配；香港中央结算(代理人)有限公司是 H 股名义持有人，名称不同，不计入。"},
    "huijin_inv": {"label": "中央汇金投资有限责任公司", "short": "汇金",
                   "match": [("exact", "中央汇金投资有限责任公司")], "exclude_contains": [],
                   "query": '(HOLDER_NAME="中央汇金投资有限责任公司")', "note": "精确匹配。"},
    "huijin_am": {"label": "中央汇金资产管理有限责任公司", "short": "汇金资管",
                  "match": [("exact", "中央汇金资产管理有限责任公司")], "exclude_contains": [],
                  "query": '(HOLDER_NAME="中央汇金资产管理有限责任公司")', "note": "精确匹配。"},
    "csf": {"label": "中国证券金融股份有限公司", "short": "证金",
            "match": [("exact", "中国证券金融股份有限公司")], "exclude_contains": [],
            "query": '(HOLDER_NAME="中国证券金融股份有限公司")', "note": "精确匹配。"},
    "ssf": {"label": "全国社保基金", "short": "社保",
            "match": [("prefix", "全国社保"), ("contains", "-社保基金")], "exclude_contains": ["划转"],
            "query": '(HOLDER_TYPE="全国社保基金")',
            "note": "前缀匹配“全国社保”（全国社保基金N组合；数据里偶有省略“基金”二字的写法），另含基金公司受托管理的"
                    "“XX基金管理有限公司-社保基金N组合”；名称含“划转”的国有股划转账户不计入。同一公司多个组合合计。"},
    "pension": {"label": "基本养老保险基金", "short": "养老",
                "match": [("prefix", "基本养老保险基金"), ("contains", "-基本养老保险基金")], "exclude_contains": [],
                "query": '(HOLDER_TYPE="基本养老基金")',
                "note": "前缀匹配“基本养老保险基金”（如 基本养老保险基金一零零三组合）。同一公司多个组合合计。"},
}
HOLDER_FETCH = {"n_full": 5, "sleep": 0.3, "ttl_full": 7.0, "ttl_partial": 0.5, "flat_rel": 1e-4, "flat_pp": 0.005}
STATUS_CN = {"new": "新进前十", "up": "增持", "down": "减持", "flat": "不变", "out": "跌出前十", "inout": "期间进出前十"}


def log(msg):
    print(msg, flush=True)


def latest_holders():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "holders_*.json")))
    return files[-1] if files else None


def load_latest():
    p = latest_holders()
    if not p:
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), p
    except (OSError, ValueError) as e:
        log(f"WARNING: {os.path.basename(p)} unreadable ({type(e).__name__}); holders ignored")
        return None, p


# ----------------------------------------------------------------------------- pure helpers (tested in smoke)
def match_rule(rule, name):
    """True when `name` matches the rule's patterns and contains none of its exclude texts."""
    if not name:
        return False
    if any(x in name for x in rule.get("exclude_contains") or []):
        return False
    for mode, s in rule["match"]:
        if (mode == "exact" and name == s) or (mode == "prefix" and name.startswith(s)) or (mode == "contains" and s in name):
            return True
    return False


def deadline(period):
    """Statutory disclosure deadline of a quarterly / annual report."""
    d = dt.date.fromisoformat(period)
    return {3: dt.date(d.year, 4, 30), 6: dt.date(d.year, 8, 31), 9: dt.date(d.year, 10, 31), 12: dt.date(d.year + 1, 4, 30)}[d.month]


def quarter_ends(today, n_back=12):
    """The last n_back quarter ends on or before `today`, ascending."""
    qe = lambda y, m: dt.date(y, m, 31 if m in (3, 12) else 30)
    out, y = [], today.year
    while len(out) < n_back:
        for m in (12, 9, 6, 3):
            if qe(y, m) <= today and len(out) < n_back:
                out.append(qe(y, m).isoformat())
        y -= 1
    return sorted(out)


def plan_periods(today, n_full=5):
    """(full periods ascending, partial periods ascending): full = deadline already passed."""
    qs = quarter_ends(today, n_full + 4)
    full = [p for p in qs if deadline(p) < today]
    full = full[-n_full:]
    partial = [p for p in qs if p > full[-1]]
    return full, partial


def bonus_factor(events, after, upto):
    """Multiplier turning a share count held at `after` into the share basis of `upto`: product of (1 + 送转 per share)
    over ex-dates in (after, upto]. events = [{ex_date, bonus_it_per10}]."""
    f = 1.0
    for e in events or []:
        x, b = e.get("ex_date"), e.get("bonus_it_per10") or 0
        if x and after < x <= upto and b > 0:
            f *= 1 + b / 10
    return f


def derive(hist, full, reported, events, bonus_ok=True, partial=None):
    """12-month status of one tracked holder at one company. Pure function (tested in smoke).

    hist: [{period, shares, float_pct, change_shares, status, n}] (any order; may include partial periods)
    full: full periods ascending (5); reported: periods for which the company disclosed a top-10 list;
    events: 送转 events of the company [{ex_date, bonus_it_per10}]; bonus_ok False => compare float_pct instead.
    Returns None when the holder never appears in a full period or a partial one, else a dict:
      status (new | up | down | flat | out | inout | None), status_cn, in_latest, first_seen, base (first reported full
      period), latest (last reported full period), net_shares (latest - base on the latest share basis, both present),
      net_pp (float_pct latest - base, both present), n_up / n_down (quarter steps with both present), basis
      ('shares_bonus_adj' | 'float_pct'), adj (送转 factor applied to the base, 1.0 = none), hist (full periods,
      oldest first), partial (latest partial-period entry vs the latest full period, or None).
    Absence from the top 10 is not a zero holding: 'new' = not in the top 10 at the base period and in it at the latest;
    'out' = in it at the base and not at the latest.
    """
    H = {h["period"]: h for h in hist or []}
    rep = [p for p in full if p in reported]
    if not rep and not any(p in H for p in (partial or [])):
        return None
    base, last = (rep[0], rep[-1]) if rep else (None, None)
    seq = [H[p] for p in full if p in H]
    out = {"status": None, "status_cn": None, "in_latest": bool(last and last == full[-1] and last in H),
           "first_seen": seq[0]["period"] if seq else None, "base": base, "latest": last, "net_shares": None,
           "net_pp": None, "n_up": 0, "n_down": 0, "basis": "shares_bonus_adj" if bonus_ok else "float_pct", "adj": 1.0,
           "hist": seq, "partial": None}

    def cmp(a, pa, b, pb):
        """sign of b vs a (b later) on the chosen basis; returns (sign, delta_shares_adj, delta_pp)."""
        fa = bonus_factor(events, pa, pb)
        sa, sb = (a.get("shares") or 0) * fa, b.get("shares") or 0
        dpp = None if a.get("float_pct") is None or b.get("float_pct") is None else b["float_pct"] - a["float_pct"]
        if bonus_ok:
            sg = 0 if abs(sb - sa) <= HOLDER_FETCH["flat_rel"] * max(sa, sb, 1) else (1 if sb > sa else -1)
        else:
            sg = 0 if dpp is None or abs(dpp) <= HOLDER_FETCH["flat_pp"] else (1 if dpp > 0 else -1)
        return sg, sb - sa, dpp, fa

    for a, b in zip(full, full[1:]):
        if a in H and b in H:
            sg = cmp(H[a], a, H[b], b)[0]
            out["n_up"] += sg > 0
            out["n_down"] += sg < 0
    if rep:
        ib, il = base in H, last in H
        if ib and il and base != last:
            sg, ds, dpp, fa = cmp(H[base], base, H[last], last)
            out.update(status={1: "up", -1: "down", 0: "flat"}[sg], net_shares=round(ds), adj=round(fa, 6),
                       net_pp=None if dpp is None else round(dpp, 4))
        elif ib and il:
            out["status"] = None   # only one reported full period: no change to measure
        elif il:
            out["status"] = "new"
        elif ib:
            out["status"] = "out"
        elif seq:
            out["status"] = "inout"
    pp = [p for p in (partial or []) if p in reported]
    if pp:
        p = pp[-1]
        e = H.get(p)
        ref = H.get(full[-1]) if full[-1] in reported else None
        st = None
        if e and ref:
            st = {1: "up", -1: "down", 0: "flat"}[cmp(ref, full[-1], e, p)[0]]
        elif e:
            st = "new"
        elif ref:
            st = "out"
        if e or ref:
            out["partial"] = {"period": p, "entry": e, "status": st, "status_cn": STATUS_CN.get(st)}
    out["status_cn"] = STATUS_CN.get(out["status"])
    if out["status"] is None and not seq and not out["partial"]:
        return None
    return out


# ----------------------------------------------------------------------------- fetch
def _entry(rows, period):
    """Sum matching rows of one company / period / rule into one history entry."""
    rows = sorted(rows, key=lambda r: em.num(r.get("HOLDER_RANK")) or 99)
    sh = sum(em.num(r.get("HOLD_NUM")) or 0 for r in rows)
    pcts = [em.num(r.get("FREE_HOLDNUM_RATIO")) for r in rows]
    e = {"period": period, "shares": sh, "float_pct": None if any(p is None for p in pcts) else round(sum(pcts), 4),
         "n": len(rows), "rank": em.num(rows[0].get("HOLDER_RANK")), "update": em.day(rows[0].get("UPDATE_DATE"))}
    if len(rows) == 1:
        r = rows[0]
        raw = str(r.get("HOLD_NUM_CHANGE") or "")
        e["change_shares"] = None if raw in ("新进", "") else (0.0 if raw == "不变" else em.num(raw))
        e["status"] = r.get("HOLDNUM_CHANGE_NAME") or raw or None
    else:
        e["change_shares"], e["status"] = None, "合计"
        e["parts"] = [{"name": r.get("HOLDER_NAME"), "shares": em.num(r.get("HOLD_NUM")),
                       "float_pct": em.num(r.get("FREE_HOLDNUM_RATIO")), "status": r.get("HOLDNUM_CHANGE_NAME")} for r in rows]
    return e


def fetch_holders(today=None):
    from .ranges import bonus_events
    F = HOLDER_FETCH
    t0 = dt.datetime.now(em.CST)
    today = today or t0.date()
    full, partial = plan_periods(today, F["n_full"])
    periods = full + partial
    path = os.path.join(DATA_DIR, f"holders_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    log(f"holders: full periods {full}, partial {partial}; rules {list(HOLDER_RULES)}")

    reported, raw, fails, unmatched = {}, {}, [], {}
    queries = {}
    for key, rule in HOLDER_RULES.items():
        queries.setdefault(rule["query"], []).append(key)
    units = [("reported", p, None) for p in periods] + [("holder", p, q) for p in periods for q in queries]

    def save(complete, extra=None):
        items = {}
        for (code, key), per in raw.items():
            items.setdefault(code, {})[key] = [_entry(per[p], p) for p in periods if p in per]
        doc = {"fetched_at": t0.strftime("%Y-%m-%d %H:%M"), "today": today.isoformat(), "periods": full,
               "partial_periods": [{"period": p, "n_reported": len(reported.get(p, ())), "deadline": deadline(p).isoformat()}
                                   for p in partial],
               "deadlines": {p: deadline(p).isoformat() for p in periods},
               "n_reported": {p: len(reported.get(p, ())) for p in periods},
               "reported": {p: sorted(reported.get(p, ())) for p in periods},
               "rules": {k: {kk: v for kk, v in r.items()} for k, r in HOLDER_RULES.items()}, "fetch_rules": F,
               "complete": complete, "failed": fails, "items": items,
               "src": {"report": REPORT, "url": em.DC_URL, "columns": COLS,
                       "note": "market-wide queries per rule x END_DATE; HOLDER_RANK=1 query per END_DATE for disclosure coverage"},
               "http": dict(em.STATS)}
        if extra:
            doc.update(extra)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path)
        return doc

    for i, (kind, p, q) in enumerate(units, 1):
        ttl = F["ttl_partial"] if p in partial else F["ttl_full"]
        net0 = em.STATS["network"]
        if kind == "reported":
            rows, ok = em.dc_report(REPORT, "SECURITY_CODE,END_DATE,HOLDER_RANK", f"(END_DATE='{p}')(HOLDER_RANK=1)",
                                    "SECURITY_CODE", "1", ttl=ttl, label=f"holders/reported {p}")
            reported.setdefault(p, set()).update(r["SECURITY_CODE"] for r in rows if em.is_a_code(r.get("SECURITY_CODE")))
            log(f"  [{i}/{len(units)}] {p} companies with a top-10 float holder list: {len(reported[p])} ok={ok}")
        else:
            keys = queries[q]
            rows, ok = em.dc_report(REPORT, COLS, f"{q}(END_DATE='{p}')", "SECURITY_CODE,HOLDER_RANK", "1,1", ttl=ttl,
                                    label=f"holders/{'+'.join(keys)} {p}")
            n_m, seen = 0, set()
            for r in rows:
                c, nm = r.get("SECURITY_CODE"), r.get("HOLDER_NAME")
                if not em.is_a_code(c) or "A股" not in str(r.get("SHARES_TYPE") or "A股"):
                    continue
                dk = (c, nm, r.get("HOLDER_RANK"))
                if dk in seen:
                    continue
                seen.add(dk)
                reported.setdefault(p, set()).add(c)
                hit = False
                for k in keys:
                    if match_rule(HOLDER_RULES[k], nm):
                        raw.setdefault((c, k), {}).setdefault(p, []).append(r)
                        n_m += 1
                        hit = True
                if not hit:
                    unmatched[nm] = unmatched.get(nm, 0) + 1
            log(f"  [{i}/{len(units)}] {p} {'+'.join(keys)}: rows={len(rows)} matched={n_m} ok={ok}")
        if not ok:
            fails.append({"kind": kind, "period": p, "query": q})
        save(False)
        if em.STATS["network"] > net0:
            time.sleep(F["sleep"])

    beg = full[0]
    events, ev_ok = bonus_events(beg, periods[-1], F["ttl_full"])
    log(f"  送转/dividend events {beg}..{periods[-1]}: {sum(len(v) for v in events.values())} rows, ok={ev_ok}")
    bonus = {c: [e for e in v if (e.get("bonus_it_per10") or 0) > 0] for c, v in events.items()}
    bonus = {c: v for c, v in bonus.items() if v}
    top_unmatched = sorted(unmatched.items(), key=lambda x: -x[1])[:15]
    doc = save(not fails, {"bonus_ok": ev_ok, "bonus": bonus, "unmatched_top": top_unmatched})
    n_codes = {k: sum(1 for c in doc["items"].values() if k in c) for k in HOLDER_RULES}
    n_latest = {k: sum(1 for c in doc["items"].values() if any(e["period"] == full[-1] for e in c.get(k, []))) for k in HOLDER_RULES}
    log(f"saved {os.path.relpath(path, ROOT)}: codes_with_any={n_codes} in_top10_at_{full[-1]}={n_latest} "
        f"failed={len(fails)} bonus_codes={len(bonus)} http={em.STATS}")
    log(f"  unmatched names returned by the type queries (top): {len(unmatched)} distinct")
    if fails:
        log(f"WARNING: {len(fails)} queries incomplete (complete=false); re-run `python -m cn holders` to resume from the cache")
    return path if doc["items"] else None


# ----------------------------------------------------------------------------- build-time attach (display only)
def derive_code(doc, code):
    """{rule_key: derived dict} for one code (only rules with any history), or {}."""
    if not doc:
        return {}
    full = doc.get("periods") or []
    parts = [x["period"] for x in doc.get("partial_periods") or []]
    rep = doc.get("_rep_sets")
    if rep is None:
        rep = doc["_rep_sets"] = {p: set(v) for p, v in (doc.get("reported") or {}).items()}
    reported = {p for p in full + parts if code in rep.get(p, ())}
    it = (doc.get("items") or {}).get(code) or {}
    ev = (doc.get("bonus") or {}).get(code) or []
    out = {}
    for k in HOLDER_RULES:
        if k not in it:
            continue
        d = derive(it[k], full, reported, ev, doc.get("bonus_ok", True), parts)
        if d:
            out[k] = d
    return out


def compact(d):
    """Template payload for one derived dict (short keys keep the page small)."""
    h = lambda e: [e["period"], e.get("shares"), e.get("float_pct"), e.get("change_shares"), e.get("status"), e.get("n", 1)]
    p = d.get("partial")
    return {"s": d["status"], "il": d["in_latest"], "fs": d["first_seen"], "b": d["base"], "l": d["latest"],
            "ns": d["net_shares"], "np": d["net_pp"], "nu": d["n_up"], "nd": d["n_down"], "adj": d["adj"],
            "h": [h(e) for e in d["hist"]],
            "q": None if not p else {"p": p["period"], "s": p["status"], "e": h(p["entry"]) if p["entry"] else None}}


def attach(rows, doc):
    """Add r['inst'] = {rule_key: compact} to ② screen rows in place (display only; called after all scoring)."""
    for r in rows:
        r["inst"] = {k: compact(v) for k, v in derive_code(doc, r["code"]).items()}


def meta(doc, path, rows):
    if not doc:
        return {"file": None}
    full = doc.get("periods") or []
    cnt = {}
    for k in HOLDER_RULES:
        c = {"in_latest": 0, "new": 0, "up": 0, "down": 0, "out": 0, "flat": 0, "inout": 0}
        cv = dict(c)
        for r in rows:
            x = (r.get("inst") or {}).get(k)
            if not x:
                continue
            for tgt in ([c, cv] if r.get("label") == "偏价值" else [c]):
                tgt["in_latest"] += bool(x["il"])
                if x["s"] in tgt:
                    tgt[x["s"]] += 1
        cnt[k] = {"all": c, "value": cv}
    return {"file": os.path.basename(path), "fetched_at": doc.get("fetched_at"), "periods": full,
            "partial": doc.get("partial_periods") or [], "deadlines": doc.get("deadlines") or {},
            "n_reported": doc.get("n_reported") or {}, "complete": doc.get("complete"), "bonus_ok": doc.get("bonus_ok"),
            "rules": [{"key": k, "label": r["label"], "short": r["short"], "note": r["note"],
                       "match": [list(m) for m in r["match"]], "exclude": r.get("exclude_contains") or []}
                      for k, r in HOLDER_RULES.items()],
            "counts": cnt, "n_screen": len(rows), "n_reported_screen": sum(1 for r in rows if full and r["code"] in doc["_rep_sets"].get(full[-1], ()))}
