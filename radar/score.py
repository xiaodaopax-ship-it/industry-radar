"""Lynch-style checklist, data-driven bull / bear points, research-priority ranking."""
from .paths import CFG

FL, POOL = CFG["flags"], CFG["pool"]
FF, FPOOL = FL["financial"], POOL["financial"]
FACTOR_CN = {"growth": "增速", "accel": "加速", "gm": "毛利率", "gm_trend": "毛利率趋势", "op_margin": "营业利润率",
             "value": "增速调整后的市销率", "dilution": "稀释", "insider": "内部人", "coverage": "关注度", "surprise": "EPS 超预期",
             "pretax_m": "税前利润率", "pretax_trend": "税前利润率趋势", "roe": "ROE", "value_fin": "PEG（无则市净率）", "equity_g": "股东权益增长"}


def derived(r, prof=None):
    """prof: {"profile": "standard" | "financial", "credit": bool, "verify": [...]} from the segment config."""
    prof = prof or {}
    m = r.get("mkt") or {}
    d = {"mcap": m.get("mcap"), "price": m.get("price"), "ccy": m.get("currency")}
    if prof.get("profile") == "financial":
        d.update(profile="financial", credit=bool(prof.get("credit")), seg_verify=list(prof.get("verify") or []))
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
    if d.get("profile") == "financial":
        _fin_derived(r, d)
    return d


def _fin_derived(r, d):
    """Pretax margin, ROE, P/B, equity growth, credit cost. Cash runway is not meaningful (cash is largely customer funds)."""
    pct = lambda a, b: round(a / b * 100, 1) if a is not None and b else None
    d["runway"] = None
    d["pretax_m"], d["pretax_m_ly"] = pct(r.get("pretax"), r.get("rev_q")), pct(r.get("pretax_ly"), r.get("rev_q_ly"))
    d["pretax_trend"] = None if d["pretax_m"] is None or d["pretax_m_ly"] is None else round(d["pretax_m"] - d["pretax_m_ly"], 1)
    eq, eq_ly = r.get("equity"), r.get("equity_ly")
    d["equity"] = eq
    avg = (eq + eq_ly) / 2 if eq and eq_ly and eq_ly > 0 else eq
    d["roe"] = pct(r.get("ni_ttm"), avg) if avg and avg > 0 else None
    d["pb"] = round(d["mcap"] / eq, 2) if d["mcap"] and eq and eq > 0 else None
    d["equity_g"] = round((eq / eq_ly - 1) * 100, 1) if eq and eq_ly and eq_ly > 0 else None
    # credit cost only for lending segments; > 100% of revenue is treated as a tagging error, not a number to rank on
    pr, pr_ly = pct(r.get("prov"), r.get("rev_q")), pct(r.get("prov_ly"), r.get("rev_q_ly"))
    ok = lambda x: x is not None and -50 <= x <= 100
    d["prov_r"] = pr if d["credit"] and ok(pr) else None
    d["prov_r_ly"] = pr_ly if d["prov_r"] is not None and ok(pr_ly) else None
    d["prov_chg"] = None if d["prov_r"] is None or d["prov_r_ly"] is None else round(d["prov_r"] - d["prov_r_ly"], 1)
    d["prov_bad"] = bool(d["credit"] and pr is not None and not ok(pr))
    d["nii_share"] = pct(r.get("nii"), r.get("rev_q")) if r.get("nii") is not None and r.get("basis") == "quarter" else None
    d["anomaly"] = [k for k in d["anomaly"] if k != "opm"]
    if d["pretax_trend"] is not None and abs(d["pretax_trend"]) > POOL["anomaly"]["opm_jump_pp"]:
        d["anomaly"].append("pretax")
    # TTM net income that does not fit the latest quarters or pretax income: usually a one-off (deferred tax asset
    # recognition) or a tagging error; ROE and P/E are then shown but not ranked
    ni, n2, pre = r.get("ni_ttm"), r.get("ni_q2"), r.get("pretax")
    if ni is not None and n2 is not None and n2 > 0 and ni > 0 and not 1 / 5 <= ni / (2 * n2) <= 5:
        d["anomaly"].append("ni")
        d["ni_why"] = f"最近两季合计 {n2 / 1e6:.0f} 百万"
    elif ni is not None and pre is not None and pre > 0 and ni > 8 * pre:
        d["anomaly"].append("ni")
        d["ni_why"] = f"本季税前利润 {pre / 1e6:.0f} 百万"
    # non-controlling interests: the market cap covers all shares but parent-only NI / equity do not (e.g. IBKR)
    tot = r.get("equity_total")
    d["nci_share"] = round((1 - eq / tot) * 100) if eq and tot and tot > eq > 0 else None
    if (d["nci_share"] or 0) >= FF["nci_warn_pct"]:
        d["anomaly"].append("nci")
    bad_val = "ni" in d["anomaly"] or "nci" in d["anomaly"]
    d["value_fin"] = None if "nci" in d["anomaly"] else d["pb"] if "ni" in d["anomaly"] or d["peg"] is None else d["peg"]
    d["roe_rankable"] = not bad_val


def _growth_item(r, d, add):
    acc = d["accel"]
    basis = "最新财年" if r.get("basis") == "annual" else "最新季度"
    add("增长", "ok" if acc is not None and acc >= 0 else "warn" if acc is not None else "na",
        f"{basis}同比 {r['yoy']}%" + (f"，上一季 {r['yoy_prev']}%，{'加速' if acc >= 0 else '放缓'} {abs(acc):.1f} 个百分点" if acc is not None else "（没有上一期同比可比）"))


def _tail_items(r, d, add):
    """Dilution, insiders, EPS vs estimate, coverage: identical for every profile."""
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


def _fin_checklist(r, d, cyc):
    """Financial profile: no gross margin, no cash runway (cash is largely customer funds); ROE / P/B / credit cost instead."""
    items = []
    add = lambda k, s, t: items.append({"key": k, "status": s, "text": t})
    _growth_item(r, d, add)
    pm, ly, tr = d["pretax_m"], d["pretax_m_ly"], d["pretax_trend"]
    if pm is not None:
        bad = "pretax" in d["anomaly"]
        add("盈利能力", "warn" if bad or pm <= 0 or (tr or 0) < 0 else "ok",
            f"税前利润率 {ly if ly is not None else '—'}% → {pm}%" + ("。变化幅度异常，可能含一次性项目，排序时不计入" if bad else
                                                                   "，低于" + str(FF["pretax_low_pct"]) + "%" if pm < FF["pretax_low_pct"] else ""))
    else:
        add("盈利能力", "na", "没有税前利润数据")
    roe, odd_ni, nci = d["roe"], "ni" in d["anomaly"], "nci" in d["anomaly"]
    if roe is not None and (odd_ni or nci):
        add("ROE", "warn", f"ROE {roe}%（TTM 净利润 / 平均股东权益）。" + ("TTM 净利润与最近季度或税前利润不匹配，可能含一次性项目或数据错误" if odd_ni else
                                                               f"少数股东权益约占股东权益合计的 {d['nci_share']}%，归母口径") + "，排序时不计入")
    else:
        add("ROE", "na" if roe is None else "ok" if roe >= FF["roe_good_pct"] else "warn" if roe < FF["roe_low_pct"] else "na",
            f"ROE {roe}%（TTM 净利润 / 平均股东权益）" + ("，低于 " + str(FF["roe_low_pct"]) + "%" if roe is not None and roe < FF["roe_low_pct"] else "") if roe is not None
            else ("TTM 亏损，ROE 为负" if (r.get("ni_ttm") or 0) < 0 else "没有 TTM 净利润或股东权益数据"))
    pe, pb, peg = d["pe"], d["pb"], d["peg"]
    if pe is not None or pb is not None:
        st = "na" if odd_ni or nci else "ok" if peg is not None and peg <= FL["peg_good_max"] else "warn" if (peg or 0) > 2 or (peg is None and (pb or 0) > FF["rich_pb"]) else "na"
        add("估值", st, f"市盈率 {num(pe, 0)} 倍，市净率 {num(pb, 1)} 倍" + (f"，近似 PEG {peg}" if peg is not None else "，未盈利或增速为负，PEG 不适用")
            + ("。市值按全部股份计，净利润和股东权益只含归母部分，两个倍数都偏高，要按原始财报换算" if nci else "。市盈率受净利润异常影响，参考市净率" if odd_ni else ""))
    else:
        add("估值", "na", "没有取到市值或盈利数据")
    eg, eq = d["equity_g"], d["equity"]
    if eq is not None and eq <= 0:
        add("资本", "warn", f"股东权益为负（{eq / 1e6:.0f} 百万美元）")
    elif eg is not None:
        add("资本", "warn" if eg < 0 else "ok", f"股东权益同比 {eg:+.1f}%（{eq / 1e8:.1f} 亿美元）" + ("，在下降" if eg < 0 else ""))
    else:
        add("资本", "na", "没有股东权益数据" if eq is None else f"股东权益 {eq / 1e8:.1f} 亿美元，没有去年同期可比")
    if not d["credit"]:
        add("信用成本", "na", "非放贷环节，不适用")
    elif d["prov_r"] is not None:
        st = "warn" if (d["prov_chg"] or 0) >= FF["prov_up_warn_pp"] or d["prov_r"] >= FF["prov_high_pct"] else "ok"
        add("信用成本", st, f"信用损失拨备占收入 {num(d['prov_r_ly'])}% → {d['prov_r']}%" +
            (f"，变化 {d['prov_chg']:+.1f} 个百分点" if d["prov_chg"] is not None else ""))
    else:
        add("信用成本", "na", "拨备数值异常（超过收入），疑似标签错误，读 10-Q 原文" if d["prov_bad"] else
            "XBRL 里没有拨备科目（可能按公允价值计量贷款，或只在年报披露），读 10-Q 的信用质量附注")
    if cyc:
        add("周期性", "warn", "收入随交易量 / 利率变化，有周期性")
    _tail_items(r, d, add)
    return items


def num(v, n=1):
    return "—" if v is None else f"{v:.{n}f}"


def checklist(r, d, cyc):
    if d.get("profile") == "financial":
        return _fin_checklist(r, d, cyc)
    items = []
    add = lambda k, s, t: items.append({"key": k, "status": s, "text": t})
    _growth_item(r, d, add)
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
    _tail_items(r, d, add)
    return items


def _fin_bull_bear(r, d, cyc, leader):
    bull, bear = [], []
    yoy, pm, tr, roe = r["yoy"], d["pretax_m"], d["pretax_trend"], d["roe"]
    if yoy >= 40 and (d["accel"] or 0) >= 0:
        bull.append(f"营收同比 {yoy:.0f}%" + ("且在加速" if d["accel"] is not None else ""))
    elif yoy >= 15:
        bull.append(f"营收同比 {yoy:.0f}%")
    elif yoy < 0:
        bear.append(f"营收同比下降 {abs(yoy):.0f}%")
    if (d["accel"] or 0) <= -10:
        bear.append(f"增速放缓 {abs(d['accel']):.0f} 个百分点")
    odd_ni, nci = "ni" in d["anomaly"], "nci" in d["anomaly"]
    if odd_ni:
        bear.append(f"TTM 净利润 {r['ni_ttm'] / 1e6:.0f} 百万美元与{d['ni_why']}"
                    + "不匹配，可能含一次性项目（如递延所得税资产确认）或数据错误，ROE" + (f" {roe:.0f}%" if roe is not None else "") + " 和市盈率参考有限")
    elif nci:
        bear.append(f"少数股东权益约占股东权益合计的 {d['nci_share']}%，市值按全部股份计而净利润和股东权益只含归母部分，市盈率 {num(d['pe'], 0)} 倍、市净率 {num(d['pb'], 1)} 倍都偏高，要按原始财报换算")
    elif roe is not None and roe >= FF["roe_good_pct"]:
        bull.append(f"ROE {roe:.0f}%，资本回报高")
    elif roe is not None and roe < FF["roe_low_pct"]:
        bear.append(f"ROE 只有 {roe:.1f}%")
    if "pretax" in d["anomaly"]:
        bear.append(f"税前利润率一年变化 {tr:+.0f} 个百分点，幅度异常，可能含一次性项目")
    elif pm is not None and pm > 0 and (tr or 0) >= 5:
        bull.append(f"税前利润率 {d['pretax_m_ly']:.0f}% → {pm:.0f}%，提升 {tr:.0f} 个百分点，经营杠杆在显现")
    elif tr is not None and tr <= -10:
        bear.append(f"税前利润率一年下降 {abs(tr):.0f} 个百分点（{d['pretax_m_ly']:.0f}% → {pm:.0f}%）")
    if pm is not None and pm < FF["pretax_low_pct"]:
        bear.append(f"税前利润率只有 {pm:.1f}%")
    elif pm is not None and pm >= FF["pretax_high_pct"] and "pretax" not in d["anomaly"] and not any(x.startswith("税前利润率 ") for x in bull):
        bull.append(f"税前利润率 {pm:.0f}%")
    if (r.get("ni_ttm") or 0) < 0:
        bear.append(f"TTM 净亏损 {-r['ni_ttm'] / 1e6:.0f} 百万美元")
    if d["equity"] is not None and d["equity"] <= 0:
        bear.append("股东权益为负")
    elif d["equity_g"] is not None and d["equity_g"] < 0:
        bear.append(f"股东权益一年下降 {abs(d['equity_g']):.0f}%（回购、分红或亏损，要看原因）")
    elif d["equity_g"] is not None and d["equity_g"] >= 15:
        bull.append(f"股东权益一年增长 {d['equity_g']:.0f}%")
    if d["prov_r"] is not None:
        if d["prov_chg"] is not None and d["prov_chg"] >= FF["prov_up_warn_pp"]:
            bear.append(f"信用损失拨备占收入 {d['prov_r_ly']:.1f}% → {d['prov_r']:.1f}%，上升 {d['prov_chg']:.1f} 个百分点，信用成本在上升")
        elif d["prov_r"] >= FF["prov_high_pct"]:
            bear.append(f"信用损失拨备占收入 {d['prov_r']:.0f}%，信用成本高")
        elif d["prov_chg"] is not None and d["prov_chg"] <= -FF["prov_up_warn_pp"]:
            bull.append(f"信用损失拨备占收入 {d['prov_r_ly']:.1f}% → {d['prov_r']:.1f}%，下降 {abs(d['prov_chg']):.1f} 个百分点")
    if cyc:
        bear.append("收入随交易量 / 利率变化，有周期性" + (f"（净利息收入约占收入 {d['nii_share']:.0f}%）" if d.get("nii_share") is not None and d["nii_share"] > 0 else ""))
    if odd_ni or nci:
        pass  # multiples are distorted; already explained above
    elif d["peg"] is not None and d["peg"] <= FL["peg_good_max"]:
        bull.append(f"近似 PEG {d['peg']}（市盈率 {d['pe']:.0f} 倍），增速相对估值便宜")
    elif (d["peg"] or 0) > 2:
        bear.append(f"估值偏高（市盈率 {d['pe']:.0f} 倍，PEG {d['peg']}" + (f"，市净率 {d['pb']} 倍" if d["pb"] else "") + "）")
    elif d["peg"] is None and (d["pb"] or 0) > FF["rich_pb"]:
        bear.append(f"市净率 {d['pb']} 倍，PEG 不适用，估值偏高")
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
    if "growth" in d["anomaly"]:
        bear.append(f"营收同比 {yoy:.0f}%，幅度异常，先确认是否有一次性收入")
    if not leader and r["rev_q"] < 20e6 and r.get("basis") == "quarter":
        bear.append(f"季度营收 {r['rev_q'] / 1e6:.0f} 百万美元，不到 2,000 万，基数小，增速波动大")
    if not d["mcap"]:
        bear.append("没有取到市值，估值无法判断")
    return bull, bear


def bull_bear(r, d, cyc, seg_id, leader=False):
    if d.get("profile") == "financial":
        return _fin_bull_bear(r, d, cyc, leader)
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


def _next_earnings(r):
    m = r.get("mkt") or {}
    nq, nd = m.get("next_q") or {}, m.get("next_date")
    if not nd:
        return []
    exp = []
    if nq.get("eps") is not None:
        exp.append(f"EPS 预期 {nq['eps']:.2f}")
    if nq.get("rev"):
        exp.append(f"营收预期 {nq['rev'] / 1e8:.1f} 亿美元")
    return [f"下次财报 {nd}：对比一致预期（{'，'.join(exp) or '暂无预期数据'}），重点看下季指引"]


def _fin_verify(r, d, cyc):
    q = _next_earnings(r)
    q += d["seg_verify"][:3]  # business questions configured per segment (read the 10-Q / monthly metrics)
    if cyc and d.get("nii_share") is not None:
        q.append(f"利率下降的敏感性：净利息收入约占本季收入 {d['nii_share']:.0f}%，看 10-Q 里的利率敏感性披露")
    if d["prov_chg"] is not None and d["prov_chg"] >= FF["prov_up_warn_pp"]:
        q.append(f"拨备占收入上升 {d['prov_chg']:.1f} 个百分点：是放贷规模扩大的前置拨备，还是逾期率在上升")
    if d["credit"] and d["prov_r"] is None:
        q.append("XBRL 没有可用的拨备数据：在 10-Q 信用质量附注里找拨备、核销和逾期率")
    if "ni" in d["anomaly"]:
        q.append("TTM 净利润与季度数据不匹配：核对 10-K / 10-Q 里的所得税（递延所得税资产估值准备转回）和一次性项目")
    if "nci" in d["anomaly"]:
        q.append("少数股东权益结构：按全部股份口径换算净利润和股东权益后再看市盈率、市净率")
    if "pretax" in d["anomaly"]:
        q.append("税前利润率异常变化：在财报附注里找一次性收益或费用")
    if "growth" in d["anomaly"]:
        q.append("营收构成：有没有一次性收益（出售资产、公允价值变动）")
    if (d["accel"] or 0) <= -10:
        q.append("增速放缓：是交易量 / 利率回落，还是客户在流失")
    if d["equity_g"] is not None and d["equity_g"] < 0:
        q.append("股东权益下降的原因：回购分红、亏损还是其他综合收益变动")
    if (r.get("dilution") or 0) > FL["dilution_warn_pct"]:
        q.append("股数增加的原因：增发、并购还是股权激励")
    if d["surprise"] is not None and d["surprise"] < 0:
        q.append("EPS 低于预期的原因，管理层是否下调了指引")
    if (d["ins_net"] or 0) < -0.5:
        q.append("内部人卖出是计划内减持（10b5-1）还是主动离场")
    q.append("两分钟故事：客户为什么选它，竞争对手为什么拦不住")
    return q[:6]


def verify(r, d, cyc, seg_id):
    """Turn the data-level risks into concrete questions to check before forming a view."""
    if d.get("profile") == "financial":
        return _fin_verify(r, d, cyc)
    q = _next_earnings(r)
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


def _fin_gate(r, d):
    """Cyclical segments stay in (flag only); a TTM net loss replaces the cash-runway gate; equity must be positive."""
    g = POOL["gates"]
    if g["require_market_data"] and not d["mcap"]:
        return "缺市值数据"
    if r.get("dilution") is not None and r["dilution"] > g["max_dilution_pct"]:
        return f"稀释超过 {g['max_dilution_pct']}%"
    if FPOOL["require_ttm_profit"]:
        if r.get("ni_ttm") is None:
            return "缺 TTM 净利润数据"
        if r["ni_ttm"] < 0:
            return f"TTM 净亏损 {-r['ni_ttm'] / 1e6:.0f} 百万美元（金融类要求盈利才进入排序）"
    if d["equity"] is not None and d["equity"] <= 0:
        return "股东权益为负"
    if "growth" in d["anomaly"]:
        return f"营收同比超过 {g['max_yoy_pct']}%，先核实是否一次性收入"
    if d["mcap"] and d["mcap"] < g.get("min_mcap", 0):
        return f"市值不到 {g['min_mcap'] / 1e8:.0f} 亿美元，流动性风险大"
    return None


def gate(r, d, mem):
    if d.get("profile") == "financial":
        return _fin_gate(r, d)
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
    # financial profile
    "pretax_m": (lambda r, d: None if "pretax" in d["anomaly"] else d.get("pretax_m"), True),
    "pretax_trend": (lambda r, d: None if "pretax" in d["anomaly"] else d.get("pretax_trend"), True),
    "roe": (lambda r, d: d.get("roe") if d.get("roe_rankable") else None, True), "value_fin": (lambda r, d: d.get("value_fin"), False),
    "equity_g": (lambda r, d: d.get("equity_g"), True),
}


def rank(group, profile="standard"):
    """Average percentile rank across factors within a sector and profile; a missing factor counts as 0.5 (neutral)."""
    for name in (FPOOL["factors"] if profile == "financial" else POOL["factors"]):
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
