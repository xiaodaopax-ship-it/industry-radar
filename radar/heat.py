"""Market-wide industry heat map for US filers.

Groups every SEC filer with comparable quarterly revenue by SIC major group (2-digit) and
industry (4-digit), and measures where growth is broad-based and accelerating. This is the
discovery layer: it shows which industries are heating up before anyone configures a sector.

    python -m radar heat     fetch (SEC frames + submissions, cached) and save data/heat_<ts>.json
"""
import datetime as dt
import glob
import json
import os
import statistics as st

from . import us
from .paths import DATA

# SIC major groups (first two digits), Chinese names. Source: SEC / OSHA SIC manual.
MAJOR = {
    "01": "农作物", "02": "畜牧", "07": "农业服务", "08": "林业", "09": "渔业", "10": "金属采矿", "12": "煤炭", "13": "石油天然气开采",
    "14": "非金属矿", "15": "房屋建筑", "16": "重型工程建筑", "17": "专业建筑承包", "20": "食品", "21": "烟草", "22": "纺织", "23": "服装",
    "24": "木材", "25": "家具", "26": "造纸", "27": "印刷出版", "28": "化工与制药", "29": "石油炼化", "30": "橡胶塑料", "31": "皮革",
    "32": "建材玻璃", "33": "基础金属", "34": "金属制品", "35": "工业机械与计算机设备", "36": "电子与电气设备", "37": "交通运输设备",
    "38": "仪器与医疗器械", "39": "其他制造", "40": "铁路", "41": "公共交通", "42": "货运与仓储", "44": "水运", "45": "航空运输",
    "46": "管道运输", "47": "运输服务", "48": "通信", "49": "电力燃气与公用事业", "50": "批发（耐用品）", "51": "批发（非耐用品）",
    "52": "建材与园艺零售", "53": "综合零售", "54": "食品零售", "55": "汽车经销与加油站", "56": "服装零售", "57": "家居零售",
    "58": "餐饮", "59": "其他零售", "60": "银行", "61": "非银信贷", "62": "证券与经纪", "63": "保险", "64": "保险代理", "65": "房地产",
    "67": "控股与投资", "70": "酒店住宿", "72": "个人服务", "73": "商业服务与软件", "75": "汽车服务", "76": "维修服务", "78": "影视",
    "79": "娱乐休闲", "80": "医疗服务", "81": "法律服务", "82": "教育", "83": "社会服务", "87": "工程与研究服务", "89": "其他服务",
    "99": "未分类",
}


# ---- configuration verdict (rules shown on the dashboard; thresholds are judgement calls, not back-tested)
# Major groups whose revenue moves mainly with commodity / freight prices: high growth there is usually a price cycle,
# and the Lynch-style method (growth + gross margin + PEG) misreads cycle peaks as cheap growth.
CYCLICAL_MAJOR = {"10": "金属价格", "12": "煤价", "13": "油气价格", "29": "油价与炼油价差", "33": "金属价格", "44": "运价"}
FINANCIAL_MAJOR = {"60", "61", "62", "63", "64", "67"}   # no gross margin; needs the financial evaluation profile
VERDICT = {"breadth_pct20_min": 40, "breadth_accel_min": 50, "price_growth_min": 20, "price_gm_chg_max": 0.5,
           "covered_pct_min": 30}


def _verdict(code, a, cov_pct):
    """One label + reasons for a SIC major group (code = 2 digits) or industry (major code passed in)."""
    why = []
    broad = (a["pct_20"] or 0) >= VERDICT["breadth_pct20_min"] and (a["pct_accel"] or 0) >= VERDICT["breadth_accel_min"]
    why.append(f"增速 ≥20% 占比 {a['pct_20']}%、加速占比 {a['pct_accel']}%"
               + ("，增长普遍" if broad else f"，未同时达到 {VERDICT['breadth_pct20_min']}% / {VERDICT['breadth_accel_min']}%"))
    price = (a["med_growth"] or 0) >= VERDICT["price_growth_min"] and a["med_gm_chg"] is not None and a["med_gm_chg"] <= VERDICT["price_gm_chg_max"]
    if price:
        why.append(f"营收中位数 {a['med_growth']:+.0f}% 但毛利率中位数只变 {a['med_gm_chg']:+.1f}pt，可能是价格传导而不是量的增长")
    if cov_pct >= VERDICT["covered_pct_min"]:
        label = "已配置"
    elif code in FINANCIAL_MAJOR:
        label = "需金融类口径"
        why.append("金融类公司没有毛利，要用税前利润率、ROE、市净率评估")
    elif code in CYCLICAL_MAJOR:
        label = "周期 · 不配置"
        why.append(f"收入主要随{CYCLICAL_MAJOR[code]}变化，周期高点时 PE 最低、PEG 显得最便宜，现有方法会误判")
    elif broad and not price:
        label = "候选"
    elif broad and price:
        label = "待核实：可能是价格驱动"
    else:
        label = "增长不普遍"
    return {"label": label, "why": why, "cov_pct": round(cov_pct)}


def _agg(rows):
    yo = [r["yoy"] for r in rows]
    acc = [r for r in rows if r.get("yoy_prev") is not None]
    gm = [r["gm"] - r["gm_ly"] for r in rows if r.get("gm") is not None and r.get("gm_ly") is not None]
    op = [r["opm"] - r["opm_ly"] for r in rows if r.get("opm") is not None and r.get("opm_ly") is not None]
    q0, ly = sum(r["rev_q"] for r in rows), sum(r["rev_q_ly"] for r in rows)
    return {
        "n": len(rows), "rev_q": q0,
        "wgrowth": round((q0 / ly - 1) * 100, 1) if ly else None,          # revenue-weighted (dominated by big companies)
        "med_growth": round(st.median(yo), 1) if yo else None,               # typical company
        "pct_20": round(sum(1 for v in yo if v >= 20) / len(yo) * 100) if yo else None,
        "pct_accel": round(sum(1 for r in acc if r["yoy"] > r["yoy_prev"]) / len(acc) * 100) if acc else None,
        "med_gm_chg": round(st.median(gm), 1) if gm else None,
        "med_opm_chg": round(st.median(op), 1) if op else None,
    }


def collect(log=print):
    P, rows = us.fundamentals()
    log(f"heat: {len(rows)} filers; fetching SIC codes (cached submissions) ...")
    out = []
    for i, r in enumerate(rows):
        sub = us.submissions(r["cik"])
        if not sub.get("sic"):
            continue
        tk = sub["tickers"][0] if sub.get("tickers") else None
        out.append({"cik": r["cik"], "ticker": tk, "name": r["name"], "sic": sub["sic"], "sic_desc": sub.get("sic_desc"),
                    "yoy": r["yoy"], "yoy_prev": r["yoy_prev"], "rev_q": r["rev_q"], "rev_q_ly": r["rev_q_ly"], "rev_ttm": r["rev_ttm"],
                    "gm": r["gm"], "gm_ly": r["gm_ly"], "opm": r["opm"], "opm_ly": r["opm_ly"]})
        if (i + 1) % 250 == 0:
            log(f"  {i + 1}/{len(rows)}")
    path = os.path.join(DATA, f"heat_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "periods": P, "rows": out}, f, ensure_ascii=False)
    log(f"heat snapshot saved: {path} ({len(out)} filers with SIC)")


def latest():
    files = sorted(glob.glob(os.path.join(DATA, "heat_*.json")), key=os.path.getmtime)
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def summarize(snap, sector_sics, min_n=8):
    """Major groups and industries with at least min_n companies; extreme growth values are capped so one outlier cannot dominate."""
    if not snap:
        return None
    rows = [dict(r, yoy=max(-90.0, min(r["yoy"], 300.0))) for r in snap["rows"] if r["rev_q_ly"] >= 5e6]
    groups, inds = {}, {}
    for r in rows:
        groups.setdefault(f"{r['sic']:04d}"[:2], []).append(r)
        inds.setdefault(r["sic"], []).append(r)
    covered = lambda sics: sorted({sid for sid, ss in sector_sics.items() if any(s in ss for s in sics)})
    all_sics = set().union(*sector_sics.values()) if sector_sics else set()
    major = []
    for g, rs in groups.items():
        if len(rs) < min_n:
            continue
        sics = sorted({r["sic"] for r in rs})
        a = _agg(rs)
        cov_pct = 100 * sum(1 for r in rs if r["sic"] in all_sics) / len(rs)   # share of the group's companies, not "any overlap"
        major.append({"code": g, "name": MAJOR.get(g, g), **a, "covered_by": covered(sics), "verdict": _verdict(g, a, cov_pct),
                      "top": [{"ticker": r["ticker"], "name": r["name"], "yoy": r["yoy"]} for r in sorted(rs, key=lambda r: -r["rev_q"])[:5]]})
    industry = []
    for sic, rs in inds.items():
        if len(rs) < min_n:
            continue
        a = _agg(rs)
        industry.append({"sic": sic, "desc": rs[0]["sic_desc"], "major": f"{sic:04d}"[:2], **a, "covered_by": covered([sic]),
                         "verdict": _verdict(f"{sic:04d}"[:2], a, 100.0 if sic in all_sics else 0.0),
                         "fast": [{"ticker": r["ticker"], "name": r["name"], "yoy": r["yoy"]} for r in sorted(rs, key=lambda r: -r["yoy"])[:5] if r["yoy"] >= 20]})
    return {"fetched_at": snap["fetched_at"], "period": snap["periods"]["q0"], "n": len(rows), "min_n": min_n,
            "verdict_rules": VERDICT, "cyclical_major": CYCLICAL_MAJOR, "financial_major": sorted(FINANCIAL_MAJOR),
            "market": _agg(rows), "major": sorted(major, key=lambda x: -x["rev_q"]), "industry": sorted(industry, key=lambda x: -(x["pct_20"] or 0))}
