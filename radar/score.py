"""Lynch-style checklist, data-driven bull / bear points, research-priority ranking."""
from .paths import CFG

FL, POOL = CFG["flags"], CFG["pool"]
FACTOR_CN = {"growth": "增速", "accel": "加速", "gm": "毛利率", "gm_trend": "毛利率趋势", "op_margin": "营业利润率",
             "value": "增速调整后的市销率", "dilution": "稀释", "insider": "内部人", "coverage": "关注度", "surprise": "EPS 超预期"}


def derived(r):
    m = r.get("mkt") or {}
    d = {"mcap": m.get("mcap"), "price": m.get("price"), "ccy": m.get("currency")}
    d["accel"] = None if r.get("yoy_prev") is None else round(r["yoy"] - r["yoy_prev"], 1)
    d["gm_trend"] = None if r.get("gm") is None or r.get("gm_ly") is None else round(r["gm"] - r["gm_ly"], 1)
    d["opm_trend"] = None if r.get("opm") is None or r.get("opm_ly") is None else round(r["opm"] - r["opm_ly"], 1)
    g = r["yoy"] if r.get("yoy_prev") is None else (r["yoy"] + r["yoy_prev"]) / 2
    d["g"] = round(g, 1)
    if d["mcap"] and r.get("rev_ttm"):
        d["ps"] = round(d["mcap"] / r["rev_ttm"], 2)
        d["ps_g"] = round(d["ps"] / g * 100, 2) if g > 0 else None
        d["pe"] = round(d["mcap"] / r["ni_ttm"], 1) if r.get("ni_ttm") and r["ni_ttm"] > 0 else None
        d["peg"] = round(d["pe"] / g, 2) if d["pe"] and g > 0 else None
    else:
        d.update(ps=None, ps_g=None, pe=None, peg=None)
    d["runway"] = round(r["cash"] / (-r["ni_ttm"] / 4), 1) if r.get("ni_ttm") is not None and r["ni_ttm"] < 0 and r.get("cash") else None
    ins = r.get("insider")
    d["ins_net"] = (ins["buy_val"] - ins["sell_val"]) / d["mcap"] * 100 if d["mcap"] and ins else None
    h = [x for x in m.get("eps_hist", []) if x.get("est") is not None]
    d["eps_last"] = h[-1] if h else None
    last = d["eps_last"]
    d["surprise"] = round((last["actual"] - last["est"]) / abs(last["est"]) * 100, 1) if last and abs(last["est"]) >= 0.05 else None
    d["pos52"] = round((m["price"] - m["lo52"]) / (m["hi52"] - m["lo52"]) * 100) if m.get("price") and m.get("hi52") and m.get("lo52") and m["hi52"] > m["lo52"] else None
    d["anomaly"] = [k for k, bad in (("growth", r["yoy"] > POOL["gates"]["max_yoy_pct"]),
                                     ("opm", d["opm_trend"] is not None and abs(d["opm_trend"]) > POOL["anomaly"]["opm_jump_pp"])) if bad]
    return d


def checklist(r, d, cyc):
    items = []
    add = lambda k, s, t: items.append({"key": k, "status": s, "text": t})
    acc = d["accel"]
    basis = "最新财年" if r.get("basis") == "annual" else "最新季度"
    add("增长", "ok" if acc is not None and acc >= 0 else "warn" if acc is not None else "na",
        f"{basis}同比 {r['yoy']}%" + (f"，上一季 {r['yoy_prev']}%，{'加速' if acc >= 0 else '放缓'} {abs(acc):.1f} 个百分点" if acc is not None else "（没有上一期同比可比）"))
    if r.get("gm") is not None:
        jump = d["gm_trend"]
        price_jump = jump is not None and jump >= FL["gm_jump_warn_pp"] and (r.get("gm_ly") or 0) >= 0
        st = "warn" if price_jump or cyc or r["gm"] < FL["low_gm_pct"] else "ok"
        why = ("毛利率大幅跳升，增长可能主要来自涨价，需要核实" if price_jump else "所在环节有周期性，要区分涨价和销量" if cyc else
               f"毛利率低于 {FL['low_gm_pct']}%，定价能力弱" if r["gm"] < FL["low_gm_pct"] else "毛利率在扩张" if (jump or 0) >= 0 else "毛利率小幅下降")
        add("增长质量", st, f"毛利率 {r.get('gm_ly') if r.get('gm_ly') is not None else '—'}% → {r['gm']}%。{why}")
    else:
        add("增长质量", "warn" if cyc else "na", "没有毛利数据" + ("；所在环节有周期性" if cyc else ""))
    if r.get("opm") is not None:
        bad = "opm" in d["anomaly"]
        add("盈利能力", "warn" if bad else "ok" if r["opm"] > 0 and (d["opm_trend"] or 0) >= 0 else "warn",
            f"营业利润率 {r.get('opm_ly') if r.get('opm_ly') is not None else '—'}% → {r['opm']}%" + ("。变化幅度异常，可能含一次性项目，排序时不计入" if bad else ""))
    else:
        add("盈利能力", "na", "没有营业利润数据")
    if d["ps"] is not None:
        st = "ok" if d["peg"] is not None and d["peg"] <= FL["peg_good_max"] else "warn" if (d["peg"] or 0) > 2 or (d["ps"] > FL["rich_ps"] and d["peg"] is None) else "na"
        add("估值", st, f"市销率 {d['ps']} 倍" + (f"，市盈率 {d['pe']:.0f} 倍，近似 PEG {d['peg']}" if d["peg"] else "，未盈利或增速为负，PEG 不适用"))
    else:
        add("估值", "na", "没有取到市值")
    if r.get("ni_ttm") is not None and r["ni_ttm"] < 0:
        rw = d["runway"]
        add("资产负债表", "warn" if rw is None or rw < FL["runway_warn_quarters"] else "ok",
            f"TTM 亏损 {-r['ni_ttm'] / 1e6:.0f} 百万美元；现金约够 {rw} 个季度（按净亏损粗算）" if rw else "TTM 亏损，没有现金数据")
    else:
        add("资产负债表", "ok" if r.get("ni_ttm") else "na", "TTM 盈利" if r.get("ni_ttm") else "没有 TTM 净利润数据")
    if r.get("dilution") is not None:
        add("稀释", "warn" if r["dilution"] > FL["dilution_warn_pct"] else "ok", f"稀释后股数同比 {r['dilution']:+.1f}%" +
            ("。超过 100% 通常是刚上市或合并，要看招股书" if r["dilution"] > 100 else ""))
    else:
        add("稀释", "na", "没有股数数据")
    ins = r.get("insider")
    if ins is None:
        add("内部人", "na", "非美国市场，没有采集内部人申报" if r.get("market") != "US" else "没有 Form 4 数据")
    elif ins.get("checked"):
        st = "ok" if ins["buys"] > 0 and ins["buy_val"] >= ins["sell_val"] else "warn" if (d["ins_net"] or 0) < -0.5 else "na"
        add("内部人", st, f"近 {CFG['form4']['lookback_days']} 天公开市场买入 {ins['buys']} 笔（{ins['buy_val'] / 1e6:.1f}M），卖出 {ins['sells']} 笔（{ins['sell_val'] / 1e6:.1f}M）；检查了 {ins['checked']}/{ins['total']} 份 Form 4")
    else:
        add("内部人", "na", "窗口内没有 Form 4")
    s, last = d["surprise"], d["eps_last"]
    add("财报 vs 预期", "ok" if s is not None and s >= 0 else "warn" if s is not None else "na",
        (f"最近一季 EPS 实际 {last['actual']}，预期 {last['est']}" + (f"，差 {s:+.0f}%" if s is not None else "（预期值太小，不算百分比）")) if last else "没有 EPS 预期数据")
    an = (r.get("mkt") or {}).get("analysts")
    add("关注度", "ok" if an is not None and an <= FL["few_analysts_max"] else "warn" if an is not None and an >= FL["many_analysts_min"] else "na",
        f"分析师覆盖 {an if an is not None else '—'} 家" + ("，关注少，可能有信息差" if an is not None and an <= FL["few_analysts_max"] else "，关注多，信息差小" if an is not None and an >= FL["many_analysts_min"] else ""))
    return items


def bull_bear(r, d, cyc, seg_id, leader=False):
    bull, bear = [], []
    yoy = r["yoy"]
    if yoy >= 40 and (d["accel"] or 0) >= 0:
        bull.append(f"营收同比 {yoy:.0f}%" + ("且在加速" if d["accel"] is not None else ""))
    elif yoy >= 15:
        bull.append(f"营收同比 {yoy:.0f}%")
    elif yoy < 0:
        bear.append(f"营收同比下降 {abs(yoy):.0f}%")
    if (d["accel"] or 0) <= -10:
        bear.append(f"增速放缓 {abs(d['accel']):.0f} 个百分点")
    if r.get("gm") is not None and r["gm"] >= FL["high_gm_pct"] and not cyc:
        bull.append(f"毛利率 {r['gm']:.0f}%，定价能力强")
    if r.get("gm") is not None and r["gm"] < FL["low_gm_pct"]:
        bear.append(f"毛利率只有 {r['gm']:.0f}%")
    if d["gm_trend"] is not None and d["gm_trend"] >= FL["gm_jump_warn_pp"] and (r.get("gm_ly") or 0) >= 0:
        bear.append(f"毛利率一年跳升 {d['gm_trend']:.0f} 个百分点，可能是涨价驱动")
    if cyc:
        bear.append("所在环节有周期性，要防止把周期股当成长股")
    if d["gm_trend"] is not None and d["gm_trend"] <= -5:
        bear.append(f"毛利率一年下降 {abs(d['gm_trend']):.0f} 个百分点，增长可能是靠低毛利业务换来的")
    if "opm" in d["anomaly"]:
        bear.append(f"营业利润率一年变化 {d['opm_trend']:+.0f} 个百分点，幅度异常，可能含一次性项目")
    elif d["opm_trend"] is not None and d["opm_trend"] <= -10:
        bear.append(f"营业利润率一年下降 {abs(d['opm_trend']):.0f} 个百分点，收入增长没有转化成利润")
    elif r.get("opm") is not None and r["opm"] > 0 and (d["opm_trend"] or 0) >= 5:
        bull.append(f"营业利润率提升 {d['opm_trend']:.0f} 个百分点，经营杠杆在显现")
    if "growth" in d["anomaly"]:
        bear.append(f"营收同比 {yoy:.0f}%，幅度异常，先确认是否有一次性收入")
    if seg_id == "utility":
        bear.append("公用事业的营收增长经常来自燃料和电价成本转嫁，要看售电量和费率基数")
    if seg_id == "biotech" and yoy >= 50:
        bear.append("生物技术公司的营收可能含授权费或里程碑款，要看收入构成")
    if d["peg"] is not None and d["peg"] <= FL["peg_good_max"]:
        bull.append(f"近似 PEG {d['peg']}，增速相对估值便宜")
    # high P/S alone is not "expensive" when earnings growth already covers it (PEG <= 1)
    if (d["peg"] or 0) > 2 or ((d["ps"] or 0) > FL["rich_ps"] and (d["peg"] is None or d["peg"] > 1)):
        bear.append(f"估值偏高（市销率 {d['ps']} 倍" + (f"，PEG {d['peg']}" if d["peg"] else "") + "）")
    if d["runway"] is not None and d["runway"] < FL["runway_warn_quarters"]:
        bear.append(f"仍在亏损，现金约够 {d['runway']} 个季度")
    if (r.get("dilution") or 0) > FL["dilution_warn_pct"]:
        bear.append(f"股数一年增加 {r['dilution']:.0f}%，老股东被稀释")
    ins = r.get("insider") or {}
    if ins.get("buys") and ins["buy_val"] >= ins["sell_val"]:
        bull.append(f"内部人公开市场净买入（{ins['buys']} 笔）")
    elif (d["ins_net"] or 0) < -0.5:
        bear.append(f"内部人净卖出约占市值 {abs(d['ins_net']):.1f}%")
    an = (r.get("mkt") or {}).get("analysts")
    if not leader and an is not None and an <= FL["few_analysts_max"]:
        bull.append(f"只有 {an} 家分析师覆盖，可能有信息差")
    if d["surprise"] is not None and d["surprise"] >= FL["eps_beat_pct"]:
        bull.append(f"最近一季 EPS 超预期 {d['surprise']:.0f}%" + ("（预期基数小，参考有限）" if d["surprise"] > POOL["anomaly"]["surprise_small_base_pct"] else ""))
    if d["surprise"] is not None and d["surprise"] < 0:
        bear.append(f"最近一季 EPS 低于预期 {abs(d['surprise']):.0f}%")
    if not leader and r["rev_q"] < 20e6 and r.get("basis") == "quarter":
        bear.append("季度营收不到 2,000 万美元，基数小，增速波动大")
    if r.get("basis") == "annual":
        bear.append("只有年度数据（日本公司在免费数据源里没有季度数据），信息滞后")
    if not leader and r.get("market") not in (None, "US"):
        bear.append("海外市场：没有内部人交易数据，免费数据只有财务汇总，需要回到当地交易所的原始财报核实")
    if not d["mcap"]:
        bear.append("没有取到市值，估值无法判断")
    return bull, bear


def verify(r, d, cyc, seg_id):
    """Turn the data-level risks into concrete questions to check before forming a view."""
    q = []
    m = r.get("mkt") or {}
    nq, nd = m.get("next_q") or {}, m.get("next_date")
    if nd:
        exp = []
        if nq.get("eps") is not None:
            exp.append(f"EPS 预期 {nq['eps']:.2f}")
        if nq.get("rev"):
            exp.append(f"营收预期 {nq['rev'] / 1e8:.1f} 亿美元")
        q.append(f"下次财报 {nd}：对比一致预期（{'，'.join(exp) or '暂无预期数据'}），重点看下季指引")
    if cyc:
        q.append("这一轮增长有多少来自涨价：看平均售价和出货量各自的变化")
    if d["gm_trend"] is not None and d["gm_trend"] >= FL["gm_jump_warn_pp"] and (r.get("gm_ly") or 0) >= 0:
        q.append("毛利率跳升能否持续：是涨价、产品结构变化，还是一次性因素")
    if r.get("gm") is not None and r["gm"] < FL["low_gm_pct"]:
        q.append("毛利率偏低：是行业属性（代工、组装）还是有改善空间")
    if (d["gm_trend"] is not None and d["gm_trend"] <= -5) or (d["opm_trend"] is not None and d["opm_trend"] <= -10):
        q.append("利润率下降：是产品结构变化、价格竞争，还是扩张期的费用投入")
    if r.get("market") not in (None, "US"):
        q.append("核对原始财报：台湾 MOPS、韩国 DART、日本 EDINET / TDnet")
    if "opm" in d["anomaly"]:
        q.append("营业利润率异常变化：在财报附注里找一次性收益或费用")
    if "growth" in d["anomaly"] or (seg_id == "biotech" and r["yoy"] >= 50):
        q.append("营收构成：有没有授权费、里程碑款或一次性大单")
    if (d["accel"] or 0) <= -10:
        q.append("增速放缓：是去年基数高，还是需求在变弱")
    if (r.get("dilution") or 0) > FL["dilution_warn_pct"]:
        q.append("股数增加的原因：增发、并购还是股权激励")
    if d["runway"] is not None and d["runway"] < FL["runway_warn_quarters"] * 2:
        q.append("现金还能撑多久，有没有融资计划")
    if d["surprise"] is not None and d["surprise"] < 0:
        q.append("EPS 低于预期的原因，管理层是否下调了指引")
    if (d["peg"] or 0) > 2 or ((d["ps"] or 0) > FL["rich_ps"] and (d["peg"] is None or d["peg"] > 1)):
        q.append("当前估值隐含了多高的增速，公司能不能兑现")
    if (d["ins_net"] or 0) < -0.5:
        q.append("内部人卖出是计划内减持（10b5-1）还是主动离场")
    if seg_id == "utility":
        q.append("看售电量和获批费率基数的增长，而不是营收")
    if r.get("basis") == "annual":
        q.append("找最新季报补全季度数据（EDINET 或公司投资者关系页）")
    q.append("客户集中度：最大客户占营收多少（年报里有披露）")
    q.append("两分钟故事：它为什么会越卖越多，竞争对手为什么拦不住")
    return q[:6]


def gate(r, d, mem):
    g = POOL["gates"]
    if g["exclude_cyclical"] and mem["cyclical"]:
        return "周期性环节"
    if g["require_market_data"] and not d["mcap"]:
        return "缺市值数据"
    if r.get("dilution") is not None and r["dilution"] > g["max_dilution_pct"]:
        return f"稀释超过 {g['max_dilution_pct']}%"
    if d["runway"] is not None and d["runway"] < g["min_runway_quarters"]:
        return f"现金不足 {g['min_runway_quarters']} 个季度"
    if "growth" in d["anomaly"]:
        return f"营收同比超过 {g['max_yoy_pct']}%，先核实是否一次性收入"
    if d["gm_trend"] is not None and d["gm_trend"] >= FL["gm_jump_warn_pp"] and (r.get("gm_ly") or 0) >= 0:
        return "毛利率一年跳升过大，疑似涨价驱动的周期性增长"
    if d["mcap"] and d["mcap"] < g.get("min_mcap", 0):
        return f"市值不到 {g['min_mcap'] / 1e8:.0f} 亿美元，流动性风险大"
    return None


FACTOR = {
    "growth": (lambda r, d: r["yoy"], True), "accel": (lambda r, d: d["accel"], True),
    "gm": (lambda r, d: r.get("gm"), True), "gm_trend": (lambda r, d: d["gm_trend"], True),
    "op_margin": (lambda r, d: None if "opm" in d["anomaly"] else r.get("opm"), True), "value": (lambda r, d: d["ps_g"], False),
    "dilution": (lambda r, d: r.get("dilution"), False), "insider": (lambda r, d: d["ins_net"], True),
    "coverage": (lambda r, d: (r.get("mkt") or {}).get("analysts"), False), "surprise": (lambda r, d: d["surprise"], True),
}


def rank(group):
    """Average percentile rank across factors within a sector; a missing factor counts as 0.5 (neutral)."""
    for name in POOL["factors"]:
        getter, higher = FACTOR[name]
        vals = [(getter(e["_r"], e["d"]), e) for e in group]
        known = sorted(v for v, _ in vals if v is not None)
        for v, e in vals:
            if v is None or len(known) < 2:
                p = 0.5
            else:
                below = sum(1 for k in known if k < v) + 0.5 * (sum(1 for k in known if k == v) - 1)
                p = below / (len(known) - 1)
                p = p if higher else 1 - p
            e["factors"][name] = round(p, 2)
    for e in group:
        e["score"] = round(sum(e["factors"].values()) / len(e["factors"]), 3)
    for i, e in enumerate(sorted(group, key=lambda e: -e["score"])):
        e["rank"], e["pool"] = i + 1, i < POOL["size"]


def conclusion(e):
    if e.get("pool"):
        level = "优先研究"
    elif e.get("gate"):
        level = "暂不优先"
    else:
        level = "可以观察" if len(e["bull"]) >= len(e["bear"]) else "暂不优先"
    b = "；".join(e["bull"][:2]) or "数据上没有突出的优点"
    w = "；".join(e["bear"][:2]) or "数据上没有明显的风险点"
    text = f"{level}。{b}。但要注意：{w}。" + (f"没有进入排序，原因：{e['gate']}。" if e.get("gate") else "")
    return level, text
