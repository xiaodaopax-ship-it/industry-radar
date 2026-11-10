"""Event study: do 北向 / 社保 increases in the top-10 float holders predict the next 3 / 6 months? (research only)

    python -m cn instbt   -> data/cn/instbt_<ts>.json (English stdout summary)

Nothing here feeds any score, label, rank or page. Point-in-time design (no look-ahead):
  signal period P = Q1 / H1 / Q3 quarter ends 2021-03-31 .. 2026-03-31 (annual reports share the 04-30 deadline with Q1
  and are only used as a step in the holder history);
  signal date = statutory deadline of P (cn/holders.deadline), entry = close of the first trading day AFTER it, so every
  company's report is public at entry;
  forward return = close at entry + 91 / 182 calendar days (last trading day on or before), dividends and 送转 adjusted
  with RPT_SHAREBONUS_DET (same formula as cn/ranges.derived_qfq_closes); 配股 not adjusted;
  prior return = entry - 91 days -> entry (is the signal chasing past winners?).
Holder steps between consecutive quarter ends a -> b (company disclosed a top-10 float list at both):
  up = new in the top 10 at b, or shares at b > shares at a x 送转 factor; down = dropped out, or fewer shares.
  streak at P = consecutive up steps ending at P (1 = this quarter, 2 = two quarters in a row, ...).
Universe at entry: price at entry, top-10 list disclosed at P and the quarter before, name without "ST".
Benchmarks: equal-weight mean of the universe (market) and of the same total-mcap quintile at entry (size), because
北向 prefers large caps. Excess = stock return - benchmark.
Pools (point-in-time proxies, the real 新兴筛选 / 成长 × 低估 need data we do not have for past dates):
  growth = YTD revenue YoY >= 20% and revenue >= 1亿 per quarter at P (the 新兴筛选 entry rule, cn/build.RULES);
  growth x cheap = growth and 0 < PE(TTM) <= the median positive PE(TTM) of its Eastmoney industry at entry.
Sources (Eastmoney datacenter-web, market-wide paged queries, cached 30 days): RPT_F10_EH_FREEHOLDERS (per rule x
END_DATE, plus HOLDER_RANK=1 for disclosure coverage), RPT_VALUEANALYSIS_DET (per TRADE_DATE), RPT_LICO_FN_CPD (per
REPORTDATE), RPT_SHAREBONUS_DET. stdout is English only.
"""
import datetime as dt
import json
import math
import os
import statistics

from . import em
from .holders import HOLDER_RULES, deadline, match_rule, bonus_factor
from .ranges import bonus_events

ROOT = em.ROOT
DATA_DIR = os.path.join(ROOT, "data", "cn")
BT_RULES = {"first": "2021-03-31", "last": "2026-03-31", "horizons": (91, 182), "pre_days": 91, "keys": ("hkscc", "ssf"),
            "growth_yoy_min": 20.0, "revenue_floor_per_quarter": 1e8, "flat_rel": 1e-4, "ttl": 30.0, "quintiles": 5}


def log(msg):
    print(msg, flush=True)


def qends(a, b):
    """Quarter ends in [a, b] ascending (ISO strings)."""
    out, y = [], int(a[:4])
    while True:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            s = f"{y}-{m:02d}-{d:02d}"
            if s > b:
                return out
            if s >= a:
                out.append(s)
        y += 1


def prev_q(p, n=1):
    d = dt.date.fromisoformat(p)
    y, m = d.year, d.month - 3 * n
    while m <= 0:
        m, y = m + 12, y - 1
    return dt.date(y, m, 31 if m in (3, 12) else 30).isoformat()


# ----------------------------------------------------------------------------- data
def trade_date(d, after=False):
    """Last trading day <= d (or first > d when after=True), from 000001's daily valuation rows; None if none."""
    f = f"(SECURITY_CODE=\"000001\")(TRADE_DATE{'>' if after else '<='}'{d}')"
    rows, _ = em.dc_report("RPT_VALUEANALYSIS_DET", "TRADE_DATE", f, "TRADE_DATE", "1" if after else "-1", page_size=1,
                           max_pages=1, ttl=BT_RULES["ttl"], label=f"instbt/tdate {d}", warn_truncated=False)
    return em.day(rows[0].get("TRADE_DATE")) if rows else None


def prices(d):
    """{code: {p, mcap, pe, ind, name}} for one trading date (all A shares)."""
    rows, ok = em.dc_report("RPT_VALUEANALYSIS_DET", "SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,TOTAL_MARKET_CAP,PE_TTM,BOARD_NAME",
                            f"(TRADE_DATE='{d}')", "SECURITY_CODE", "1", ttl=BT_RULES["ttl"], label=f"instbt/px {d}")
    out = {}
    for r in rows:
        c = r.get("SECURITY_CODE")
        p = em.num(r.get("CLOSE_PRICE"))
        if em.is_a_code(c) and p:
            out[c] = {"p": p, "mcap": em.num(r.get("TOTAL_MARKET_CAP")), "pe": em.num(r.get("PE_TTM")),
                      "ind": r.get("BOARD_NAME"), "name": r.get("SECURITY_NAME_ABBR") or ""}
    return out, ok


def holders_period(p):
    """(reported codes, {key: {code: shares}}) for one END_DATE; several rows of one rule are summed."""
    rows, ok1 = em.dc_report("RPT_F10_EH_FREEHOLDERS", "SECURITY_CODE", f"(END_DATE='{p}')(HOLDER_RANK=1)", "SECURITY_CODE", "1",
                             ttl=BT_RULES["ttl"], label=f"instbt/reported {p}")
    rep = {r["SECURITY_CODE"] for r in rows if em.is_a_code(r.get("SECURITY_CODE"))}
    out, ok = {}, ok1
    for k in BT_RULES["keys"]:
        rule = HOLDER_RULES[k]
        rows, okk = em.dc_report("RPT_F10_EH_FREEHOLDERS", "SECURITY_CODE,HOLDER_NAME,HOLDER_RANK,HOLD_NUM,SHARES_TYPE",
                                 f"{rule['query']}(END_DATE='{p}')", "SECURITY_CODE,HOLDER_RANK", "1,1", ttl=BT_RULES["ttl"],
                                 label=f"instbt/{k} {p}")
        ok = ok and okk
        m, seen = {}, set()
        for r in rows:
            c, nm = r.get("SECURITY_CODE"), r.get("HOLDER_NAME")
            if not em.is_a_code(c) or "A股" not in str(r.get("SHARES_TYPE") or "A股") or not match_rule(rule, nm):
                continue
            dk = (c, nm, r.get("HOLDER_RANK"))
            if dk in seen:
                continue
            seen.add(dk)
            rep.add(c)
            m[c] = m.get(c, 0) + (em.num(r.get("HOLD_NUM")) or 0)
        out[k] = m
    return rep, out, ok


def growth_period(p):
    """{code: (revenue_yoy_pct, revenue, notice_date)} from 业绩报表 at report date p."""
    rows, ok = em.dc_report("RPT_LICO_FN_CPD", "SECURITY_CODE,REPORTDATE,NOTICE_DATE,YSTZ,TOTAL_OPERATE_INCOME",
                            f"(REPORTDATE='{p}')(SECURITY_TYPE_CODE=\"058001001\")", "SECURITY_CODE", "1", ttl=BT_RULES["ttl"],
                            label=f"instbt/cpd {p}")
    out = {}
    for r in rows:
        c = r.get("SECURITY_CODE")
        if em.is_a_code(c):
            out[c] = (em.num(r.get("YSTZ")), em.num(r.get("TOTAL_OPERATE_INCOME")), em.day(r.get("NOTICE_DATE")))
    return out, ok


# ----------------------------------------------------------------------------- pure helpers
def step(sa, sb, rep_a, rep_b, factor):
    """'up' | 'down' | 'flat' | None for one holder between two periods (None = not measurable / not involved)."""
    if not (rep_a and rep_b):
        return None
    if sa is None and sb is None:
        return None
    if sa is None:
        return "new"
    if sb is None:
        return "out"
    a = sa * factor
    if abs(sb - a) <= BT_RULES["flat_rel"] * max(a, sb, 1):
        return "flat"
    return "add" if sb > a else "cut"


def streak(steps):
    """Consecutive up steps (new / add) at the end of `steps` (oldest first)."""
    n = 0
    for s in reversed(steps):
        if s in ("new", "add"):
            n += 1
        else:
            break
    return n


def adj_return(p0, d0, p1, d1, events):
    """Total return d0 -> d1 with dividends / 送转 between them (ex-date in (d0, d1])."""
    for e in sorted(events or [], key=lambda x: x["ex_date"]):
        if d0 < e["ex_date"] <= d1:
            p0 = (p0 - (e.get("cash_per10") or 0) / 10) / (1 + (e.get("bonus_it_per10") or 0) / 10)
    return p1 / p0 - 1 if p0 and p0 > 0 else None


def tstat(xs):
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return None if sd == 0 else statistics.mean(xs) / (sd / math.sqrt(len(xs)))


# ----------------------------------------------------------------------------- study
def run():
    R = BT_RULES
    t0 = dt.datetime.now(em.CST)
    sig = [p for p in qends(R["first"], R["last"]) if p[5:7] in ("03", "06", "09")]
    hper = qends(prev_q(sig[0], 3), sig[-1])
    log(f"instbt: signal periods {len(sig)} ({sig[0]}..{sig[-1]}), holder periods {len(hper)}")

    H, REP = {}, {}
    for p in hper:
        REP[p], H[p], ok = holders_period(p)
        log(f"  holders {p}: reported={len(REP[p])} " + " ".join(f"{k}={len(H[p][k])}" for k in R["keys"]) + f" ok={ok}")
    last_px = trade_date(t0.date().isoformat())
    events, ev_ok = bonus_events(hper[0], last_px, R["ttl"])
    log(f"  dividend / bonus events {hper[0]}..{last_px}: {sum(len(v) for v in events.values())} rows ok={ev_ok}")

    px_cache = {}

    def px(d):
        if d not in px_cache:
            px_cache[d], ok = prices(d)
            log(f"  prices {d}: {len(px_cache[d])} ok={ok}")
        return px_cache[d]

    GROUPS = ["all", "hk_up1", "hk_new", "hk_add", "hk_up2", "hk_up3", "hk_down1", "ss_up1", "ss_new", "ss_add", "ss_up2",
              "ss_up3", "ss_down1", "any_up2", "g", "g_nosig", "g_hk_up1", "g_hk_up2", "g_ss_up1", "g_ss_up2", "g_any_up2",
              "gv", "gv_nosig", "gv_any_up1", "gv_any_up2", "nog_any_up2"]
    obs = {g: [] for g in GROUPS}          # stock-level rows: (period, code, {h: (ex_mkt, ex_size)}, pre_ex)
    per_date = []
    for P in sig:
        dl = deadline(P).isoformat()
        E = trade_date(dl, after=True)
        exits = {h: trade_date((dt.date.fromisoformat(E) + dt.timedelta(days=h)).isoformat()) for h in R["horizons"]}
        exits = {h: d for h, d in exits.items() if d and (dt.date.fromisoformat(E) + dt.timedelta(days=h)).isoformat() <= last_px}
        pre = trade_date((dt.date.fromisoformat(E) - dt.timedelta(days=R["pre_days"])).isoformat())
        p0, ppre = px(E), px(pre)
        p1 = {h: px(d) for h, d in exits.items()}
        steps = [prev_q(P, 3), prev_q(P, 2), prev_q(P, 1), P]
        G, _ = growth_period(P)
        nq = int(P[5:7]) // 3
        # universe and per-stock signal flags
        U, flags = [], {}
        for c, q in p0.items():
            if "ST" in q["name"].upper() or c not in REP[P] or c not in REP[prev_q(P)]:
                continue
            f = {}
            for k in R["keys"]:
                ss = []
                for a, b in zip(steps, steps[1:]):
                    fac = bonus_factor(events.get(c), a, b)
                    ss.append(step(H[a][k].get(c), H[b][k].get(c), c in REP[a], c in REP[b], fac))
                f[k] = (ss[-1], streak(ss))
            g = G.get(c)
            f["g"] = bool(g and g[0] is not None and g[0] >= R["growth_yoy_min"] and (g[1] or 0) >= R["revenue_floor_per_quarter"] * nq
                          and (g[2] or "9999") <= E)
            U.append(c)
            flags[c] = f
        # industry median positive PE at entry, size quintiles
        ind_pe = {}
        for c in U:
            q = p0[c]
            if q["pe"] and q["pe"] > 0:
                ind_pe.setdefault(q["ind"], []).append(q["pe"])
        ind_med = {k: statistics.median(v) for k, v in ind_pe.items() if len(v) >= 5}
        mc = sorted((p0[c]["mcap"] or 0, c) for c in U)
        quint = {c: min(R["quintiles"] - 1, i * R["quintiles"] // len(mc)) for i, (_, c) in enumerate(mc)}
        ret = {h: {} for h in exits}
        for h, d in exits.items():
            for c in U:
                if c in p1[h]:
                    r = adj_return(p0[c]["p"], E, p1[h][c]["p"], d, events.get(c))
                    if r is not None and -0.95 < r < 20:
                        ret[h][c] = r
        bm = {}
        for h in exits:
            rs = ret[h]
            bm[h] = {"mkt": statistics.mean(rs.values()) if rs else None,
                     "q": {qq: statistics.mean([rs[c] for c in rs if quint[c] == qq]) for qq in range(R["quintiles"])
                           if any(quint[c] == qq for c in rs)}}
        prer = {c: adj_return(ppre[c]["p"], pre, p0[c]["p"], E, events.get(c)) for c in U if c in ppre}
        prer = {c: v for c, v in prer.items() if v is not None and -0.95 < v < 20}
        pre_m = statistics.mean(prer.values()) if prer else 0

        def member(c):
            f = flags[c]
            hk, ss = f["hkscc"], f["ssf"]
            up = lambda x, n: x[1] >= n
            gv = f["g"] and p0[c]["pe"] and p0[c]["pe"] > 0 and p0[c]["ind"] in ind_med and p0[c]["pe"] <= ind_med[p0[c]["ind"]]
            any1, any2 = up(hk, 1) or up(ss, 1), up(hk, 2) or up(ss, 2)
            m = {"all": True, "hk_up1": up(hk, 1), "hk_new": hk[0] == "new", "hk_add": hk[0] == "add", "hk_up2": up(hk, 2),
                 "hk_up3": up(hk, 3), "hk_down1": hk[0] in ("cut", "out"), "ss_up1": up(ss, 1), "ss_new": ss[0] == "new",
                 "ss_add": ss[0] == "add", "ss_up2": up(ss, 2), "ss_up3": up(ss, 3), "ss_down1": ss[0] in ("cut", "out"),
                 "any_up2": any2, "g": f["g"], "g_nosig": f["g"] and not any1, "g_hk_up1": f["g"] and up(hk, 1),
                 "g_hk_up2": f["g"] and up(hk, 2), "g_ss_up1": f["g"] and up(ss, 1), "g_ss_up2": f["g"] and up(ss, 2),
                 "g_any_up2": f["g"] and any2, "gv": bool(gv), "gv_nosig": bool(gv) and not any1,
                 "gv_any_up1": bool(gv) and any1, "gv_any_up2": bool(gv) and any2, "nog_any_up2": (not f["g"]) and any2}
            return [g for g in GROUPS if m[g]]

        cnt = {g: 0 for g in GROUPS}
        for c in U:
            ex = {}
            for h in exits:
                if c in ret[h] and bm[h]["mkt"] is not None:
                    ex[h] = (ret[h][c] - bm[h]["mkt"], ret[h][c] - bm[h]["q"][quint[c]])
            pe_ = prer[c] - pre_m if c in prer else None
            for g in member(c):
                cnt[g] += 1
                obs[g].append((P, c, ex, pe_))
        per_date.append({"period": P, "entry": E, "exits": exits, "pre": pre, "universe": len(U), "n": cnt,
                         "mkt": {h: bm[h]["mkt"] for h in exits}})
        log(f"  {P} entry {E} universe={len(U)} hk_up1={cnt['hk_up1']} hk_up2={cnt['hk_up2']} ss_up1={cnt['ss_up1']} "
            f"g={cnt['g']} gv={cnt['gv']} mkt3m={bm.get(91, {}).get('mkt')}")

    # summary per group x horizon x benchmark
    summ = {}
    for g in GROUPS:
        s = {"n_obs": len(obs[g]), "n_per_date": round(len(obs[g]) / len(sig), 1)}
        pres = [o[3] for o in obs[g] if o[3] is not None]
        s["pre_ex_mean"] = round(statistics.mean(pres), 4) if pres else None
        for h in R["horizons"]:
            for bi, bn in ((0, "mkt"), (1, "size")):
                xs = [o[2][h][bi] for o in obs[g] if h in o[2]]
                byd = {}
                for o in obs[g]:
                    if h in o[2]:
                        byd.setdefault(o[0], []).append(o[2][h][bi])
                dm = [statistics.mean(v) for v in byd.values()]
                s[f"{h}_{bn}"] = None if not xs else {
                    "mean": round(statistics.mean(dm), 4), "median": round(statistics.median(xs), 4),
                    "hit": round(sum(1 for x in xs if x > 0) / len(xs), 3), "dates_pos": sum(1 for x in dm if x > 0),
                    "dates": len(dm), "t": None if tstat(dm) is None else round(tstat(dm), 2), "n": len(xs)}
        summ[g] = s
    # paired differences on the same signal dates (group A minus group B, size-adjusted excess)
    def date_means(g, h):
        byd = {}
        for o in obs[g]:
            if h in o[2]:
                byd.setdefault(o[0], []).append(o[2][h][1])
        return {p: statistics.mean(v) for p, v in byd.items()}
    PAIRS = [("hk_add", "hk_down1"), ("hk_up2", "hk_down1"), ("ss_up1", "ss_down1"), ("g_any_up2", "g_nosig"),
             ("g_hk_up2", "g_nosig"), ("gv_any_up2", "gv_nosig"), ("gv_any_up1", "gv_nosig"), ("g", "all"), ("gv", "g"),
             ("g_any_up2", "nog_any_up2")]
    pairs = {}
    for a, b in PAIRS:
        for h in R["horizons"]:
            A, Bm = date_means(a, h), date_means(b, h)
            d = [A[p] - Bm[p] for p in A if p in Bm]
            pairs[f"{a}-{b}@{h}"] = None if not d else {"mean": round(statistics.mean(d), 4), "pos": sum(1 for x in d if x > 0),
                                                        "dates": len(d), "t": None if tstat(d) is None else round(tstat(d), 2)}
    doc = {"pairs": pairs,"generated_at": t0.strftime("%Y-%m-%d %H:%M"), "rules": {k: list(v) if isinstance(v, tuple) else v for k, v in R.items()},
           "signal_periods": sig, "per_date": per_date, "summary": summ, "events_ok": ev_ok, "http": dict(em.STATS),
           "obs": {g: [[o[0], o[1]] for o in obs[g]] for g in ("gv_any_up2", "g_any_up2")}}
    path = os.path.join(DATA_DIR, f"instbt_{t0.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    log(f"saved {os.path.relpath(path, ROOT)} http={em.STATS}")
    log("group            n/date  pre91  | 3m mkt mean  t   pos/T  hit | 3m size mean t  | 6m size mean  t   pos/T  hit")
    for g in GROUPS:
        s = summ[g]
        a, b, c = s.get("91_mkt") or {}, s.get("91_size") or {}, s.get("182_size") or {}
        log(f"{g:16s} {s['n_per_date']:7.1f} {s['pre_ex_mean'] if s['pre_ex_mean'] is not None else float('nan'):+.3f} | "
            f"{a.get('mean', float('nan')):+.4f} {a.get('t') or 0:5.2f} {a.get('dates_pos', 0):2d}/{a.get('dates', 0):2d} {a.get('hit', 0):.2f} | "
            f"{b.get('mean', float('nan')):+.4f} {b.get('t') or 0:5.2f} | "
            f"{c.get('mean', float('nan')):+.4f} {c.get('t') or 0:5.2f} {c.get('dates_pos', 0):2d}/{c.get('dates', 0):2d} {c.get('hit', 0):.2f}")
    log("paired (size-adjusted, same dates): A-B  mean  pos/dates  t")
    for k, v in pairs.items():
        if v:
            log(f"  {k:28s} {v['mean']:+.4f} {v['pos']:2d}/{v['dates']:2d} {v['t'] if v['t'] is not None else float('nan'):+.2f}")
    return path
