"""估值台账 (valuation ledger): the 现金流估值 of every ② screen row under the DEFAULT page parameters, one record per code per
data update, appended to data/cn/ledger/cn_ledger.jsonl (local only, append-only: lines are never rewritten or deleted).
Display only: nothing here enters the ③ priority score, the 价值 / 博弈 label, warn_n or the 对比池 bkScore.
台账记录的是模型在默认参数下的输出，用来对比数据变化带来的估值变化；不是目标价，不是买卖建议；方法未回测.

    python -m cn ledger   latest snapshot + valuation + cards + ranges + holders files -> append records (no network)

Idempotent per (code, snap_file, val_file): a second run on the same files appends 0 lines.
Every record is computed by cn/valuation.py's dcf_case() (the Python mirror of the page JS) with DCF_DEFAULTS
(FCF 利润率口径 auto, ramp N 5 / T 15%) and rf = the valuation file's China 10y yield; cards get consensus growth and the
one-off guard input (c) eps_drop() exactly as `python -m cn build` gives them to the page (c.dcf_g / c.dcf_drop).
Each new record carries `chg` = the change against the previous record of the same code (None for the first record):
  price %, base value %, gap_base pp (base / price - 1), implied growth pp, consensus growth pp (+ institution count change),
  label change, warn_n change, basis change, required FCF margin pp, and an attribution of the change by sequential
  substitution through the mirror's dcf_engine (each factor group is switched from its previous to its current value in
  this fixed order; the contributions telescope, so they sum to the total):
    base value (status ok -> ok):  (1) 营收基数 R0  (2) FCF 利润率路径 (basis, m0 / ramp start; basis switches are named)
                                   (3) 起始增速 g0  (4) 净现金与股本  (5) 参数 rf / ERP / gT / ramp N, T
    required FCF margin (negfcf -> negfcf; under ramp the required steady-state T):  (0) 股价  then (1)..(5)
  Status changes (ok <-> negfcf <-> na) are stated, not attributed.
  A rule-based one-line Chinese conclusion (facts only) names the largest driver, plus the second one when its share of
  the sum of absolute contributions is > 1/3, and a tag: 价格驱动 (|base change| < 2%), 参数驱动 (largest driver =
  parameters), otherwise 基本面驱动 (状态变化 for status changes). stdout is English only.
"""
import datetime as dt
import json
import os

from . import em
from . import valuation as V

ROOT = em.ROOT
LEDGER_DIR = os.path.join(ROOT, "data", "cn", "ledger")
LEDGER_FILE = os.path.join(LEDGER_DIR, "cn_ledger.jsonl")
LEDGER_RULES = {"flat_pct": 2.0, "second_share": 1 / 3, "page_keep": 26}
ATTR_BASE = [("r0", "营收基数"), ("m0", "FCF 利润率路径"), ("g0", "起始增速"), ("ncsh", "净现金与股本"),
             ("par", "参数 rf/ERP/gT/爬坡 N,T")]
ATTR_MREQ = [("price", "股价")] + ATTR_BASE
BASIS_CN = {"avg3": "历史平均", "fy": "最近财年", "ttm": "TTM", "ramp": "利润率爬坡"}
STATUS_CN = {"ok": "三情景", "negfcf": "FCF ≤ 0", "na": "不计算"}
MINUS = "\u2212"


def log(msg):
    print(msg, flush=True)


def _r(x, d=6):
    return None if x is None else round(x, d)


# ----------------------------------------------------------------------------- inputs (same assembly as cn.build, offline)
def gather(snap_path=None):
    """Everything a record needs, built the same way as `python -m cn build` (no network)."""
    from .build import analyze, latest_snap
    from . import cards as cardmod, ranges as rngmod, holders as hmod
    snap_path = snap_path or latest_snap()
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    res = analyze(snap)
    qd = next((q.get("as_of") for q in snap["quotes"] if q.get("as_of")), None)
    cpath = cardmod.latest_cards()
    cdoc = None
    if cpath:
        with open(cpath, encoding="utf-8") as f:
            cdoc = json.load(f)
    rdoc, _ = rngmod.load_latest()
    rngmod.attach(res["screen"], rdoc, qd)
    hdoc, _ = hmod.load_latest()
    hmod.attach(res["screen"], hdoc)
    # the cards exactly as the page gets them (cn.build): consensus growth c.dcf_g and one-off guard input (c) c.dcf_drop
    from .build import RULES
    cards = cardmod.card_payload(snap, res, cdoc, RULES)
    latest = snap["periods"]["latest"]
    cons = {c["code"]: V.consensus_growth(c.get("consensus"), latest) for c in cards}
    drops = {c["code"]: V.eps_drop(c.get("consensus"), latest) for c in cards}
    vdoc, vpath = V.load_latest()
    V.attach(res["screen"], vdoc, snap)
    rf = ((vdoc or {}).get("rf") or {}).get("value")
    return {"snap_file": os.path.basename(snap_path), "val_file": os.path.basename(vpath) if vdoc else None,
            "cards_file": os.path.basename(cpath) if cpath else None, "date": qd, "rows": res["screen"],
            "cards": {c["code"] for c in cards}, "cons": cons, "drops": drops, "P": {"rf": rf, **V.DCF_DEFAULTS}}


def make_record(ctx, r, run_at, d=None, price=None, cons=None, P=None, drop=False):
    """One ledger record (without chg), computed by V.dcf_case with ctx['P'] (DCF_DEFAULTS + rf).
    d / price / cons / P / drop override the row's inputs (used by the synthetic tests; drop=False = from ctx)."""
    d = r.get("dcf") if d is None else d
    price = r.get("price") if price is None else price
    P = ctx["P"] if P is None else P
    carded = r["code"] in ctx["cards"]
    cg = (ctx["cons"].get(r["code"]) or {}) if cons is None else cons
    cons_g = cg.get("g") if carded else None
    if drop is False:
        drop = (ctx.get("drops") or {}).get(r["code"]) if carded else None
    o = V.dcf_case(d, price, r["cyclical"], cons_g, r.get("rev_yoy"), P, drop)
    mb = o.get("mb") or {}
    ramp = mb.get("used") == "ramp"
    vps = o.get("vps") or {}
    rng = r.get("rng") or {}
    hk = ((r.get("inst") or {}).get("hkscc") or {}).get("s")
    base = vps.get("base")
    rec = {"date": ctx["date"], "run_at": run_at, "snap_file": ctx["snap_file"], "val_file": ctx["val_file"],
           "cards_file": ctx["cards_file"], "code": r["code"], "name": r.get("name"), "industry": r.get("industry"),
           "carded": carded, "cyclical": bool(r["cyclical"]), "price": price, "status": o["status"], "why": o.get("why"),
           "basis": mb.get("used"), "basis_why": mb.get("why"), "basis_block": mb.get("ttm_block") or mb.get("ramp_block"),
           "guard": [g["k"] for g in mb.get("guard") or []],
           "m_start": mb.get("start") if ramp else None, "start_src": mb.get("start_src") if ramp else None,
           "ramp_T": mb.get("T") if ramp else None, "ramp_N": mb.get("N") if ramp else None,
           "r0": (d or {}).get("r0"), "m0": o.get("m0"), "m_end": mb.get("m_end"),
           "m_avg3": mb.get("avg"), "m_ttm": mb.get("ttm"), "g0": o.get("g0"), "gsrc": o.get("gsrc"), "capped": o.get("capped"),
           "net_cash": o.get("nc"), "shares": (d or {}).get("sh"), "rf": P.get("rf"), "erp": P.get("erp"), "gT": o.get("gT"),
           "rampN": P.get("rampN"), "rampT": P.get("rampT"),
           "bear": _r(vps.get("bear")), "base": base, "bull": _r(vps.get("bull")),
           "implied_g": _r(o.get("rev_g")), "implied_g_status": o.get("rev"), "implied_m": o.get("m_req"),
           "implied_m_kind": ("T" if ramp else "m") if o.get("m_req") is not None else None,
           "cons_g": _r(cons_g), "cons_org": cg.get("org") if carded else None,
           "hi52": rng.get("hi52"), "lo52": rng.get("lo52"),
           "gap_base": _r(base / price - 1) if base is not None and price else None,
           "label": r.get("label"), "warn_n": r.get("warn_n"), "hkscc": hk}
    return rec


# ----------------------------------------------------------------------------- change + attribution
def _mpath(x):
    """Base-case margin of a record: the 10-year ramp path, or the constant m0 (same as dcf_case's M['base'])."""
    if x.get("basis") == "ramp":
        return V.margin_path(x["m_start"], x["rampT"] / 100, x["rampN"])
    return x["m0"]


def _vps(x):
    return V.dcf_engine(x["r0"], _mpath(x), x["g0"] / 100, x["gT"] / 100, (x["rf"] + x["erp"]) / 100, x["net_cash"], x["shares"])["vps"]


def _mreq(x):
    a = (x["r0"], x["g0"] / 100, x["gT"] / 100, (x["rf"] + x["erp"]) / 100, x["net_cash"], x["shares"], x["price"])
    if x.get("basis") == "ramp":
        return V.ramp_required(a[0], x["m_start"], x["rampN"], *a[1:])
    return V.margin_required(*a)


GROUPS = {"r0": ("r0",), "m0": ("basis", "m0", "m_start"), "g0": ("g0",), "ncsh": ("net_cash", "shares"),
          "par": ("rf", "erp", "gT", "rampN", "rampT"), "price": ("price",)}


def attribute(prev, cur, fn, order):
    """Sequential substitution: start from prev, switch the factor groups to cur in `order`. Returns (total, [(k, delta)])."""
    x = dict(prev)
    v0 = fn(x)
    out, last = [], v0
    for k, _ in order:
        for f in GROUPS[k]:
            x[f] = cur[f]
        v = fn(x)
        out.append((k, v - last))
        last = v
    return last - v0, v0, out


def _pct(a, b):
    return None if a is None or b is None or not b else (a / b - 1) * 100


def _d(a, b):
    return None if a is None or b is None else a - b


def sg(v, d=1, unit="%"):
    if v is None:
        return "—"
    s = f"{abs(v):.{d}f}"
    if float(s) == 0:
        return f"0{'.' + '0' * d if d else ''}{unit}"
    return ("+" if v > 0 else MINUS) + s + unit


def vs_txt(g):
    if g is None:
        return "—"
    return f"高于现价 {g * 100:.0f}%" if g >= 0 else f"低于现价 {-g * 100:.0f}%"


def pc(v, d=1):
    return "—" if v is None else f"{v * 100:.{d}f}%".replace("-", MINUS)


def _num(v):
    return "—" if v is None else f"{v:g}".replace("-", MINUS)


def _mdesc(x):
    """'−4.5%' or, under ramp, '−4.5%→15%（5 年）'."""
    if x.get("basis") == "ramp":
        return f"{pc(x.get('m_start'))}→{pc(x['rampT'] / 100, 0)}（{x['rampN']} 年）"
    return pc(x.get("m0"))


def first_txt(rec):
    return f"首次记录（{rec.get('date')}），下次更新后显示变化"


def _ig_txt(rec):
    s = rec.get("implied_g_status")
    if s == "ok" and rec.get("implied_g") is not None:
        return f"{rec['implied_g']:.0f}%".replace("-", MINUS)
    return {"below": f"<{MINUS}30%", "above": ">100%"}.get(s, "—")


def _driver_txt(k, prev, cur):
    if k == "r0":
        return f"营收基数 {sg(_pct(cur['r0'], prev['r0']))}"
    if k == "m0":
        pb, cb = prev.get("basis"), cur.get("basis")
        if pb != cb:
            return f"FCF 利润率口径 {BASIS_CN.get(pb, '—')} {_mdesc(prev)}→{BASIS_CN.get(cb, '—')} {_mdesc(cur)}"
        if cb == "ramp":
            return f"爬坡起点 {pc(prev['m0'])}→{pc(cur['m0'])}"
        return f"FCF 利润率 {sg((cur['m0'] - prev['m0']) * 100, 1, 'pt')}"
    if k == "g0":
        return f"起始增速 {sg(cur['g0'] - prev['g0'], 1, 'pt')}"
    if k == "ncsh":
        return "净现金与股本"
    if k == "par":
        ch = [f"{n} {_num(prev.get(f))}→{_num(cur.get(f))}" for f, n in (("rf", "rf"), ("erp", "ERP"), ("gT", "gT"), ("rampN", "爬坡 N"), ("rampT", "爬坡 T"))
              if prev.get(f) != cur.get(f)]
        return "参数 " + ("、".join(ch) if ch else "rf/ERP/gT")
    if k == "price":
        return f"股价 {sg(_pct(cur['price'], prev['price']))}"
    return k


def _drivers(parts, prev, cur, total_rel, unit):
    """'主要来自 X（贡献 +a）；其次 Y（贡献 +b）' from [(k, contribution in `unit`)]."""
    tot_abs = sum(abs(v) for _, v in parts)
    if tot_abs <= 1e-12:
        return "", None
    srt = sorted(parts, key=lambda kv: -abs(kv[1]))
    k1, v1 = srt[0]
    txt = f"主要来自 {_driver_txt(k1, prev, cur)}（贡献 {sg(v1, 1, unit)}）"
    if len(srt) > 1 and abs(srt[1][1]) / tot_abs > LEDGER_RULES["second_share"]:
        k2, v2 = srt[1]
        txt += f"；其次 {_driver_txt(k2, prev, cur)}（贡献 {sg(v2, 1, unit)}）"
    return txt, k1


def compute_chg(prev, cur):
    """Change of `cur` against `prev` (same code). Pure function of the two records."""
    c = {"prev_date": prev.get("date"), "prev_run": prev.get("run_at"), "prev_status": prev.get("status"),
         "price_pct": _r(_pct(cur.get("price"), prev.get("price")), 4),
         "base_pct": None, "gap_pp": None, "implied_g_pp": None, "implied_m_pp": None,
         "cons_g_pp": _r(_d(cur.get("cons_g"), prev.get("cons_g")), 4), "cons_org_d": _d(cur.get("cons_org"), prev.get("cons_org")),
         "label": None if prev.get("label") == cur.get("label") else [prev.get("label"), cur.get("label")],
         "warn_n_d": _d(cur.get("warn_n"), prev.get("warn_n")),
         "basis": None if prev.get("basis") == cur.get("basis") else [prev.get("basis"), cur.get("basis")],
         "attr_of": None, "attr": None, "total": None, "tag": None, "conclusion": ""}
    if cur.get("gap_base") is not None and prev.get("gap_base") is not None:
        c["gap_pp"] = _r((cur["gap_base"] - prev["gap_base"]) * 100, 4)
    if cur.get("implied_g_status") == "ok" and prev.get("implied_g_status") == "ok":
        c["implied_g_pp"] = _r(cur["implied_g"] - prev["implied_g"], 4)
    if cur.get("implied_m") is not None and prev.get("implied_m") is not None:
        c["implied_m_pp"] = _r((cur["implied_m"] - prev["implied_m"]) * 100, 4)
    ps, cs = prev.get("status"), cur.get("status")
    pp_ = c["price_pct"]
    tail = []
    if pp_ is not None:
        tail.append(f"股价 {sg(pp_)}")
    extra = []
    if c["cons_g_pp"] is not None and (abs(c["cons_g_pp"]) >= 0.05 or c["cons_org_d"]):
        extra.append(f"一致预期增速 {prev['cons_g']:.1f}%→{cur['cons_g']:.1f}%".replace("-", MINUS)
                     + (f"（机构 {prev.get('cons_org'):.0f}→{cur.get('cons_org'):.0f} 家）" if c["cons_org_d"] else ""))
    elif c["cons_org_d"]:
        extra.append(f"一致预期机构 {prev.get('cons_org'):.0f}→{cur.get('cons_org'):.0f} 家")
    if c["label"]:
        extra.append(f"标签 {c['label'][0]}→{c['label'][1]}")
    if c["warn_n_d"]:
        extra.append(f"⚠ {prev.get('warn_n')}→{cur.get('warn_n')}")
    if ps == "ok" and cs == "ok":
        total, v0, parts = attribute(prev, cur, _vps, ATTR_BASE)
        rel = v0 > 0
        c["attr_of"] = "base"
        c["total"] = total
        c["attr"] = [[k, v, (v / v0 * 100) if rel else None] for k, v in parts]
        bp = (total / v0 * 100) if rel else None
        c["base_pct"] = bp          # unrounded so that the attribution percentages sum to it exactly
        if rel:
            drv, k1 = _drivers([(k, v / v0 * 100) for k, v in parts], prev, cur, bp, "%")
        else:
            drv, k1 = _drivers(parts, prev, cur, None, " 元")
        head = f"基准估值 {sg(bp) if rel else sg(total, 2, ' 元')}" + (f"（{drv}）" if drv else "")
        if pp_ is not None and bp is not None and abs(pp_) >= 0.05 and abs(bp) >= 0.05:
            tail[-1] += "，与估值同向" if (pp_ > 0) == (bp > 0) else "，与估值反向"
        mid = []
        if c["gap_pp"] is not None:
            mid.append(f"基准相对现价从{vs_txt(prev['gap_base'])}变为{vs_txt(cur['gap_base'])}")
        mid.append(f"隐含增速 {_ig_txt(prev)}→{_ig_txt(cur)}")
        flat = LEDGER_RULES["flat_pct"]
        if bp is not None and abs(bp) < flat:
            c["tag"] = "价格驱动"           # the value barely moved: any change of the gap comes from the price
        else:
            c["tag"] = "参数驱动" if k1 == "par" else "基本面驱动"
        c["conclusion"] = "；".join([head] + tail + mid + extra) + "。"
    elif ps == "negfcf" and cs == "negfcf":
        total, v0, parts = attribute(prev, cur, _mreq, ATTR_MREQ)
        c["attr_of"] = "m_req"
        c["total"] = total
        c["attr"] = [[k, v, v * 100] for k, v in parts]   # contributions in pt of revenue
        drv, k1 = _drivers([(k, v * 100) for k, v in parts], prev, cur, None, "pt")
        req = "需要的稳态 FCF 利润率" if cur.get("implied_m_kind") == "T" else "需要的 FCF 利润率"
        head = (f"基准 FCF 利润率仍 ≤ 0（{_mdesc(prev)}→{_mdesc(cur)}）；{req} {pc(prev['implied_m'])}→{pc(cur['implied_m'])}"
                f"（{sg(total * 100, 1, 'pt')}" + (f"，{drv}" if drv else "") + "）")
        c["tag"] = "参数驱动" if k1 == "par" else "基本面驱动" if k1 not in (None, "price") else "价格驱动"
        c["conclusion"] = "；".join([head] + tail + extra) + "。"
    else:
        if ps == cs:
            head = f"仍不计算（{cur.get('why') or '—'}）"
        else:
            head = f"状态 {STATUS_CN.get(ps, ps)} → {STATUS_CN.get(cs, cs)}"
            if cur.get("m0") is not None or prev.get("m0") is not None:
                pb, cb = prev.get("basis"), cur.get("basis")
                head += f"（FCF 利润率 {_mdesc(prev)}→{_mdesc(cur)}" + (
                    f"，口径 {BASIS_CN.get(pb, '—')}→{BASIS_CN.get(cb, '—')}" if pb != cb else
                    f"，口径 {BASIS_CN.get(cb, '—')}" if cb else "") + "）"
            if cs == "ok":
                head += f"；基准估值 {cur['base']:.2f} 元，{vs_txt(cur.get('gap_base'))}"
            c["tag"] = "状态变化"
        c["conclusion"] = "；".join([head] + tail + extra) + "。"
    return c


# ----------------------------------------------------------------------------- rule-based tags (display only)
# Mechanical tags from fixed thresholds. They describe the model output and the data change; they are not buy / sell
# signals; the method is not backtested. The page (templates/cn_dashboard.html, lgTags) mirrors tags() line by line on the
# same compact record tag_input(); both must give identical lists (checked by the parity script / smoke).
TAG_RULES = {
    "chg_pct": {"big": 15.0, "mid": 5.0, "flat": 2.0},      # base value change % (ok -> ok)
    "mreq_pt": {"big": 5.0, "mid": 2.0},                    # required FCF margin change, pt (negfcf -> negfcf)
    "gap": {"far": 0.5, "near": 0.15},                      # gap_base = base / price - 1 (status ok)
    "combo": {"fund_up": 5.0, "price_flat": 2.0, "price_lead": 10.0, "base_flat": 2.0, "fund_dn": 5.0,
              "fund_keys": ["r0", "m0", "g0"], "cons_pt": 3.0, "gap_pp": 10.0},
    "note": "标签按固定阈值机械生成，描述模型输出和数据变化，不是买卖信号；方法未回测。",
}


def _dom(attr):
    """Key of the largest |contribution| (first one on ties), None without attribution."""
    if not attr:
        return None
    best, bv = None, -1.0
    for a in attr:
        v = a[2] if a[2] is not None else a[1]
        if v is not None and abs(v) > bv:
            best, bv = a[0], abs(v)
    return best


def tag_input(x):
    """Compact record used by tags() here and lgTags() on the page (None fields omitted)."""
    ch = x.get("chg")
    t = {"s": x.get("status"), "p": x.get("price"), "lo": _r(x.get("bear"), 4), "b": _r(x.get("base"), 4), "hi": _r(x.get("bull"), 4),
         "gp": _r(x.get("gap_base"), 5), "ig": _r(x.get("implied_g"), 3), "igs": x.get("implied_g_status"),
         "mr": _r(x.get("implied_m"), 6), "mk": x.get("implied_m_kind"), "mb": x.get("basis"),
         "gd": "".join(x.get("guard") or []) or None, "cp": 1 if x.get("capped") else None, "cy": 1 if x.get("cyclical") else None}
    if ch:
        t.update({"ch": 1, "bp": _r(ch.get("base_pct"), 4), "pp": _r(ch.get("price_pct"), 4), "gpp": _r(ch.get("gap_pp"), 4),
                  "mpp": _r(ch.get("implied_m_pp"), 4), "cgp": _r(ch.get("cons_g_pp"), 4),
                  "lbc": 1 if ch.get("label") else None, "wnd": ch.get("warn_n_d") or None, "bsc": 1 if ch.get("basis") else None,
                  "ak": _dom(ch.get("attr")) if ch.get("attr_of") == "base" else None})
    return {k: v for k, v in t.items() if v is not None}


def tags(t, R=TAG_RULES):
    """[[group, text]] for a compact record; groups: chg (变化幅度), pos (估值位置), cmb (组合信号), note (注意)."""
    out = []
    g = t.get
    bp, pp, s = g("bp"), g("pp"), g("s")
    if g("ch"):
        c = R["chg_pct"]
        if bp is not None:
            out.append(["chg", "大幅上调" if bp >= c["big"] else "上调" if bp >= c["mid"] else "大幅下调" if bp <= -c["big"]
                        else "下调" if bp <= -c["mid"] else "小幅变化" if abs(bp) >= c["flat"] else "基本不变"])
        elif g("mpp") is not None:
            m, q = g("mpp"), R["mreq_pt"]
            if m >= q["big"]:
                out.append(["chg", "需要利润率 大幅上升"])
            elif m >= q["mid"]:
                out.append(["chg", "需要利润率 上升"])
            elif m <= -q["big"]:
                out.append(["chg", "需要利润率 大幅下降"])
            elif m <= -q["mid"]:
                out.append(["chg", "需要利润率 下降"])
    if s == "ok" and g("gp") is not None:
        gp, q = g("gp"), R["gap"]
        out.append(["pos", "现价远低于基准" if gp >= q["far"] else "现价低于基准" if gp >= q["near"] else "接近基准" if gp > -q["near"]
                    else "现价高于基准" if gp > -q["far"] else "现价远高于基准"])
        if g("p") is not None and g("lo") is not None and g("p") < g("lo"):
            out.append(["pos", "现价低于熊情景"])
        if g("p") is not None and g("hi") is not None and g("p") > g("hi"):
            out.append(["pos", "现价高于牛情景"])
    if g("ch"):
        c = R["combo"]
        if bp is not None and bp >= c["fund_up"] and pp is not None and pp <= c["price_flat"]:
            out.append(["cmb", "基本面改善·价格未跟"])
        if pp is not None and pp >= c["price_lead"] and bp is not None and bp <= c["base_flat"]:
            out.append(["cmb", "价格先行"])
        if bp is not None and bp <= -c["fund_dn"] and g("ak") in c["fund_keys"]:
            out.append(["cmb", "基本面转弱"])
        if g("cgp") is not None and g("cgp") >= c["cons_pt"]:
            out.append(["cmb", "预期上修"])
        if g("cgp") is not None and g("cgp") <= -c["cons_pt"]:
            out.append(["cmb", "预期下修"])
        if g("gp") is not None and g("gpp") is not None:
            cur = g("gp") * 100
            d = abs(cur) - abs(cur - g("gpp"))
            if d <= -c["gap_pp"]:
                out.append(["cmb", "差距收窄"])
            elif d >= c["gap_pp"]:
                out.append(["cmb", "差距扩大"])
        if g("bsc"):
            out.append(["cmb", "口径切换"])
        if g("lbc"):
            out.append(["cmb", "标签变化"])
        if g("wnd"):
            out.append(["cmb", "风险门禁变化"])
    if g("gd"):
        out.append(["note", "一次性收入"])
    if g("cp"):
        out.append(["note", "增速截断"])
    if g("mb") == "ramp":
        out.append(["note", "爬坡假设"])
    if g("cy"):
        out.append(["note", "周期"])
    if s == "ok" and g("b") is not None and g("b") <= 0:
        out.append(["note", "估值≤0"])
    return out


# ----------------------------------------------------------------------------- file
def load(path=LEDGER_FILE):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                log(f"WARNING: {os.path.basename(path)} line {i} unreadable; skipped")
    return out


def run(path=LEDGER_FILE, ctx=None, run_at=None):
    """Append one record per screen code (default parameters). Returns (appended, skipped, ctx)."""
    ctx = ctx or gather()
    if not ctx["val_file"]:
        log("ERROR: no data/cn/valuation_*.json (run `python -m cn valuation` first)")
        return None
    run_at = run_at or dt.datetime.now(em.CST).strftime("%Y-%m-%d %H:%M")
    old = load(path)
    keys = {(x["code"], x.get("snap_file"), x.get("val_file")) for x in old}
    last = {}
    for x in old:
        last[x["code"]] = x
    new, skipped = [], 0
    for r in ctx["rows"]:
        k = (r["code"], ctx["snap_file"], ctx["val_file"])
        if k in keys:
            skipped += 1
            continue
        rec = make_record(ctx, r, run_at)
        prev = last.get(r["code"])
        rec["chg"] = compute_chg(prev, rec) if prev else None
        rec["conclusion"] = rec["chg"]["conclusion"] if rec["chg"] else first_txt(rec)
        rec["tags"] = [t[1] for t in tags(tag_input(rec))]
        new.append(rec)
    if new:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in new))
            f.flush()
            os.fsync(f.fileno())
    st = {}
    for x in new:
        st[x["status"]] = st.get(x["status"], 0) + 1
    log(f"ledger {os.path.relpath(path, ROOT)}: appended={len(new)} skipped(existing)={skipped} with_change={sum(1 for x in new if x['chg'])} "
        f"status={st} snap={ctx['snap_file']} val={ctx['val_file']} date={ctx['date']}")
    return len(new), skipped, ctx


# ----------------------------------------------------------------------------- page payload
def _runs(recs):
    seen, out = set(), []
    for x in recs:
        k = (x.get("snap_file"), x.get("val_file"))
        if k not in seen:
            seen.add(k)
            out.append({"snap": k[0], "val": k[1], "date": x.get("date"), "run_at": x.get("run_at"), "n": 0})
        next(o for o in out if (o["snap"], o["val"]) == k)["n"] += 1
    return out


def _attr_c(ch):
    return None if not ch or not ch.get("attr") else [[a[0], _r(a[1], 6), _r(a[2], 4)] for a in ch["attr"]]


def _crec(x):
    """Compact record for the 40 cards' history."""
    ch = x.get("chg")
    return {"d": x["date"], "ra": x.get("run_at"), "p": x.get("price"), "s": x["status"], "w": x.get("why"),
            "lo": _r(x.get("bear"), 4), "b": _r(x.get("base"), 4), "hi": _r(x.get("bull"), 4), "gp": _r(x.get("gap_base"), 5),
            "ig": _r(x.get("implied_g"), 3), "igs": x.get("implied_g_status"), "mr": _r(x.get("implied_m"), 6),
            "m": _r(x.get("m0"), 6), "mb": x.get("basis"), "mw": x.get("basis_why"), "g": _r(x.get("g0"), 3), "gs": x.get("gsrc"),
            "mT": _r(x.get("ramp_T"), 6), "mN": x.get("ramp_N"), "gd": "".join(x.get("guard") or []) or None,
            "mk": x.get("implied_m_kind"),
            "c": ch.get("conclusion") if ch else None, "t": ch.get("tag") if ch else None, "ao": ch.get("attr_of") if ch else None,
            "a": _attr_c(ch), "bp": ch.get("base_pct") if ch else None, "pp": ch.get("price_pct") if ch else None,
            "gpp": ch.get("gap_pp") if ch else None, "igp": ch.get("implied_g_pp") if ch else None, "x": tag_input(x)}


def page_payload(card_codes, screen_codes, snap_file, path=LEDGER_FILE):
    """Compact ledger for the page: full history (last page_keep records) for the cards, latest change for every screen row."""
    recs = load(path)
    if not recs:
        return {"file": None}
    runs = _runs(recs)
    by = {}
    for x in recs:
        by.setdefault(x["code"], []).append(x)
    keep = LEDGER_RULES["page_keep"]
    cards = {c: [_crec(x) for x in by[c][-keep:]] for c in card_codes if c in by}
    allc, rec = {}, {}
    for c in screen_codes:
        if c not in by:
            continue
        x = by[c][-1]
        rec[c] = tag_input(x)
        ch = x.get("chg")
        if not ch:
            allc[c] = by[c][0]["date"]          # first record only: its date
        else:
            allc[c] = [ch.get("tag"), ch.get("base_pct"), ch.get("gap_pp"), ch.get("implied_g_pp"), ch.get("price_pct"),
                       _attr_c(ch), [ch.get("prev_status"), x["status"]], ch.get("conclusion"), ch.get("prev_date"),
                       ch.get("attr_of"), ch.get("implied_m_pp")]
    lr = runs[-1]
    return {"file": os.path.relpath(path, ROOT).replace("\\", "/"), "runs": runs, "n_records": len(recs), "latest": lr,
            "prev": runs[-2] if len(runs) > 1 else None, "matches_snap": lr["snap"] == snap_file, "rules": LEDGER_RULES,
            "order_base": [a[0] for a in ATTR_BASE], "order_mreq": [a[0] for a in ATTR_MREQ],
            "names": dict(ATTR_BASE + ATTR_MREQ), "cards": cards, "all": allc, "rec": rec, "tag_rules": TAG_RULES}
