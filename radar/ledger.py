"""Valuation ledger (估值台账): the page's cash-flow valuation under DEFAULT parameters, one record per company per run, append-only.

    python -m radar ledger      append one record per US ticker for the latest raw + valuation files, then rebuild the site

Display only: nothing here feeds the score, factors, level, PEG screen tiers or the comparison-basket score.

Every record is computed by radar/valuation.py (dcf_prep / dcf_case via dcf_entry, the exact mirror of the page engine incl. the
FCF-margin basis rules auto / avg3 / fy / ttm / ramp and the one-off guard) with valuation.default_params(D.val). There is no
separate engine here; the attribution below only re-evaluates valuation.dcf_engine / ri_engine on mixed inputs.

File: data/ledger/us_ledger.jsonl (one JSON record per line, never rewritten or deleted) plus data/ledger/us_ledger_meta.json
(run index). A record is skipped when the same (ticker, val_file, raw_file) already exists. Superseded files are moved (not
deleted) to data/ledger/archive/.

Change vs the previous record of the same ticker (`chg`). Attribution of the base value change by sequential substitution in this
fixed order: (1) R0 revenue base, (2) FCF margin path (m0 / basis / ramp start), (3) starting growth g0, (4) net cash and shares,
(5) parameters rf / ERP / gT / ramp N, T. Each step swaps one group from the previous to the current record and re-evaluates the
base case; contributions are in % of the previous base value and sum to the total change (interaction effects land on the later
step, so the split depends on the order). Residual income (financial profile): (1) ROE, (2) book value per share, (3) parameters.
Status changes (e.g. negfcf -> ok) are reported, not attributed; negfcf tracks the required margin, ri the RI value per share.
"""
import datetime as dt
import json
import os

from . import valuation as VAL
from .paths import CFG, DATA

LDIR = os.path.join(DATA, "ledger")
LEDGER = os.path.join(LDIR, "us_ledger.jsonl")
META = os.path.join(LDIR, "us_ledger_meta.json")
ARCHIVE = os.path.join(LDIR, "archive")
LC = CFG.get("valuation", {}).get("ledger", {})
FLAT = float(LC.get("flat_pct", 2.0))
EMBED_N = int(LC.get("embed_n", 26))
STATUS_CN = {"ok": "FCF 三情景", "negfcf": "FCF ≤ 0（只做反向）", "ri": "剩余收益法", "na": "不适用"}
GSRC_CN = {"cons": "一致预期", "cagr": "3 年 CAGR", "yoy": "营收同比"}


def pick_entries(data):
    """One entry per ticker: the first in the dashboard order."""
    out = {}
    for e in data["entries"]:
        out.setdefault(e["ticker"], e)
    return out


# ---------------------------------------------------------------- records
def record(e, data, P, meta):
    I, o = VAL.dcf_entry(e, data, P)
    m = e.get("mkt") or {}
    st = o["mode"] if o["mode"] in ("na", "ri") else ("negfcf" if o.get("neg") else "ok")
    sc = {s["k"]: s["perShare"] for s in o.get("sc") or []}
    base = sc.get("base")
    B = o.get("basis") or {}
    ramp = B.get("key") == "ramp"
    revM, revG = o.get("revM") or {}, o.get("revG") or {}
    why = o.get("why") if st == "na" else "基准 FCF 利润率 ≤ 0，只给需要的利润率" if st == "negfcf" else None
    return {**meta, "ticker": e["ticker"], "name": e.get("name"), "sector": e["sector"], "segment": e["segment"], "kind": e["kind"],
            "profile": e.get("profile"), "price": I.get("price"), "ccy": I.get("ccy"), "status": st, "why": why,
            "basis": {"key": B["key"], "why": B["why"], "fb": B["fb"], "mode": P["mbasis"]} if B else None,
            "guard": list(I.get("guard") or []),
            "m_start": B.get("ms") if ramp else None, "ramp_T": B["m0"] if ramp else None, "ramp_N": B.get("N") if ramp else None,
            "ramp_n": P["ramp_n"], "ramp_t": P["ramp_t"] / 100,       # the parameters in effect (used when a later record switches to ramp)
            "m0": o.get("m0"), "m_avg3": I.get("mAvg"), "m_avg_n": I.get("m0n"), "m_ttm": I.get("mTtm"), "m_fy": I.get("mFy"),
            "r0": I.get("R0"), "g0": o.get("g0"), "g0raw": I.get("g0raw"), "gsrc": I.get("gkind"), "capped": o.get("capped"),
            "net_cash": I.get("netCash"), "shares": I.get("shares"), "sh_src": I.get("sh_src"),
            "rf": P.get("rf"), "erp": P["erp"], "gT": o.get("gT"), "r": o.get("r"), "warn": o.get("warn") or [],
            "roe": I.get("roe"), "bvps": I.get("bvps"),
            "bear": sc.get("bear"), "base": base, "bull": sc.get("bull"),
            "implied_g": o.get("implied") if st == "ok" else None,
            "implied_side": revG.get("side") if st == "ok" and not revG.get("ok") else None,
            "implied_m": revM.get("x") if st in ("ok", "negfcf") and revM.get("ok") else None,
            "implied_m_kind": ("steady_T" if ramp else "m0") if st in ("ok", "negfcf") else None,
            "implied_roe": o.get("implied") if st == "ri" else None,
            "cons_g": (I.get("gCons") or {}).get("g"), "cons_n": (I.get("gCons") or {}).get("n"),
            "target_median": m.get("target"), "hi52": m.get("hi52"), "lo52": m.get("lo52"),
            "gap_base": base / I["price"] - 1 if base is not None and I.get("price") else None}


# ---------------------------------------------------------------- change and attribution
def _pct(a, b):
    return (b / a - 1) * 100 if a is not None and b is not None and a > 0 else None


def _pp(a, b):
    return (b - a) * 100 if a is not None and b is not None else None


def _state(r):
    """Inputs of the base case of record r (status ok), split into the attribution groups."""
    k = (r.get("basis") or {}).get("key")
    return {"R0": r["r0"], "bk": k, "mm": None if k == "ramp" else r["m0"], "ms": r.get("m_start"), "g0": r["g0"],
            "nc": r["net_cash"], "sh": r["shares"], "rf": r["rf"], "erp": r["erp"], "gT": r["gT"],
            "N": r.get("ramp_N") or r.get("ramp_n"), "T": r.get("ramp_T") if r.get("ramp_T") is not None else r.get("ramp_t")}


def fcf_value(x):
    """Base-case value per share from a state (exactly valuation.dcf_case's base scenario)."""
    ramp = {"ms": x["ms"], "N": x["N"]} if x["bk"] == "ramp" else None
    return VAL.dcf_engine(x["R0"], x["T"] if ramp else x["mm"], x["g0"], x["gT"], x["rf"] / 100 + x["erp"] / 100,
                          x["nc"], x["sh"], ramp=ramp)["perShare"]


def ri_value(x):
    return VAL.ri_engine(x["roe"], x["gT"], x["rf"] / 100 + x["erp"] / 100, x["bvps"])["perShare"]


FCF_STEPS = [("R0", "营收基数", ("R0",)), ("m0", "FCF 利润率路径", ("bk", "mm", "ms")), ("g0", "起始增速", ("g0",)),
             ("bs", "净现金与股数", ("nc", "sh")), ("rate", "参数 rf/ERP/gT/爬坡 N,T", ("rf", "erp", "gT", "N", "T"))]
RI_STEPS = [("roe", "ROE", ("roe",)), ("bvps", "每股净资产", ("bvps",)), ("rate", "参数 rf/ERP/gT", ("rf", "erp", "gT"))]


def attribution(p, c):
    """Sequential substitution of the base value per share -> (steps [{k, l, c}], total %, v_prev, v_cur); c in % of v_prev."""
    if c["status"] == "ok":
        steps, fn, cur, tgt = FCF_STEPS, fcf_value, _state(p), _state(c)
    else:
        steps, fn = RI_STEPS, ri_value
        cur, tgt = ({"roe": r["roe"], "bvps": r["bvps"], "rf": r["rf"], "erp": r["erp"], "gT": r["gT"]} for r in (p, c))
    v_prev = fn(cur)
    if not v_prev or v_prev <= 0:
        return None, None, v_prev, None
    out, last = [], v_prev
    for key, label, ks in steps:
        for k in ks:
            cur[k] = tgt[k]
        v = fn(cur)
        out.append({"k": key, "l": label, "c": (v - last) / v_prev * 100})
        last = v
    return out, (last - v_prev) / v_prev * 100, v_prev, last


def change(p, c):
    """`chg` of record c relative to the previous record p of the same ticker."""
    same_ccy = p.get("ccy") == c.get("ccy")
    ch = {"prev_date": p.get("date"), "prev_run_at": p.get("run_at"), "prev_status": p.get("status"),
          "status_changed": p.get("status") != c.get("status"),
          "price_pct": _pct(p.get("price"), c.get("price")) if same_ccy else None,
          "base_pct": None, "gap_pp": _pp(p.get("gap_base"), c.get("gap_base")),
          "implied_pp": (_pp(p.get("implied_g"), c.get("implied_g")) if c["status"] == "ok" else
                         _pp(p.get("implied_roe"), c.get("implied_roe")) if c["status"] == "ri" else None),
          "mreq_pp": _pp(p.get("implied_m"), c.get("implied_m")) if c["status"] == "negfcf" and p.get("status") == "negfcf" else None,
          "cons_pp": _pp(p.get("cons_g"), c.get("cons_g")),
          "cons_n_chg": (c["cons_n"] - p["cons_n"]) if p.get("cons_n") is not None and c.get("cons_n") is not None else None,
          "target_pct": _pct(p.get("target_median"), c.get("target_median")),
          "m0_pp": _pp(p.get("m0"), c.get("m0")), "ms_pp": _pp(p.get("m_start"), c.get("m_start")),
          "g0_pp": _pp(p.get("g0"), c.get("g0")), "r0_pct": _pct(p.get("r0"), c.get("r0")),
          "basis_from": (p.get("basis") or {}).get("key"), "basis_to": (c.get("basis") or {}).get("key"),
          "gsrc_from": p.get("gsrc"), "gsrc_to": c.get("gsrc"), "attr": None, "attr_total": None}
    if not ch["status_changed"] and c["status"] in ("ok", "ri") and same_ccy:
        attr, tot, _, _ = attribution(p, c)
        if attr is not None:
            ch.update(attr=attr, attr_total=tot, base_pct=tot)
    ch["tag"] = tag(ch)
    return ch


def tag(ch):
    """基本面驱动 / 价格驱动 / 参数驱动 / 基本不变 / '' (mixed or not comparable)."""
    bp, pp, attr = ch.get("base_pct"), ch.get("price_pct"), ch.get("attr")
    if bp is None or not attr:
        return ""
    tot = sum(abs(a["c"]) for a in attr)
    fund = sum(abs(a["c"]) for a in attr if a["k"] in ("R0", "m0", "g0", "roe", "bvps"))
    rate = sum(abs(a["c"]) for a in attr if a["k"] == "rate")
    if abs(bp) >= FLAT and tot > 0 and rate > tot / 2:
        return "参数驱动"
    if abs(bp) < FLAT and pp is not None and abs(pp) >= FLAT:
        return "价格驱动"
    if abs(bp) >= FLAT and pp is not None and abs(bp) > abs(pp) and tot > 0 and fund > tot / 2:
        return "基本面驱动"
    if abs(bp) < FLAT and (pp is None or abs(pp) < FLAT):
        return "基本不变"
    return ""


def _s(v, d=1, u="%"):
    """Signed number with a real minus sign."""
    if v is None:
        return "—"
    t = f"{abs(v):.{d}f}{u}"
    return ("+" if v > 0 and round(v, d) > 0 else "−" if v < 0 and round(abs(v), d) > 0 else "") + t


def _p(v, d=1):
    return "—" if v is None else f"{v * 100:.{d}f}%".replace("-", "−")


def _gap(v):
    return "—" if v is None else f"{'高于' if v >= 0 else '低于'} {abs(v) * 100:.0f}%"


def _driver(a, p, c, ch):
    k, t = a["k"], f"{a['l']} {_s(a['c'], 1, 'pt')}"
    if k == "R0":
        return t + f"，R0 {_s(ch['r0_pct'])}"
    if k == "m0":
        if ch["basis_from"] != ch["basis_to"]:
            return t + f"，口径 {ch['basis_from'] or '—'}→{ch['basis_to'] or '—'}"
        if ch["basis_to"] == "ramp":
            return t + f"，爬坡起点 {_p(p.get('m_start'))}→{_p(c.get('m_start'))}"
        return t + f"，m0 {_p(p.get('m0'))}→{_p(c.get('m0'))}"
    if k == "g0":
        if ch["gsrc_from"] != ch["gsrc_to"]:
            return t + f"，来源 {GSRC_CN.get(ch['gsrc_from'], '—')}→{GSRC_CN.get(ch['gsrc_to'], '—')}"
        return t + f"，g0 {_p(p.get('g0'))}→{_p(c.get('g0'))}"
    if k == "bs":
        return t + f"，股数 {_s(_pct(p.get('shares'), c.get('shares')))}"
    if k == "rate":
        bits = [f"rf {_s(c['rf'] - p['rf'], 2, 'pt')}"] if c.get("rf") != p.get("rf") else []
        if c.get("erp") != p.get("erp"):
            bits.append(f"ERP {_s(c['erp'] - p['erp'], 1, 'pt')}")
        if c.get("gT") != p.get("gT") and c.get("gT") is not None and p.get("gT") is not None:
            bits.append(f"gT {_s((c['gT'] - p['gT']) * 100, 2, 'pt')}")
        return t + ("，" + "、".join(bits) if bits else "")
    if k == "roe":
        return t + f"，ROE {_p(p.get('roe'))}→{_p(c.get('roe'))}"
    return t


def conclude(p, c, ch):
    """One plain-fact Chinese line (no buy / sell wording)."""
    if p is None or ch is None:
        return f"首次记录（{c.get('date')}），下次更新后显示变化"
    parts, pp = [], ch.get("price_pct")
    if ch["status_changed"]:
        t = f"估值方式变化：{STATUS_CN.get(p.get('status'), p.get('status'))} → {STATUS_CN.get(c.get('status'), c.get('status'))}，不做归因"
        if ch["basis_from"] != ch["basis_to"] and ch["basis_to"]:
            t += f"（口径 {ch['basis_from'] or '—'}→{ch['basis_to']}）"
        if c.get("status") == "na" and c.get("why"):
            t += f"（{c['why']}）"
        parts.append(t)
        if c.get("base") is not None:
            parts.append(f"本期基准估值 {c['base']:.2f}，相对现价{_gap(c.get('gap_base'))}")
    elif c["status"] == "negfcf":
        req = "需要的稳态 FCF 利润率" if c.get("implied_m_kind") == "steady_T" else "需要的 FCF 利润率"
        parts.append(f"FCF 利润率 ≤ 0，仍只做反向；{req} {_p(p.get('implied_m'))}→{_p(c.get('implied_m'))}")
    elif c["status"] == "na":
        parts.append(f"不适用：{c.get('why') or '—'}")
    bp = ch.get("base_pct")
    if bp is not None:
        t = f"{'剩余收益估值' if c['status'] == 'ri' else '基准估值'} {_s(bp)}"
        attr = sorted(ch.get("attr") or [], key=lambda a: -abs(a["c"]))
        if attr and abs(attr[0]["c"]) >= 0.05:
            sp = lambda a, b: a + (" " if b[:1].isascii() else "") + b  # noqa: E731  (space before Latin / digits)
            d = [sp("主要来自", _driver(attr[0], p, c, ch))]
            if len(attr) > 1 and abs(attr[1]["c"]) >= 0.05 and abs(attr[1]["c"]) > abs(bp) / 3:
                d.append(sp("其次", _driver(attr[1], p, c, ch)))
            t += "（" + "；".join(d) + "）"
        parts.append(t)
    if pp is not None:
        parts.append(f"股价 {_s(pp)}")
    if not ch["status_changed"] and p.get("gap_base") is not None and c.get("gap_base") is not None:
        parts.append(f"基准估值相对现价从{_gap(p['gap_base'])} 变为{_gap(c['gap_base'])}")
    if c.get("status") == "ok" and p.get("implied_g") is not None and c.get("implied_g") is not None:
        parts.append(f"隐含增速 {_p(p['implied_g'], 0)}→{_p(c['implied_g'], 0)}")
    elif c.get("status") == "ri" and p.get("implied_roe") is not None and c.get("implied_roe") is not None:
        parts.append(f"隐含 ROE {_p(p['implied_roe'])}→{_p(c['implied_roe'])}")
    if ch.get("cons_pp") is not None and abs(ch["cons_pp"]) >= 0.05:
        parts.append(f"一致预期增速 {_s(ch['cons_pp'], 1, 'pt')}" + (f"（分析师 {ch['cons_n_chg']:+d} 家）" if ch.get("cons_n_chg") else ""))
    tg = ch.get("tag") or ""
    return "；".join(parts) + "。" + tg


# ---------------------------------------------------------------- file io
def read(path=None):
    path = path or LEDGER
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def append(new, path=None):
    """Compute chg / concl against the previous record of each ticker and append; existing (ticker, val_file, raw_file) are skipped."""
    path = path or LEDGER
    old = read(path)
    seen = {(r["ticker"], r["val_file"], r["raw_file"]) for r in old}
    last = {}
    for r in old:
        last[r["ticker"]] = r
    add = []
    for c in new:
        if (c["ticker"], c["val_file"], c["raw_file"]) in seen:
            continue
        p = last.get(c["ticker"])
        ch = change(p, c) if p else None
        c["chg"], c["concl"] = ch, conclude(p, c, ch)
        add.append(c)
        last[c["ticker"]] = c
    if add:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:   # append only: existing lines are never rewritten
            for c in add:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return add, len(new) - len(add)


def _meta_path(path):
    return META if (path or LEDGER) == LEDGER else os.path.splitext(path)[0] + "_meta.json"


def write_meta(path=None):
    rows = read(path)
    runs = {}
    for r in rows:
        k = (r["val_file"], r["raw_file"])
        x = runs.setdefault(k, {"date": r["date"], "run_at": r["run_at"], "val_file": r["val_file"], "raw_file": r["raw_file"], "n": 0})
        x["n"] += 1
    meta = {"version": 2, "updated_at": dt.datetime.now().isoformat(timespec="seconds"), "file": os.path.basename(path or LEDGER),
            "engine": "radar/valuation.py dcf_entry (default params, FCF margin basis)",
            "n_records": len(rows), "n_tickers": len({r["ticker"] for r in rows}), "runs": sorted(runs.values(), key=lambda x: x["run_at"]),
            "note": "append-only; default parameters; display only (not a target price, not advice); superseded files in archive/"}
    with open(_meta_path(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def compute(data, V, val_file, raw_file, run_at=None):
    """V: the full valuation file (for its date); the engine reads data["val"] (D.val), exactly what the page reads."""
    P = VAL.default_params(data.get("val"))
    meta = {"date": ((V or {}).get("fetched_at") or "")[:10], "px_date": (data.get("fetched_at") or "")[:10],
            "run_at": run_at or dt.datetime.now().isoformat(timespec="seconds"),
            "raw_file": os.path.basename(raw_file), "val_file": os.path.basename(val_file) if val_file else None}
    return [record(e, data, P, meta) for _, e in sorted(pick_entries(data).items())]


def run(path=None):
    from .build import latest_raw_file, model
    raw_file, raw = latest_raw_file()
    val_file, V = VAL.latest_file()
    if not V:
        raise SystemExit("no valuation file in data/; run `python -m radar valuation` first")
    data = model(raw, with_ledger=False)
    recs = compute(data, V, val_file, raw_file)
    add, skipped = append(recs, path)
    meta = write_meta(path)
    st = {}
    for r in add:
        st[r["status"]] = st.get(r["status"], 0) + 1
    print(f"ledger: {len(recs)} tickers, appended {len(add)}, skipped {skipped} (already recorded for "
          f"{os.path.basename(val_file)} + {os.path.basename(raw_file)}); status of appended: {st}; "
          f"file has {meta['n_records']} records over {len(meta['runs'])} runs")
    return add, skipped


# ---------------------------------------------------------------- embed for the page (D.ledger)
def _r(v, d=4):
    return None if v is None else round(v, d)


def _compact(r):
    ch = r.get("chg")
    x = None
    if ch:
        x = {k: _r(ch.get(k), 3) for k in ("price_pct", "base_pct", "gap_pp", "implied_pp", "mreq_pp", "cons_pp", "target_pct")}
        x.update(cn=ch.get("cons_n_chg"), bf=ch.get("basis_from"), bt=ch.get("basis_to"), ps=ch.get("prev_status"), pd=ch.get("prev_date"),
                 a=[[a["k"], _r(a["c"], 3)] for a in ch.get("attr") or []] or None, tag=ch.get("tag") or "")
    b = r.get("basis") or {}
    o = {"d": r["date"], "pd": r.get("px_date"), "ra": r["run_at"], "st": r["status"], "ccy": r.get("ccy"),
         "px": _r(r.get("price"), 4), "br": _r(r.get("bear"), 4), "bs": _r(r.get("base"), 4), "bl": _r(r.get("bull"), 4),
         "gap": _r(r.get("gap_base")), "ig": _r(r.get("implied_g")), "isd": r.get("implied_side"), "im": _r(r.get("implied_m")),
         "iroe": _r(r.get("implied_roe")), "m0": _r(r.get("m0")), "mb": b.get("key"), "g0": _r(r.get("g0")),
         "sec": r["sector"], "seg": r["segment"], "kind": r["kind"], "nm": (r.get("name") or "")[:40], "c": r.get("concl"), "x": x}
    if r.get("why"):
        o["why"] = r["why"]
    if b.get("key") == "ramp":
        o.update(ms=_r(r.get("m_start")), rn=r.get("ramp_N"))
    if r.get("guard"):
        o["gd"] = len(r["guard"])
    return o


def embed(path=None, n=None):
    rows = read(path)
    if not rows:
        return None
    n = n or EMBED_N
    by = {}
    for r in rows:
        by.setdefault(r["ticker"], []).append(r)
    runs = {}
    for r in rows:
        k = r["val_file"] + "|" + r["raw_file"]
        x = runs.setdefault(k, {"k": k, "date": r["date"], "px_date": r.get("px_date"), "run_at": r["run_at"], "n": 0})
        x["n"] += 1
    rl = sorted(runs.values(), key=lambda x: x["run_at"])
    return {"runs": rl[-n:], "n_runs": len(rl), "latest": rl[-1]["k"], "prev": rl[-2]["k"] if len(rl) > 1 else None, "flat_pct": FLAT,
            "by": {t: [{**_compact(r), "k": r["val_file"] + "|" + r["raw_file"]} for r in v[-n:]] for t, v in by.items()}}
