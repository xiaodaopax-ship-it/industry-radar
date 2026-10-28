"""Smoke test for cn/em.py. Run from the industry-radar root:  python -m cn.smoke

Calls every function with small, realistic arguments, prints counts + one sample row (ASCII-escaped),
and cross-checks 600519 / 300750 between independent Eastmoney reports. Exit code 1 if any check fails.
"""
import collections
import datetime as dt
import json
import os
import shutil
import sys

from . import em

RESULTS = []  # (name, ok, detail)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def sample(rows, pred=None):
    r = next((x for x in rows if pred(x)), None) if pred else (rows[0] if rows else None)
    print("  sample:", json.dumps(r, ensure_ascii=True)[:700] if r else None)


def rel(a, b):
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b)


def close(name, a, b, tol):
    r = rel(a, b)
    check(name, r is not None and r <= tol, f"a={a} b={b} rel_diff={None if r is None else round(r, 5)} tol={tol}")


def main():
    # check details may contain characters the console code page lacks (e.g. U+2212 on GBK): never crash on printing
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    today = dt.date.today()
    print(f"cn smoke test, today={today}")

    print("\n[a] quotes()")
    q = em.quotes()  # EOD snapshot (default). em.quotes(live=True) additionally tries push2 live paging.
    live_probe = em.fetch_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&secids=1.600519,0.300750"
        "&fields=f2,f3,f8,f9,f12,f13,f14,f20,f21,f23,f26,f100,f114,f115", ttl=1.0, quiet=True)
    live_rows = ((live_probe or {}).get("data") or {}).get("diff") or []
    print(f"  push2 ulist probe (2 codes, verifies f-codes): rows={len(live_rows)}")
    for r in live_rows:
        print("  push2:", json.dumps(r, ensure_ascii=True))
    qm0 = {x["code"]: x for x in q}
    for r in live_rows:  # live vs EOD should be close (same day or adjacent day)
        e = qm0.get(r.get("f12"), {})
        close(f"{r.get('f12')} push2 f115 PE(TTM) vs EOD PE_TTM", em.num(r.get("f115")), e.get("pe_ttm"), 0.12)
        close(f"{r.get('f12')} push2 f20 total mcap vs EOD", em.num(r.get("f20")), e.get("total_mcap"), 0.12)
        close(f"{r.get('f12')} push2 f23 PB vs EOD PB_MRQ", em.num(r.get("f23")), e.get("pb"), 0.12)
    by_board = collections.Counter(x["board"] for x in q)
    by_src = collections.Counter(x["quote_source"] for x in q)
    print(f"  rows={len(q)} by_board={dict(by_board)} by_source={dict(by_src)}")
    if not by_src.get("push2_live"):
        print("  NOTE: push2 live quotes unavailable this run; EOD values used (turnover/list_date empty)")
    check("quotes count > 5000", len(q) > 5000)
    check("quotes all 5 boards present", len(by_board) == 5)
    qm = {x["code"]: x for x in q}
    sample(q, lambda x: x["code"] == "600519")
    check("quotes 600519 market=SH board=SH_MAIN", qm.get("600519", {}).get("market") == "SH"
          and qm["600519"]["board"] == "SH_MAIN")
    check("quotes 300750 board=ChiNext", qm.get("300750", {}).get("board") == "ChiNext")
    cov = sum(1 for x in q if x["industry"]) / max(len(q), 1)
    check("quotes industry coverage > 90%", cov > 0.9, f"coverage={cov:.3f}")

    fin = {}
    for rd in ("2026-06-30", "2026-03-31"):
        print(f"\n[b] financials('{rd}')")
        f = em.financials(rd)
        fin[rd] = {x["code"]: x for x in f}
        n = len(f)
        c_ocf = sum(1 for x in f if x["operating_cash_flow"] is not None)
        c_bs = sum(1 for x in f if x["has_balance_sheet"])
        c_rev = sum(1 for x in f if x["revenue"] is not None)
        print(f"  rows={n} with_revenue={c_rev} with_ocf={c_ocf} with_balance_sheet={c_bs}")
        check(f"financials {rd} count > 5000", n > 5000)
        check(f"financials {rd} OCF coverage > 95%", n and c_ocf / n > 0.95, f"{c_ocf}/{n}")
        check(f"financials {rd} balance-sheet coverage > 95%", n and c_bs / n > 0.95, f"{c_bs}/{n}")
        sample(f, lambda x: x["code"] == "300750")

    print("\n[cross-checks] 600519 / 300750")
    for code in ("600519", "300750"):
        h1, q1 = fin["2026-06-30"].get(code), fin["2026-03-31"].get(code)
        check(f"{code} present in both financial dates", h1 is not None and q1 is not None)
        if not h1 or not q1:
            continue
        inc = em.income_statement(code, "2026-06-30")
        inc = inc[0] if inc else {}
        close(f"{code} H1 revenue: CPD vs DMSK_INCOME", h1["revenue"], inc.get("revenue"), 0.001)
        close(f"{code} H1 parent NP: CPD vs DMSK_INCOME", h1["parent_net_profit"], inc.get("parent_net_profit"), 0.001)
        ocf_ps = (h1["operating_cash_flow_per_share"] or 0) * (h1["share_capital"] or 0)
        close(f"{code} H1 OCF: per-share x share capital vs CASHFLOW", ocf_ps, h1["operating_cash_flow"], 0.03)
        close(f"{code} H1 equity: BPS x share capital vs GBALANCE parent equity",
              (h1["bps"] or 0) * (h1["share_capital"] or 0), h1["parent_equity"], 0.05)
        check(f"{code} YTD cumulative: H1 revenue > Q1 revenue",
              (h1["revenue"] or 0) > (q1["revenue"] or 0) > 0, f"H1={h1['revenue']} Q1={q1['revenue']}")
        print(f"  {code} H1: OCF/NP={h1['operating_cash_flow'] / h1['parent_net_profit']:.2f} "
              f"AR={h1['accounts_receivable']} inventory={h1['inventory']} goodwill={h1['goodwill']} "
              f"interest_bearing_debt={h1['interest_bearing_debt']}")

    print("\n[h] valuation_history('600519', 5) / ('300750', 5)")
    for code in ("600519", "300750"):
        vh = em.valuation_history(code, 5)
        print(f"  {code} rows={len(vh)} first={vh[0]['date'] if vh else None} last={vh[-1]['date'] if vh else None}")
        check(f"valuation_history {code} >= 1000 daily rows", len(vh) >= 1000)
        if vh:
            sample(vh[-1:])
            for p in em.valuation_percentiles(vh):
                print(f"  {code} {p['metric']}: latest={p['latest']} pct={p['percentile']} n={p['n']} "
                      f"min={p['min']} median={p['median']} max={p['max']}")
            qq = qm.get(code, {})
            close(f"{code} total mcap: quotes vs valuation_history last", qq.get("total_mcap"), vh[-1]["total_mcap"], 0.05)
            close(f"{code} PE TTM: quotes f115 vs valuation_history last", qq.get("pe_ttm"), vh[-1]["pe_ttm"], 0.05)
            close(f"{code} PB: quotes f23 vs valuation_history last", qq.get("pb"), vh[-1]["pb"], 0.05)

    print("\n[c] forecasts(since='2026-09-01')")
    fc = em.forecasts("2026-09-01")
    np_rows = [x for x in fc if x["metric_code"] == "004"]
    print(f"  rows={len(fc)} net_profit_rows={len(np_rows)} types={dict(collections.Counter(x['forecast_type'] for x in np_rows))}")
    check("forecasts count > 0", len(fc) > 0)
    check("forecasts all notice_date >= since", all((x["notice_date"] or "") >= "2026-09-01" for x in fc))
    sample(np_rows or fc)

    print("\n[d] holder_changes(since='2026-07-01')")
    hc = em.holder_changes("2026-07-01")
    cnt = collections.Counter((x["source"], x["direction"]) for x in hc)
    print(f"  rows={len(hc)} by_source_direction={dict((f'{a}/{b}', v) for (a, b), v in cnt.items())}")
    check("holder_changes executive rows > 0", any(x["source"] == "executive" for x in hc))
    check("holder_changes major_shareholder rows > 0", any(x["source"] == "major_shareholder" for x in hc))
    check("holder_changes sign consistent (decrease => shares < 0)",
          all((x["shares"] < 0) == (x["direction"] == "decrease") for x in hc if x["shares"] and x["direction"] != "unknown"))
    sample(hc, lambda x: x["source"] == "executive")
    sample(hc, lambda x: x["source"] == "major_shareholder")

    print("\n[e] pledge()")
    pl = em.pledge()
    print(f"  rows={len(pl)} stats_date={pl[0]['stats_date'] if pl else None}")
    check("pledge count > 500", len(pl) > 500)
    check("pledge ratio within 0-100", all(0 <= (x["pledge_ratio_pct"] or 0) <= 100 for x in pl))
    sample(pl)

    end = (today + dt.timedelta(days=60)).isoformat()
    print(f"\n[f] unlocks('{today}', '{end}')")
    ul = em.unlocks(today.isoformat(), end)
    print(f"  rows={len(ul)} total_unlock_value_cny={sum(x['unlock_value'] or 0 for x in ul):.0f}")
    check("unlocks count > 0", len(ul) > 0)
    check("unlocks dates in window", all(today.isoformat() <= (x["date"] or "") <= end for x in ul))
    check("unlocks ratio_total_pct within 0-100", all(0 <= (x["ratio_total_pct"] or 0) <= 100 for x in ul))
    sample(ul)

    print("\n[g] regulatory(since='2026-09-01')")
    rg = em.regulatory("2026-09-01")
    kinds = collections.Counter(x["kind"] for x in rg)
    exch = collections.Counter(em.market_of(x["code"]) for x in rg)
    print(f"  rows={len(rg)} kinds={dict(kinds)} by_exchange={dict(exch)}")
    check("regulatory count > 0", len(rg) > 0)
    check("regulatory covers SH and SZ codes", exch.get("SH", 0) > 0 and exch.get("SZ", 0) > 0)
    sample(rg, lambda x: x["kind"] in ("inquiry_letter", "concern_letter", "investigation"))

    print("\n[j] 52-week qfq range / research reports / target median (display only)")
    from . import ranges, cards
    syn = [{"date": "2025-09-01", "close": 9, "high": 9, "low": 9}, {"date": "2025-10-23", "close": 10, "high": 11, "low": 9.5},
           {"date": "2026-03-02", "close": 14, "high": 15, "low": 13}, {"date": "2026-10-21", "close": 12, "high": 12.5, "low": 11.5}]
    it = ranges.summarize(syn, "2025-10-23", "2026-10-21", "qfq_hl", "x", 12)
    check("ranges.summarize hi/lo/dates (pre-window bar excluded)", it["hi52"] == 15 and it["lo52"] == 9.5
          and it["hi_date"] == "2026-03-02" and it["lo_date"] == "2025-10-23" and it["n_days"] == 3, json.dumps(it)[:200])
    check("ranges.summarize flags clean", not it["short_history"] and not it["stale"] and not it["basis_mismatch"])
    it2 = ranges.summarize(syn[2:3], "2025-10-23", "2026-10-21", "qfq_hl", "x", 14)
    check("ranges.summarize short history + stale flagged", it2["short_history"] and it2["stale"])
    rows_ = [{"code": "X", "price": 12}]
    ranges.attach(rows_, {"items": {"X": it}}, "2026-10-21")
    check("ranges.attach pos52 / off_hi / off_lo", rows_[0]["pos52"] == round(100 * 2.5 / 5.5, 1)
          and rows_[0]["off_hi"] == -20.0 and rows_[0]["off_lo"] == round(100 * (12 / 9.5 - 1), 1), str(rows_[0]["pos52"]))
    qd = qm.get("600519", {}).get("as_of")
    if qd:
        st = (dt.date.fromisoformat(qd[:10]) - dt.timedelta(weeks=52) + dt.timedelta(days=1)).isoformat()
        ev, ok_b = em.share_bonus("600519", st)
        check("share_bonus 600519 ok with >= 1 cash dividend in 52 weeks", ok_b and any((e["cash_per10"] or 0) > 0 for e in ev), str(len(ev)))
        bars, ok_v, _ = ranges.derived_qfq_closes("600519", st, qd[:10], ev, 1.0)
        r6 = ranges.summarize(bars, st, qd[:10], "qfq_close", "", qm["600519"]["price"])
        check("derived qfq 600519 >= 200 bars, price within [lo52, hi52]", ok_v and r6["n_days"] >= 200
              and r6["lo52"] <= qm["600519"]["price"] <= r6["hi52"], f"n={r6['n_days']} lo={r6['lo52']} hi={r6['hi52']}")
        check("derived qfq 600519 last close == quote price (qfq anchored at latest bar)", not r6["basis_mismatch"]
              and r6["last_close"] == qm["600519"]["price"], f"{r6['last_close']} vs {qm['600519']['price']}")
    rr, ok_r = em.research_reports("600519", (today - dt.timedelta(days=180)).isoformat(), today.isoformat())
    check("research_reports 600519 ok, rows have date/org", ok_r and len(rr) > 0 and all(r["date"] and r["org"] for r in rr), f"rows={len(rr)}")
    syn_x = {"reports": {"window": ["2026-04-01", "2026-10-01"], "n_reports": 5, "with_target": [
        {"org_code": "A", "org": "A", "date": "2026-09-01", "target_hi": 10, "target_lo": 10},
        {"org_code": "A", "org": "A", "date": "2026-05-01", "target_hi": 50, "target_lo": 50},
        {"org_code": "B", "org": "B", "date": "2026-08-01", "target_hi": 14, "target_lo": 12},
        {"org_code": "C", "org": "C", "date": "2026-07-01", "target_hi": 20, "target_lo": None},
        {"org_code": "D", "org": "D", "date": "2026-04-15", "target_hi": 99, "target_lo": 99}]},
        "bonus": [{"ex_date": "2026-05-20", "bonus_it_per10": 4, "cash_per10": 1}]}
    tm = cards.target_median(syn_x, 10, "2026-10-01")
    check("target_median: latest per institution, range midpoint, pre-split report dropped", tm["median"] == 13 and tm["n"] == 3
          and len(tm["dropped"]) == 2 and tm["vs_price_pct"] == 30.0, json.dumps({k: tm[k] for k in ("median", "n", "min", "max")}))
    syn_x["reports"]["with_target"] = syn_x["reports"]["with_target"][:2]
    check("target_median: < 3 institutions => no median", cards.target_median(syn_x, 10, "2026-10-01")["median"] is None)

    print("\n[k] tracked holders in the top-10 float holders (display only)")
    from . import holders
    HR = holders.HOLDER_RULES
    check("holders.match_rule: HKSCC exact, 代理人 (H-share nominee) excluded",
          holders.match_rule(HR["hkscc"], "香港中央结算有限公司") and not holders.match_rule(HR["hkscc"], "香港中央结算(代理人)有限公司"))
    check("holders.match_rule: 社保 prefix + manager-run portfolio, 划转 excluded",
          holders.match_rule(HR["ssf"], "全国社保基金一一八组合") and holders.match_rule(HR["ssf"], "华夏基金管理有限公司-社保基金四二二组合")
          and not holders.match_rule(HR["ssf"], "博时基金管理有限公司-社保基金划转三零零一组合")
          and not holders.match_rule(HR["pension"], "华泰优逸五号混合型养老金产品-中国银行股份有限公司"))
    fp, pp_ = holders.plan_periods(dt.date(2026, 10, 23))
    check("holders.plan_periods 2026-10-23: 5 full periods to 2026-06-30, 2026-09-30 partial",
          fp == ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"] and pp_ == ["2026-09-30"], f"{fp} {pp_}")
    P5 = fp
    rep = set(P5)
    E = lambda p, s, f=1.0: {"period": p, "shares": s, "float_pct": f, "n": 1}
    d_new = holders.derive([E("2026-03-31", 100), E("2026-06-30", 120)], P5, rep, [])
    d_out = holders.derive([E("2025-06-30", 100), E("2025-09-30", 90)], P5, rep, [])
    d_io = holders.derive([E("2025-12-31", 100)], P5, rep, [])
    d_up = holders.derive([E(p, s, f) for p, s, f in zip(P5, (100, 110, 105, 120, 130), (1.0, 1.1, 1.05, 1.2, 1.3))], P5, rep, [])
    check("holders.derive: 新进前十 / 跌出前十 / 期间进出 / 增持 with quarter counts",
          d_new["status"] == "new" and d_new["in_latest"] and d_new["first_seen"] == "2026-03-31"
          and d_out["status"] == "out" and not d_out["in_latest"] and d_io["status"] == "inout"
          and d_up["status"] == "up" and d_up["net_shares"] == 30 and abs(d_up["net_pp"] - 0.3) < 1e-9
          and d_up["n_up"] == 3 and d_up["n_down"] == 1, f"{d_new['status']} {d_out['status']} {d_io['status']} {d_up['status']}")
    # 送转 10送5 (ex-date 2025-11-10): 1000 shares at 2025-06-30 = 1500 on the latest basis; 1400 at the latest = 减持
    ev = [{"ex_date": "2025-11-10", "bonus_it_per10": 5, "cash_per10": 1}]
    hs = [E("2025-06-30", 1000, 2.0), E("2026-06-30", 1400, 1.9)]
    d_b = holders.derive(hs, P5, rep, ev)
    d_nb = holders.derive(hs, P5, rep, [])
    d_fp = holders.derive(hs, P5, rep, ev, bonus_ok=False)
    check("holders.derive 送转: raw +400 shares is 减持 after the 1.5x bonus adjustment; float_pct fallback also 减持",
          d_b["status"] == "down" and d_b["net_shares"] == -100 and d_b["adj"] == 1.5 and d_nb["status"] == "up"
          and d_fp["status"] == "down" and d_fp["basis"] == "float_pct", f"{d_b['status']} {d_b['net_shares']} {d_nb['status']} {d_fp['status']}")
    check("holders.bonus_factor: only ex-dates in (after, upto]", holders.bonus_factor(ev, "2025-06-30", "2026-06-30") == 1.5
          and holders.bonus_factor(ev, "2025-12-31", "2026-06-30") == 1.0)
    d_late = holders.derive([E("2026-03-31", 50), E("2026-06-30", 60)], P5, {"2026-03-31", "2026-06-30"}, [])
    d_part = holders.derive([E("2026-06-30", 60), E("2026-09-30", 40)], P5, rep | {"2026-09-30"}, [], partial=["2026-09-30"])
    check("holders.derive: listed late => base = first reported period; partial period kept separate",
          d_late["status"] == "up" and d_late["base"] == "2026-03-31" and d_part["status"] == "new"
          and d_part["partial"]["status"] == "down", f"{d_late['status']} {d_late['base']} {d_part['partial']}")
    hdoc, hpath = holders.load_latest()
    if hdoc:
        its = hdoc.get("items") or {}
        n_hk = sum(1 for v in its.values() if any(e["period"] == hdoc["periods"][-1] for e in v.get("hkscc", [])))
        check("holders file: 5 full periods, complete, HKSCC in top 10 at > 1500 companies at the latest period",
              len(hdoc["periods"]) == 5 and hdoc.get("complete") and n_hk > 1500, f"{hpath[-28:]} n={n_hk}")
        # quarter steps without 送转: our share-count sign vs Eastmoney's own HOLDNUM_CHANGE_NAME of the later period
        sg = {"增加": 1, "减少": -1, "不变": 0}
        tot = agree_n = 0
        for code, v in its.items():
            if code in (hdoc.get("bonus") or {}):
                continue
            for k, hist in v.items():
                H_ = {e["period"]: e for e in hist}
                for a, b in zip(hdoc["periods"], hdoc["periods"][1:]):
                    if a in H_ and b in H_ and H_[b].get("n") == 1 and H_[a].get("n") == 1 and H_[b].get("status") in sg:
                        d = H_[b]["shares"] - H_[a]["shares"]
                        tot += 1
                        agree_n += (d > 0) - (d < 0) == sg[H_[b]["status"]]
        check("holders: quarter-step direction agrees with Eastmoney HOLDNUM_CHANGE_NAME >= 98%", tot > 1000 and agree_n / tot >= 0.98,
              f"{agree_n}/{tot}")
        rows000001, ok1 = em.dc_report("RPT_F10_EH_FREEHOLDERS", "SECURITY_CODE,END_DATE,HOLDER_NAME,HOLD_NUM",
                                       f"(SECURITY_CODE=\"000001\")(END_DATE='{hdoc['periods'][-1]}')", "HOLDER_RANK", "1",
                                       ttl=7, label="smoke holders 000001")
        per = next((r for r in rows000001 if r["HOLDER_NAME"] == "香港中央结算有限公司"), None)
        mine = next((e for e in its.get("000001", {}).get("hkscc", []) if e["period"] == hdoc["periods"][-1]), None)
        check("holders: 000001 HKSCC market-wide query == per-code F10 query", ok1 and per and mine
              and em.num(per["HOLD_NUM"]) == mine["shares"], f"{per and per['HOLD_NUM']} vs {mine and mine['shares']}")
    else:
        check("holders file present (run python -m cn holders)", False)

    print("\n[l] cash-flow valuation reference (display only): TTM, engine mirror, reverse DCF, inputs file")
    from . import valuation as V
    fys, ytd, ago = V.plan_periods("2026-06-30")
    check("valuation.plan_periods 2026-06-30: FY2021..FY2025 + YTD 2026-06-30 vs 2025-06-30",
          fys == [f"{y}-12-31" for y in range(2021, 2026)] and ytd == "2026-06-30" and ago == "2025-06-30", f"{fys} {ytd} {ago}")
    check("valuation.plan_periods 2025-12-31: no YTD period", V.plan_periods("2025-12-31")[1:] == (None, None))
    check("valuation.ttm: FY + YTD - YTD a year ago; fiscal-year fallback when a YTD value is missing",
          V.ttm(100, 60, 50) == (110, "ttm") and V.ttm(100, None, 50) == (100, "fy") and V.ttm(None, 1, 1) == (None, None))
    # constant growth (g0 == gT) => EV equals the Gordon value FCF_1 / (r - g) exactly
    e = V.dcf_engine(1000.0, 0.1, 0.03, 0.03, 0.08, 50.0, 10.0)
    close("dcf_engine: g0 == gT reduces to Gordon FCF1 / (r - g) (+ net cash) per share", e["vps"], (1000 * 1.03 * 0.1 / 0.05 + 50) / 10, 1e-9)
    e2 = V.dcf_engine(1000.0, 0.1, 0.25, 0.02, 0.07, -30.0, 4.0)
    rev, pv = 1000.0, 0.0
    for t in range(1, 11):
        rev *= 1 + 0.25 + (0.02 - 0.25) * (t - 1) / 9
        pv += rev * 0.1 / 1.07 ** t
    close("dcf_engine: 10-year linear fade + terminal value, hand-rolled loop", e2["vps"], (pv + rev * 0.1 * 1.02 / 0.05 / 1.07 ** 10 - 30) / 4, 1e-12)
    rv = V.dcf_reverse(1000.0, 0.1, 0.02, 0.07, -30.0, 4.0, e2["vps"])
    check("dcf_reverse: recovers g0 = 25% from its own price", rv["status"] == "ok" and abs(rv["g"] - 0.25) < 1e-9, str(rv))
    check("dcf_reverse: no solution flagged (price above value at +100%, below at -30%)",
          V.dcf_reverse(1000.0, 0.1, 0.02, 0.07, 0, 4.0, 1e9)["status"] == "above" and V.dcf_reverse(1000.0, 0.1, 0.02, 0.07, 0, 4.0, 1e-9)["status"] == "below")
    mr = V.margin_required(1000.0, 0.25, 0.02, 0.07, -30.0, 4.0, e2["vps"])
    close("margin_required: implied FCF margin at base growth == 10%", mr, 0.1, 1e-9)
    Pp = {"rf": 1.6, **V.DCF_DEFAULTS}
    syn = {"r0": 1000.0, "m": [0.05, 0.06, 0.08, 0.10, 0.12], "g3": 75.0, "cash": 100.0, "debt": 40.0, "sh": 10.0}
    o = V.dcf_case(syn, 50.0, False, None, 300.0, Pp)
    check("dcf_case: 3-year average margin, 3y CAGR capped to +40%, gT clamped to rf, bear < base < bull",
          o["status"] == "ok" and abs(o["m0"] - 0.1) < 1e-12 and o["g0"] == 40 and o["capped"] and o["gT"] == 1.6
          and o["vps"]["bear"] < o["vps"]["base"] < o["vps"]["bull"], json.dumps({k: o[k] for k in ("m0", "g0", "gT", "vps")}))
    oc = V.dcf_case(syn, 50.0, True, 12.0, 300.0, Pp)
    check("dcf_case: cyclical uses the 5-year average; consensus growth wins over the 3y CAGR",
          abs(oc["m0"] - 0.082) < 1e-12 and oc["gsrc"] == "cons" and oc["g0"] == 12.0, f"{oc['m0']} {oc['gsrc']}")
    on = V.dcf_case({**syn, "m": [None, None, -0.2, -0.1, 0.05], "g3": None}, 50.0, False, None, 30.0, {**Pp, "basis": "avg3"})
    check("dcf_case (basis avg3): base margin <= 0 => negfcf (no scenarios, required margin only); YTD growth fallback",
          on["status"] == "negfcf" and "vps" not in on and on["m_req"] > 0 and on["gsrc"] == "yoy" and on["g0"] == 30.0, str(on.get("m_req")))
    check("dcf_case: r - gT <= 0.5pt => not computed; missing balance sheet / rf => N/A",
          V.dcf_case(syn, 50.0, False, None, 30.0, {**Pp, "erp": 0.3})["why"] == "rg"
          and V.dcf_case({**syn, "cash": None}, 50.0, False, None, 30.0, Pp)["why"] == "bal"
          and V.dcf_case(syn, 50.0, False, None, 30.0, {**Pp, "rf": None})["why"] == "rf")
    cg = V.consensus_growth({"org_num": 5, "eps": [{"year": 2025, "kind": "actual", "eps": 1.0}, {"year": 2026, "kind": "estimate", "eps": 1.0},
                                                  {"year": 2027, "kind": "estimate", "eps": 1.2}, {"year": 2028, "kind": "estimate", "eps": 1.44}]}, "2026-06-30")
    check("consensus_growth: 2026E -> 2028E CAGR 20%; < 3 institutions => None",
          abs(cg["g"] - 20.0) < 1e-9 and cg["y0"] == 2026 and V.consensus_growth({"org_num": 2, "eps": []}, "2026-06-30")["g"] is None, str(cg))
    vdoc, vpath = V.load_latest()
    if vdoc:
        its = vdoc.get("items") or {}
        n = len(its)
        n_ok = sum(1 for it in its.values() if (V.compact(it, vdoc, {}) or {}).get("r0") is not None and it.get("bal"))
        check("valuation file: complete, rf (China 10y) present + field check ok, >= 95% of codes with TTM revenue + balance sheet",
              vdoc.get("complete") and vdoc["rf"].get("value") is not None and vdoc["rf"].get("check") == "ok" and n > 1000 and n_ok / n >= 0.95,
              f"{os.path.basename(vpath)} rf={vdoc['rf'].get('value')} ({vdoc['rf'].get('date')}) ok={n_ok}/{n}")
        code = "300308" if "300308" in its else next(iter(its))
        f10, okf = em.dc_report("RPT_F10_FINANCE_GCASHFLOW", "SECUCODE,REPORT_DATE,NETCASH_OPERATE,CONSTRUCT_LONG_ASSET",
                                f"(SECUCODE=\"{em.secucode(code)}\")(REPORT_DATE='{vdoc['fiscal_years'][-1]}')", ttl=7, label=f"smoke f10 cf {code}")
        a = f10[0] if f10 else {}
        it = its[code]
        check(f"valuation: {code} FY OCF / capex (market-wide DMSK) == per-code F10 RPT_F10_FINANCE_GCASHFLOW",
              okf and a and em.num(a.get("NETCASH_OPERATE")) == it["ocf"][vdoc["fiscal_years"][-1]]
              and em.num(a.get("CONSTRUCT_LONG_ASSET")) == it["capex"][vdoc["fiscal_years"][-1]],
              f"{a.get('NETCASH_OPERATE')} / {a.get('CONSTRUCT_LONG_ASSET')}")
    else:
        check("valuation file present (run python -m cn valuation)", False)

    print("\n[m] FCF margin basis (FCF 利润率口径): auto / avg3 / fy / ttm / ramp, one-off guard, ramp math, fallback")
    CB = V.choose_basis
    mr_ = [0.02, 0.03, 0.04, 0.06, 0.08]          # rising FY-2 < FY-1 < FY0, avg3 = 0.06
    mn_ = [-0.3, -0.5, -0.4, -0.2, -0.05]         # avg3 = -0.2167 <= 0, FY0 > FY-1 (improving)
    b = CB(mr_, 0.15, False, "auto")
    check("choose_basis auto: TTM exists, not guarded, TTM > avg3, FY-2 < FY-1 < FY0 => ttm",
          b["used"] == "ttm" and b["m0"] == 0.15 and b["m_end"] == 0.15 and b["why"] == "auto_ttm" and abs(b["avg"] - 0.06) < 1e-12 and b["path"] is None, str(b))
    check("choose_basis auto: TTM <= avg3 / FY not rising / no TTM => avg3 (ttm_block low / trend / missing, ramp_block avg_pos)",
          [CB(mr_, t, False, "auto")["ttm_block"] for t in (0.05, None)] == ["low", "missing"]
          and CB([0.02, 0.03, 0.09, 0.06, 0.08], 0.15, False, "auto")["ttm_block"] == "trend"
          and CB([0.02, 0.03, None, 0.06, 0.08], 0.15, False, "auto")["ttm_block"] == "trend"
          and all(CB(mr_, t, False, "auto")["used"] == "avg3" and CB(mr_, t, False, "auto")["ramp_block"] == "avg_pos" for t in (0.05, None)))
    b = CB(mn_, None, False, "auto")
    check("choose_basis auto: avg3 <= 0 and FY0 > FY-1 => ramp from FY0 (no TTM); avg3 <= 0 but not improving => avg3 (negfcf)",
          b["used"] == "ramp" and b["why"] == "auto_ramp" and b["start"] == -0.05 and b["start_src"] == "fy" and b["m_end"] == 0.15
          and CB([-0.3, -0.5, -0.1, -0.2, -0.25], None, False, "auto")["used"] == "avg3"
          and CB([-0.3, -0.5, -0.1, -0.2, -0.25], None, False, "auto")["ramp_block"] == "not_improving", str(b))
    b = CB([-0.3, -0.5, -0.1, -0.2, -0.05], 0.02, False, "auto")
    check("choose_basis auto: ramp starts from TTM when TTM is usable (TTM 2% > avg3 but FY-2 < FY-1 < FY0 fails => ramp from TTM)",
          b["used"] == "ramp" and b["start"] == 0.02 and b["start_src"] == "ttm" and b["ttm_block"] == "trend", str(b))
    check("choose_basis auto: cyclical => multi-year average, never TTM or ramp (even when both rules would fire)",
          all(CB(m_, t, True, "auto")["used"] == "avg3" and CB(m_, t, True, "auto")["why"] == "auto_cyc" for m_, t in ((mr_, 0.15), (mn_, None), (mn_, 0.5)))
          and abs(CB(mr_, 0.15, True, "auto")["m0"] - 0.046) < 1e-12)
    check("choose_basis fy / ttm / ramp / avg3: chosen basis used; missing => avg3 fallback (fb_fy / fb_ttm / fb_ramp)",
          CB(mr_, 0.15, False, "fy")["m0"] == 0.08 and CB(mr_, 0.15, True, "ttm")["m0"] == 0.15 and CB(mr_, 0.15, False, "avg3")["why"] == "user"
          and CB(mr_, None, False, "ramp", N=4, T=0.2)["used"] == "ramp" and CB(mr_, None, False, "ramp", N=4, T=0.2)["path"][3] == 0.2
          and CB([0.02, 0.03, 0.04, 0.06, None], 0.15, False, "fy")["why"] == "fb_fy"
          and abs(CB([0.02, 0.03, 0.04, 0.06, None], 0.15, False, "fy")["m0"] - 0.05) < 1e-12
          and CB(mr_, None, False, "ttm")["why"] == "fb_ttm" and abs(CB(mr_, None, False, "ttm")["m0"] - 0.06) < 1e-12
          and CB([0.02, 0.03, 0.04, 0.06, None], None, False, "ramp")["why"] == "fb_ramp"
          and CB([0.02, 0.03, 0.04, 0.06, None], 0.1, False, "ramp", guard=[{"k": "a"}])["why"] == "fb_ramp")
    check("choose_basis avg3 == base_margin (previous behaviour), cyclical and non-cyclical; margin_basis alias",
          all(CB(m_, 0.3, cy, "avg3")["m0"] == V.base_margin(m_, cy)[0] for m_ in (mr_, mn_, syn["m"], [None, 0.1, None, -0.2, 0.05]) for cy in (False, True))
          and V.margin_basis(mr_, 0.15, False, "auto")["used"] == "ttm")
    # one-off guard (a) TTM revenue > 1.5 x FY revenue, (b) YTD revenue > FY revenue, (c) consensus EPS next year < 0.7 x this year
    dg = {"r0": 100.0, "rb": "ttm", "ry": 90.0, "rh": 50.0}
    ga = V.one_off_guard({**dg, "r0": 140.0})
    gb = V.one_off_guard({**dg, "rh": 95.0})
    cons_ = lambda e0, e1, org=5: {"org_num": org, "eps": [{"year": 2026, "kind": "estimate", "eps": e0}, {"year": 2027, "kind": "estimate", "eps": e1}]}
    dr_ = V.eps_drop(cons_(7.68, 1.25), "2026-06-30")
    check("one_off_guard: none / (a) TTM rev 140 > 1.5 x 90 only on a TTM basis / (b) YTD 95 > FY 90 / (c) EPS 7.68 -> 1.25",
          V.one_off_guard(dg) == [] and [g["k"] for g in V.one_off_guard({**dg, "r0": 136.0})] == ["a"] and [g["k"] for g in ga] == ["a"]
          and V.one_off_guard({**dg, "r0": 140.0, "rb": "fy"}) == [] and [g["k"] for g in gb] == ["b"]
          and [g["k"] for g in V.one_off_guard(dg, dr_)] == ["c"] and V.one_off_guard(dg, None) == [], f"{ga} {gb} {dr_}")
    check("eps_drop: hit when next year < 0.7 x current-year estimate; None with < 3 institutions or an estimate <= 0; 0.75 ratio => no hit",
          dr_["hit"] and dr_["y0"] == 2026 and dr_["y1"] == 2027 and V.eps_drop(cons_(7.68, 1.25, 2), "2026-06-30") is None
          and V.eps_drop(cons_(1.0, -0.1), "2026-06-30") is None and V.eps_drop(cons_(1.0, 0.75), "2026-06-30")["hit"] is False
          and V.eps_drop(cons_(1.0, 0.69), "2026-06-30")["hit"] is True, str(dr_))
    b = CB(mr_, 0.15, False, "auto", ga)
    check("guard blocks TTM under auto (ttm_block guard) and as the ramp start (start = FY0); under basis ttm it is still used and flagged",
          b["used"] == "avg3" and b["ttm_block"] == "guard" and CB(mn_, 0.5, False, "auto", ga)["start_src"] == "fy"
          and CB(mn_, 0.5, False, "auto", ga)["start"] == -0.05 and CB(mr_, 0.15, False, "ttm", ga)["used"] == "ttm"
          and CB(mr_, 0.15, False, "ttm", ga)["guard"] == ga, str(b))
    # ramp math
    pth = V.margin_path(-0.05, 0.15, 5)
    check("margin_path: m_t = start + (T - start) x min(t / N, 1): -5% -> 15% in 5 years = -1, 3, 7, 11, 15, 15 ... 15%",
          all(abs(a_ - b_) < 1e-12 for a_, b_ in zip(pth, [-0.01, 0.03, 0.07, 0.11, 0.15] + [0.15] * 5)) and len(pth) == 10
          and abs(V.margin_path(0.0, 0.1, 10)[4] - 0.05) < 1e-12 and V.margin_path(0.2, 0.1, 1) == [0.1] * 10, str(pth))
    rev, pv = 1000.0, 0.0
    for t in range(1, 11):
        rev *= 1 + 0.2 + (0.02 - 0.2) * (t - 1) / 9
        pv += rev * pth[t - 1] / 1.08 ** t
    ep = V.dcf_engine(1000.0, pth, 0.2, 0.02, 0.08, 50.0, 10.0)
    close("dcf_engine with a ramp path: hand-rolled loop, terminal uses m_10", ep["vps"], (pv + rev * 0.15 * 1.02 / 0.06 / 1.08 ** 10 + 50) / 10, 1e-12)
    rq = V.ramp_required(1000.0, -0.05, 5, 0.2, 0.02, 0.08, 50.0, 10.0, ep["vps"])
    rvp = V.dcf_reverse(1000.0, pth, 0.02, 0.08, 50.0, 10.0, ep["vps"])
    check("ramp_required recovers T = 15% from its own price; reverse DCF with the path fixed recovers g0 = 20%",
          abs(rq - 0.15) < 1e-9 and rvp["status"] == "ok" and abs(rvp["g"] - 0.2) < 1e-9, f"{rq} {rvp}")
    sr = {**syn, "m": mn_, "mt": None, "g3": 20.0}
    orp = V.dcf_case(sr, 50.0, False, None, 30.0, Pp)
    gT_ = min(Pp["gT"], Pp["rf"]) / 100
    rb_ = (Pp["rf"] + Pp["erp"]) / 100
    exp = {k: V.dcf_engine(1000.0, V.margin_path(-0.05, 0.15 * f, 5), g / 100, gT_, r_ / 100, 60.0, 10.0)["vps"]
           for k, f, g, r_ in (("bear", 0.8, 14.0, rb_ * 100 + 1), ("base", 1.0, 20.0, rb_ * 100), ("bull", 1.2, 26.0, rb_ * 100 - 1))}
    check("dcf_case auto => ramp (default N 5, T 15%): ok (was negfcf under avg3); bear / bull = T x (1 -/+ 20%), r / g0 scenarios unchanged",
          orp["status"] == "ok" and orp["mb"]["used"] == "ramp" and V.dcf_case(sr, 50.0, False, None, 30.0, {**Pp, "basis": "avg3"})["status"] == "negfcf"
          and all(abs(orp["vps"][k] - exp[k]) < 1e-9 for k in exp) and abs(orp["path"][4] - 0.15) < 1e-12
          and abs(orp["m_req"] - V.ramp_required(1000.0, -0.05, 5, 0.2, gT_, rb_, 60.0, 10.0, 50.0)) < 1e-12, json.dumps(orp.get("vps")))
    on_ = V.dcf_case(sr, 50.0, False, None, 30.0, {**Pp, "rampT": -2.0})
    o3 = V.dcf_case(sr, 50.0, False, None, 30.0, {**Pp, "basis": "ramp", "rampN": 3, "rampT": 10.0})
    check("dcf_case ramp: T <= 0 => negfcf with the required steady-state T; basis / N / T from P (per-company override)",
          on_["status"] == "negfcf" and "vps" not in on_ and abs(on_["m_req"] - orp["m_req"]) < 1e-12
          and o3["status"] == "ok" and abs(o3["path"][2] - 0.10) < 1e-12 and abs(o3["path"][0] - (-0.05 + 0.15 / 3)) < 1e-12)
    synn = {**syn, "m": [-0.3, -0.2, -0.1, -0.05, -0.02], "mt": 0.08}
    oa, ot = V.dcf_case(synn, 50.0, False, None, 30.0, {**Pp, "basis": "avg3"}), V.dcf_case(synn, 50.0, False, None, 30.0, Pp)
    check("dcf_case: negative-FCF classification uses the chosen basis (avg3 negfcf, auto -> TTM ok); defaults auto / N 5 / T 15%",
          oa["status"] == "negfcf" and ot["status"] == "ok" and ot["m0"] == 0.08 and ot["mb"]["why"] == "auto_ttm"
          and V.DCF_DEFAULTS["basis"] == "auto" and V.DCF_DEFAULTS["rampN"] == 5 and V.DCF_DEFAULTS["rampT"] == 15.0
          and V.dcf_case({**synn, "r0": 1000.0, "rb": "ttm", "ry": 500.0}, 50.0, False, None, 30.0, Pp)["mb"]["used"] == "ramp")
    doc_ = {"fiscal_years": [f"{y}-12-31" for y in range(2021, 2026)], "ytd": "2026-06-30", "ytd_ago": "2025-06-30"}
    it_ = {"rev": {"2025-12-31": 100.0, "2026-06-30": 60.0, "2025-06-30": 50.0}, "ocf": {"2025-12-31": 20.0, "2026-06-30": 9.0, "2025-06-30": 5.0},
           "capex": {"2025-12-31": 5.0, "2026-06-30": 3.0, "2025-06-30": 2.0}}
    c1 = V.compact(it_, doc_, {})
    c2 = V.compact({**it_, "capex": {"2025-12-31": 5.0}}, doc_, {})
    check("compact: TTM margin mt = (OCF - capex) / revenue on the same TTM basis (dropped when the bases differ); ry / rh guard inputs",
          c1["mt"] == round((24 - 6) / 110, 6) and c2["mt"] is None and c1["ry"] == 100 and c1["rh"] == 60, f"{c1['mt']} {c2['mt']} {c1.get('ry')} {c1.get('rh')}")
    vdoc_, _ = V.load_latest()
    from . import cards as CM
    cp_ = CM.latest_cards()
    if vdoc_ and cp_ and "688331" in (vdoc_.get("items") or {}):
        with open(cp_, encoding="utf-8") as f:
            cdoc_ = json.load(f)
        d_ = V.compact(vdoc_["items"]["688331"], vdoc_, {})
        dr = V.eps_drop(((cdoc_.get("cards") or {}).get("688331") or {}).get("consensus"), vdoc_["latest"])
        g_ = V.one_off_guard(d_, dr)
        b = CB(d_["m"], d_["mt"], False, "auto", g_)
        check("real data 688331 荣昌生物: auto => ramp (avg3 <= 0, FY2025 improving), guard (b) H1 revenue > FY2025 and (c) EPS drop, TTM not used, start = FY2025",
              b["used"] == "ramp" and b["why"] == "auto_ramp" and b["ttm_block"] == "guard" and {"b", "c"} <= {x["k"] for x in g_}
              and b["start_src"] == "fy" and b["start"] == d_["m"][-1] and b["avg"] <= 0 and d_["m"][-1] > d_["m"][-2],
              f"guard={[x['k'] for x in g_]} avg3={b['avg']:.4f} fy={d_['m'][-2]:.4f}->{d_['m'][-1]:.4f} ttm={d_['mt']} rh={d_['rh']} ry={d_['ry']} eps={dr}")
    else:
        check("real data 688331 present in the valuation + cards files", False)

    print("\n[n] valuation ledger: default parameters (auto basis, ramp, guard), change, attribution, conclusion, idempotent append")
    from . import ledger as LDG
    ledger_checks(LDG, V, syn)

    print("\n[o] forward tracking of the ledger tags (radar/forward.py evaluate, cn/forward.py adapter)")
    forward_checks(LDG)

    print("\n[p] full-market lookup (cn/build.py full_lookup) and value channel (cn/quality.py)")
    lookup_value_checks()
    dividend_checks()

    print("\n[robustness] failure handling")
    bad = em.financials("1900-01-01")
    check("financials(bad date) returns [] without raising", bad == [])
    bad2 = em.valuation_history("000000", 1)
    check("valuation_history(unknown code) returns [] without raising", bad2 == [])

    fails = [r for r in RESULTS if not r[1]]
    print(f"\nSUMMARY: {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed; http={em.STATS}")
    for name, _, detail in fails:
        print(f"  FAILED: {name} {detail}")
    return 0 if not fails else 1


def ledger_checks(LDG, V, syn):
    """估值台账 checks: records come from V.dcf_case with DCF_DEFAULTS (auto basis, ramp N 5 / T 15%, one-off guard);
    the attribution (sequential substitution through the mirror, margin path incl. ramp) must sum to the total."""
    import tempfile
    P0 = {"rf": 1.64, **V.DCF_DEFAULTS}
    ctx_ = {"date": "2026-10-21", "snap_file": "snap_T1.json", "val_file": "val_T1.json", "cards_file": None, "cards": {"000001"},
            "cons": {"000001": {"g": 12.0, "org": 5}}, "drops": {}, "P": P0}
    row_ = {"code": "000001", "name": "T", "industry": "X", "cyclical": False, "rev_yoy": 30.0, "price": 50.0, "label": "中性", "warn_n": 1,
            "dcf": {**syn, "mt": None}}
    mk = LDG.make_record

    def sums(ch, prev, cur):
        """|sum of contributions - total| and |sum of % contributions - base %| (ok -> ok)."""
        s = sum(a[1] for a in ch["attr"])
        return abs(s - (cur["base"] - prev["base"])), abs(sum(a[2] for a in ch["attr"]) - (cur["base"] / prev["base"] - 1) * 100)

    # (1) defaults: the record is dcf_case(DCF_DEFAULTS) - same numbers, auto basis
    cur_ = mk(ctx_, row_, "2026-10-21 17:00")
    o_ = V.dcf_case(row_["dcf"], 50.0, False, 12.0, 30.0, P0)
    check("ledger.make_record = dcf_case(DCF_DEFAULTS): base / bear / bull / implied g, basis auto -> avg3, rampN / rampT recorded",
          cur_["base"] == o_["vps"]["base"] and abs(cur_["bull"] - o_["vps"]["bull"]) < 1e-6 and cur_["basis"] == "avg3"
          and cur_["basis_why"] == "auto_avg" and cur_["rampN"] == 5 and cur_["rampT"] == 15.0 and cur_["gsrc"] == "cons"
          and cur_["implied_g_status"] == o_["rev"] and cur_["implied_g"] == (None if o_["rev_g"] is None else round(o_["rev_g"], 6))
          and cur_["implied_m"] == o_["m_req"], f"base={cur_['base']:.4f} basis={cur_['basis']} implied_g={cur_['implied_g_status']}")
    prev_ = mk(ctx_, row_, "2026-10-14 17:00", price=46.0, d={**syn, "mt": None, "r0": 900.0, "cash": 80.0},
               cons={"g": 10.0, "org": 4}, P={**P0, "rf": 1.80})
    ch = LDG.compute_chg(prev_, cur_)
    e1, e2 = sums(ch, prev_, cur_)
    check("ledger.compute_chg ok->ok: attribution order R0, margin path, g0, net cash & shares, params; sums to the total (1e-9)",
          ch["attr_of"] == "base" and [a[0] for a in ch["attr"]] == ["r0", "m0", "g0", "ncsh", "par"] and e1 < 1e-9 and e2 < 1e-9
          and abs(ch["base_pct"] - (cur_["base"] / prev_["base"] - 1) * 100) < 1e-9 and bool(ch["conclusion"]), f"err={e1:.1e} {e2:.1e}")
    # (2) ramp: auto -> ramp (avg3 <= 0, FY0 improving); start and r0 change, parameters change incl. ramp T
    mn = [-0.3, -0.5, -0.4, -0.2, -0.05]
    rr = {**row_, "dcf": {**syn, "m": mn, "mt": None, "g3": 20.0}}
    cr = mk(ctx_, rr, "c")
    pr = mk(ctx_, rr, "p", d={**rr["dcf"], "m": mn[:-1] + [-0.08], "r0": 950.0}, P={**P0, "rampT": 12.0, "erp": 5.5})
    chr_ = LDG.compute_chg(pr, cr)
    e1, e2 = sums(chr_, pr, cr)
    vr = V.dcf_engine(1000.0, V.margin_path(-0.05, 0.15, 5), 0.12, 0.0164, 0.0664, 60.0, 10.0)["vps"]
    check("ledger ramp record: basis ramp, start = FY0, path to T 15% in 5 years (base = dcf_engine with the path); ramp->ramp attribution sums (1e-9)",
          cr["basis"] == "ramp" and pr["basis"] == "ramp" and cr["m_start"] == -0.05 and cr["ramp_T"] == 0.15 and cr["ramp_N"] == 5
          and abs(cr["base"] - vr) < 1e-9 and cr["implied_m_kind"] == "T" and e1 < 1e-9 and e2 < 1e-9
          and all(abs(a[1]) > 1e-9 for a in chr_["attr"] if a[0] in ("r0", "m0", "par")), f"err={e1:.1e} attr={[a[0] for a in chr_['attr']]}")
    # (3) basis switch avg3 -> ttm (TTM 15% > avg3, FY rising) and ramp -> ttm: carried by the margin-path step, named in the text
    ct_ = mk(ctx_, {**row_, "dcf": {**syn, "mt": 0.15}}, "c")
    chs = LDG.compute_chg(cur_, ct_)
    e1, _ = sums(chs, cur_, ct_)
    crt = mk(ctx_, {**rr, "dcf": {**rr["dcf"], "mt": 0.08}}, "c")
    chs2 = LDG.compute_chg(cr, crt)
    e3, _ = sums(chs2, cr, crt)
    only_m = all(abs(a[1]) < 1e-12 for a in chs["attr"] if a[0] != "m0")
    check("ledger basis switch avg3->ttm and ramp->ttm: chg.basis set, whole change in the margin-path step, sums (1e-9), switch named",
          ct_["basis"] == "ttm" and chs["basis"] == ["avg3", "ttm"] and only_m and e1 < 1e-9 and crt["basis"] == "ttm"
          and chs2["basis"] == ["ramp", "ttm"] and e3 < 1e-9 and "TTM" in chs["conclusion"] and "TTM" in chs2["conclusion"],
          f"err={e1:.1e} {e3:.1e}")
    # (4) tags
    chp = LDG.compute_chg(mk(ctx_, row_, "x", P={**P0, "erp": 6.0}), cur_)
    chq = LDG.compute_chg(mk(ctx_, row_, "x", price=40.0), cur_)
    chT = LDG.compute_chg(mk(ctx_, rr, "x", P={**P0, "rampT": 20.0}), cr)
    check("ledger tags: ERP-only => parameter-driven; ramp T only => parameter-driven; price-only => price-driven (base change 0 < 2%)",
          chp["tag"] == "参数驱动" and chT["tag"] == "参数驱动" and chq["tag"] == "价格驱动" and chq["base_pct"] == 0
          and abs(chq["price_pct"] - 25) < 1e-9, f"{chp['base_pct']:.2f} {chT['base_pct']:.2f}")
    # (5) negfcf -> negfcf (required margin), negfcf -> ok (status change, not attributed)
    rn = {**row_, "dcf": {**syn, "m": [None, None, -0.1, -0.05, -0.08], "mt": None}}   # avg3 < 0, FY0 not improving => no ramp
    pn, cn_ = mk(ctx_, rn, "x", price=150.0), mk(ctx_, rn, "y")
    chn = LDG.compute_chg(pn, cn_)
    rok = mk(ctx_, {**rn, "dcf": {**rn["dcf"], "m": [None, None, 0.02, 0.03, 0.05]}}, "z")
    chs3 = LDG.compute_chg(cn_, rok)
    check("ledger negfcf: required-margin attribution (price first) sums; negfcf->ok = status change, no attribution, base stated",
          pn["status"] == "negfcf" and chn["attr_of"] == "m_req" and chn["attr"][0][0] == "price"
          and abs(sum(a[1] for a in chn["attr"]) - (cn_["implied_m"] - pn["implied_m"])) < 1e-12 and chn["tag"] == "价格驱动"
          and rok["status"] == "ok" and chs3["attr"] is None and chs3["tag"] == "状态变化" and chs3["base_pct"] is None
          and f"{rok['base']:.2f}" in chs3["conclusion"], f"{pn['status']}->{rok['status']}")
    # (6) one-off guard (c) for cards: an EPS drop blocks TTM under auto
    drop = {"y0": 2026, "y1": 2027, "e0": 2.0, "e1": 1.0, "org": 5, "hit": True}
    cg_ = mk({**ctx_, "drops": {"000001": drop}}, {**row_, "dcf": {**syn, "mt": 0.15}}, "g")
    check("ledger guard (c): card with an EPS drop -> guard ['c'], TTM blocked (basis avg3, block 'guard'); same row without drop -> ttm",
          cg_["guard"] == ["c"] and cg_["basis"] == "avg3" and cg_["basis_block"] == "guard" and ct_["basis"] == "ttm", str(cg_["guard"]))
    # (7) synthetic attribution-sum sweep: 200 random pairs incl. basis switches (avg3 / ttm / ramp), all factors moving
    import random
    rnd = random.Random(7)
    worst, n_sw, n_ok = 0.0, 0, 0
    for _ in range(200):
        def one():
            m = [rnd.uniform(-0.3, 0.3) for _ in range(5)]
            d = {"r0": rnd.uniform(500, 2000), "m": m, "mt": rnd.choice([None, rnd.uniform(-0.1, 0.4)]), "g3": rnd.uniform(-20, 60),
                 "cash": rnd.uniform(0, 300), "debt": rnd.uniform(0, 200), "sh": rnd.uniform(5, 20)}
            P = {**P0, "rf": rnd.uniform(1.2, 2.5), "erp": rnd.uniform(4, 7), "rampN": rnd.randint(2, 8), "rampT": rnd.uniform(8, 25)}
            return mk(ctx_, {**row_, "dcf": d}, "r", d=d, price=rnd.uniform(10, 100), P=P)
        a, b = one(), one()
        if a["status"] != "ok" or b["status"] != "ok":
            continue
        n_ok += 1
        n_sw += a["basis"] != b["basis"]
        c_ = LDG.compute_chg(a, b)
        worst = max(worst, abs(sum(x[1] for x in c_["attr"]) - (b["base"] - a["base"])) / max(abs(b["base"] - a["base"]), abs(a["base"]), 1e-9))
    check("ledger attribution sweep: random ok->ok pairs (incl. basis switches) sum to the total within 1e-9 relative",
          n_ok >= 50 and n_sw >= 10 and worst < 1e-9, f"pairs={n_ok} switches={n_sw} worst={worst:.1e}")
    # (7b) rule-based tags (TAG_RULES) on synthetic compact records; boundaries inclusive as documented
    T = lambda **k: [t[1] for t in LDG.tags(k)]
    base = dict(s="ok", p=10.0, lo=8.0, b=12.0, hi=16.0, gp=0.2)
    chg_ok = [T(**base, ch=1, bp=v)[0] for v in (15.0, 14.9, 5.0, 2.0, 1.99, -1.99, -2.0, -5.0, -15.0)]
    check("ledger tag rules: 变化幅度 thresholds ±15 / ±5 / ±2 (inclusive)",
          chg_ok == ["大幅上调", "上调", "上调", "小幅变化", "基本不变", "基本不变", "小幅变化", "下调", "大幅下调"]
          and T(s="negfcf", ch=1, mpp=5.0) == ["需要利润率 大幅上升"] and T(s="negfcf", ch=1, mpp=-2.0) == ["需要利润率 下降"]
          and T(s="negfcf", ch=1, mpp=1.9) == [] and T(**base, bp=30.0)[0] == "现价低于基准", str(chg_ok))
    pos = [T(**{**base, "gp": g})[0] for g in (0.5, 0.15, 0.149, -0.149, -0.15, -0.5)]
    check("ledger tag rules: 估值位置 ±50% / ±15% + 现价低于熊情景 / 现价高于牛情景; status negfcf => no position tag",
          pos == ["现价远低于基准", "现价低于基准", "接近基准", "接近基准", "现价高于基准", "现价远高于基准"]
          and "现价低于熊情景" in T(**{**base, "p": 7.0}) and "现价高于牛情景" in T(**{**base, "p": 17.0, "gp": -0.3})
          and T(s="negfcf", p=10.0, gp=0.2) == [], str(pos))
    cmb = lambda **k: [x for x in T(**base, ch=1, **k) if x not in ("上调", "大幅上调", "下调", "大幅下调", "小幅变化", "基本不变", "现价低于基准")]
    check("ledger tag rules: 组合信号 (fund up / price lead / fund weak by r0-m0-g0 only / cons ±3pt / gap ±10pp / basis / label / warn)",
          cmb(bp=5.0, pp=2.0) == ["基本面改善·价格未跟"] and cmb(bp=5.0, pp=2.1) == [] and cmb(bp=2.0, pp=10.0) == ["价格先行"]
          and cmb(bp=-5.0, ak="m0") == ["基本面转弱"] and cmb(bp=-5.0, ak="par") == [] and cmb(bp=0, cgp=3.0) == ["预期上修"]
          and cmb(bp=0, cgp=-3.0) == ["预期下修"] and cmb(bp=0, gpp=-10.0) == ["差距收窄"] and cmb(bp=0, gpp=10.0) == ["差距扩大"]
          and cmb(bp=0, gpp=9.9) == [] and cmb(bp=0, bsc=1, lbc=1, wnd=-1) == ["口径切换", "标签变化", "风险门禁变化"]
          and cmb(bp=5.0, pp=0.0) == ["基本面改善·价格未跟"] and T(**base, bp=5.0, pp=0.0) == ["现价低于基准"],
          str(cmb(bp=-5.0, ak="m0")))
    check("ledger tag rules: 注意 tags (guard / capped / ramp / cyclical / base <= 0); tag_input from a record with chg; run() stores tags",
          T(s="ok", p=10.0, b=-1.0, gp=-1.1, gd="ab", cp=1, mb="ramp", cy=1)[-5:] == ["一次性收入", "增速截断", "爬坡假设", "周期", "估值≤0"]
          and LDG.tag_input({**cur_, "chg": ch})["ch"] == 1 and LDG.tag_input({**cur_, "chg": ch}).get("ak") in ("r0", "m0", "g0", "ncsh", "par")
          and "ch" not in LDG.tag_input({**cur_, "chg": None}))
    # (8) append: idempotent, append-only, first-record text
    td = tempfile.mkdtemp(prefix="cnledger_")
    tp = os.path.join(td, "t.jsonl")
    try:
        ctx_t = {**ctx_, "rows": [row_, {**rn, "code": "000002"}]}
        a1 = LDG.run(tp, ctx_t, "2026-10-21 17:00")
        lines1 = open(tp, encoding="utf-8").read()
        a2 = LDG.run(tp, ctx_t, "2026-10-21 18:00")
        a3 = LDG.run(tp, {**ctx_t, "snap_file": "snap_T2.json"}, "2026-10-28 17:00")
        lines3 = open(tp, encoding="utf-8").read()
        recs = LDG.load(tp)
        check("ledger.run: idempotent per (code, snap, val) (2nd run appends 0), append-only, first record => chg None + first-record text",
              a1[0] == 2 and a2[0] == 0 and a2[1] == 2 and a3[0] == 2 and lines3.startswith(lines1) and len(recs) == 4
              and recs[0]["chg"] is None and recs[0]["conclusion"] == LDG.first_txt(recs[0]) and recs[2]["chg"] is not None
              and recs[2]["tags"] == [t[1] for t in LDG.tags(LDG.tag_input(recs[2]))] and "基本不变" in recs[2]["tags"],
              f"{a1[:2]} {a2[:2]} {a3[:2]}")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    # (9) the real ledger file: unique keys, new logic (basis + ramp params on every record), base == mirror(record)
    real = LDG.load()
    if real:
        ks = [(x["code"], x["snap_file"], x["val_file"]) for x in real]
        oks = [x for x in real if x["status"] == "ok"]
        dev = max((abs(LDG._vps(x) - x["base"]) / abs(x["base"]) for x in oks if x["base"]), default=0.0)
        bas = collections.Counter(x["basis"] for x in real)
        check("ledger file: (code, snap, val) unique, >= 1000 records, new logic (basis, rampN / rampT, guard on every record), "
              "base == dcf_engine(record inputs, margin path)",
              len(set(ks)) == len(ks) and len(real) >= 1000 and all(x.get("date") and x.get("status") and "rampN" in x and "guard" in x for x in real)
              and bas.get("ramp", 0) > 0 and bas.get("ttm", 0) > 0 and dev < 1e-12,
              f"records={len(real)} runs={len(LDG._runs(real))} basis={dict(bas)} max_rel_dev={dev:.1e}")
        r688 = next((x for x in reversed(real) if x["code"] == "688331"), None)
        check("ledger file 688331: ramp (start = FY0) with guard a / b / c, status ok",
              r688 is not None and r688["basis"] == "ramp" and r688["start_src"] == "fy" and {"b", "c"} <= set(r688["guard"])
              and r688["status"] == "ok", str(r688 and (r688["basis"], r688["guard"])))
    else:
        check("ledger file present (run python -m cn ledger)", False)


def forward_checks(LDG):
    """前向跟踪: horizon windows, excess vs the same-date median, split guard, no samples before a horizon has elapsed,
    and the real ledger adapter (tags recomputed = the stored tags)."""
    import datetime as dt
    from radar import forward as F
    from . import forward as CF
    d0 = dt.date(2026, 1, 5)
    ser = {}
    for i in range(24):
        p, sh, pts = 10.0, 1e8, []
        for w in range(15):
            if i == 0 and w == 6:
                sh *= 2.0                                   # 10送10-like: unadjusted price would halve
                p /= 2
            pts.append({"d": (d0 + dt.timedelta(days=7 * w)).isoformat(), "p": p, "sh": sh, "grp": "g%d" % (i % 2),
                        "tags": ["A"] if i < 12 else ["B"]})
            p *= 1.02 if i < 12 else 0.99
        ser["%06d" % i] = pts
    res = F.evaluate(ser)
    a1 = {a["tag"]: a for a in res["agg"] if a["h"] == 1}
    a4 = {a["tag"]: a for a in res["agg"] if a["h"] == 4}
    ex1 = res["excluded"].get("1", {})
    check("forward: 1w / 4w samples, excess = return - same-date median (A +2%/w vs B -1%/w), split guard excludes the one-week sample "
          "across the share change, 26 / 52 weeks pending",
          abs(a1["A"]["med"] - 0.02) < 1e-9 and a1["A"]["mex"] > 0 > a1["B"]["mex"] and a1["A"]["up"] == 1.0 and a1["B"]["up"] == 0.0
          and abs(a1["全部样本"]["mex"]) < 1e-12 and sum(ex1.values()) == 1 and a4["A"]["nr"] == 11 and 26 not in {a["h"] for a in res["agg"]}
          and set(res["pending"]) == {"26", "52"} and all(7 * s["h"] - 2 <= s["days"] for s in res["samples"]),
          f"A1={a1['A']['med']:.4f} ex={ex1} pending={res['pending']}")
    one = F.evaluate({k: v[:1] for k, v in ser.items()})
    check("forward: a single start date gives no samples, every horizon pending (no backfill)",
          one["samples"] == [] and set(one["pending"]) == {"1", "4", "13", "26", "52"})
    real = LDG.load()
    if real:
        s = CF.series(real)
        # records written after tagging carry `tags`: the forward adapter must recompute exactly those (older records have none)
        stored = [(x, p) for x in real for p in s[x["code"]] if p["d"] == x["date"] and x.get("tags") is not None]
        same = all(p["tags"] == x["tags"] for x, p in stored)
        pg = CF.page_payload()
        check("forward on the real ledger: one point per (code, date), tags recomputed = stored tags where stored; page payload has rules / pending / note",
              same and sum(len(v) for v in s.values()) == len({(x["code"], x["date"]) for x in real})
              and pg is not None and pg["rules"]["horizons"] == [1, 4, 13, 26, 52] and pg.get("note") is not None,
              f"stored_tags={len(stored)} starts={pg and pg.get('n_starts')} samples={pg and pg.get('n_samples')}")


def lookup_value_checks():
    """全市场速查: one row per quoted stock, '' reasons <=> the ② screen, every failing rule listed.
    价值通道: metrics() on a synthetic company; on the real files every channel row satisfies every rule, ranks are 1..n."""
    import json as _json
    from . import build as B, quality as QM
    sp = B.latest_snap()
    with open(sp, encoding="utf-8") as f:
        snap = _json.load(f)
    res = B.analyze(snap)
    L = res["lookup"]
    i_why = L["cols"].index("why")
    inl = {r[0] for r in L["rows"] if r[i_why] == ""}
    scr = {r["code"] for r in res["screen"]}
    multi = sum(1 for r in L["rows"] if "," in r[i_why])
    check("lookup: one row per quoted stock, empty reason set == ② screen codes, growth-dropped stocks counted",
          len(L["rows"]) == len(snap["quotes"]) and inl == scr and res["counts"]["dropped"].get("growth", 0) > 0,
          f"rows={len(L['rows'])} screen={len(scr)} dropped={res['counts']['dropped']} multi_reason={multi}")
    r182 = next((r for r in L["rows"] if r[0] == "002182"), None)
    check("lookup real data: 002182 present with reason 'growth' (revenue YoY < 20%)",
          r182 is not None and "growth" in r182[i_why].split(","), str(r182 and r182[i_why]))
    ys = ["2021", "2022", "2023", "2024", "2025"]
    it = {y: {"rev": 100.0 * (1.05 ** i), "np": 10.0, "roe": [12, 14, 9, 15, 13][i], "ocf": 12.0, "cx": 4.0, "eq": 80.0, "cash": 30.0,
              "ibd": 20.0, "gw": 4.0, "div": 5.0} for i, y in enumerate(ys)}
    m = QM.metrics(it, ys)
    check("value metrics: ROE median / min, FCF positive years, cash conversion, 4-year CAGR, debt / equity, payout",
          m["roe_med"] == 13 and m["roe_min"] == 9 and m["fcf_pos"] == 5 and abs(m["conv"] - 1.2) < 1e-12
          and abs(m["cagr"] - 5.0) < 1e-9 and abs(m["de"] - 0.25) < 1e-12 and abs(m["payout"] - 0.5) < 1e-12 and m["net_cash"] == 10.0,
          str({k: m[k] for k in ("roe_med", "roe_min", "fcf_pos", "conv", "cagr", "de", "payout")}))
    qdoc, qp = QM.load_latest()
    if not qdoc:
        check("value channel file present (run python -m cn quality)", False)
        return
    V = QM.channel(snap, res, qdoc, None)
    R = QM.VALUE_RULES
    bad = [r["code"] for r in V["rows"] if not (r["roe_med"] >= R["roe_median_min"] - 0.05 and r["roe_min"] >= R["roe_min_min"] - 0.05
           and r["fcf_pos"] >= R["fcf_pos_min"] and r["conv"] >= R["conv_min"] - 0.005 and r["cagr"] >= -0.05 and r["de"] <= R["de_max"] + 0.005)]
    ranks = sorted(r["rank"] for r in V["rows"] if r.get("rank"))
    check("value channel (real files): every row meets every rule, cyclical rows unranked, ranks 1..n, reason set for every quote",
          not bad and ranks == list(range(1, V["n_ranked"] + 1)) and all(not r.get("rank") for r in V["rows"] if r["cyclical"])
          and len(V["why"]) == len(snap["quotes"]) and sum(1 for w in V["why"].values() if w == "") == V["n_pass"],
          f"file={os.path.basename(qp)} pass={V['n_pass']} ranked={V['n_ranked']} bad={bad[:5]}")


def dividend_checks():
    """红利筛选: synthetic pass / fail cases, then every real row meets every rule, ranks 1..n per group, one reason per quote."""
    import json as _json
    from . import build as B, quality as QM, dividend as DV
    ys = ["2021", "2022", "2023", "2024", "2025"]
    it = {y: {"np": 10e8, "div": 4e8 * (1.05 ** i), "roe": 15.0, "ocf": 14e8, "cx": 4e8} for i, y in enumerate(ys)}
    q, f = {"total_mcap": 100e8, "pe_ttm": 10.0}, {"parent_net_profit": 5e8, "net_profit_yoy_pct": 3.0}
    m, w = DV.evaluate(it, ys, q, f, False)
    cut = {**it, "2025": {**it["2025"], "div": 1e8}}
    _, wc = DV.evaluate(cut, ys, {**q, "total_mcap": 20e8}, f, False)
    _, wr = DV.evaluate(it, ys, q, {**f, "net_profit_yoy_pct": -45.0}, False)
    check("dividend rules (synthetic): pass case; a cut below 80% of the 3-year mean and a YTD profit drop are rejected",
          w == [] and abs(m["dy"] - 4e8 * 1.05 ** 4 / 100e8 * 100) < 1e-9 and "d_cut" in wc and "d_recent" in wr, f"{w} {wc} {wr}")
    qdoc, _ = QM.load_latest()
    if not qdoc:
        check("dividend: quality file present (run python -m cn quality)", False)
        return
    with open(B.latest_snap(), encoding="utf-8") as fh:
        snap = _json.load(fh)
    res = B.analyze(snap)
    P = DV.panel(snap, res, qdoc, 1.64)
    R = DV.DIV_RULES
    bad = [r["code"] for r in P["rows"] if not (r["dy"] >= R["dy_min"] - 0.005 and all(x and x > 0 for x in r["div"])
           and r["payout"] <= (R["payout_max_fin"] if r["group"] == "fin" else R["payout_max"]) + 1e-3
           and (r["group"] == "fin" or (r["cover"] is not None and r["cover"] >= 1 - 1e-3)) and r["mcap"] >= R["mcap_min"])]
    ranks_ok = all(sorted(r["rank"] for r in P["rows"] if r["group"] == g) == list(range(1, P["n"][g] + 1)) for g in P["n"])
    check("dividend screen (real files): every row meets every rule, ranks 1..n in each group, a reason for every quote",
          not bad and ranks_ok and len(P["why"]) == len(snap["quotes"]) and sum(1 for v in P["why"].values() if v == "") == len(P["rows"]),
          f"groups={P['n']} bad={bad[:5]}")
    if len(qdoc["fiscal_years"]) >= DV.LONG_RULES["n_years"]:
        LTP, LR = DV.long_term(snap, res, qdoc, 1.64), DV.LONG_RULES
        cyc = set(B.RULES["cyclical_industries"]) | set(DV.LONG_CYCLICAL_EXTRA)
        badl = [r["code"] for r in LTP["rows"] if not (len(r["div"]) == 10 and all(x and x > 0 for x in r["div"]) and (r["max_cut"] or 0) >= LR["max_cut"] - 0.05
                and r["industry"] not in cyc and r["rev_cagr"] >= LR["rev_cagr_min"] - 0.05 and r["div_cagr"] >= -0.05 and r["dy"] >= LR["dy_min"] - 0.005)]
        rk = all(sorted(r["rank"] for r in LTP["rows"] if r["group"] == g) == list(range(1, LTP["n"][g] + 1)) for g in LTP["n"])
        check("long-term dividend view (10 fiscal years): every row meets the rules, no cyclical industry, ranks 1..n per group",
              not badl and rk and len(LTP["why"]) == len(snap["quotes"]), f"groups={LTP['n']} bad={badl[:5]}")
    else:
        check("long-term dividend view: quality file has 10 fiscal years (run python -m cn quality)", False, str(len(qdoc["fiscal_years"])))
    # extra full cards: long-term dividend companies outside ② + the 交集 view; their rows never enter ② / ③ / counts
    from . import cards as CM
    res2 = B.analyze(snap)
    n_screen = len(res2["screen"])
    CM.with_extras(snap, res2)
    scr = {r["code"] for r in res2["screen"]}
    lt_codes = {r["code"] for r in DV.long_term(snap, res2, qdoc, 1.64)["rows"]}
    xr = {r["code"] for r in res2["extra_rows"]}
    cards = set(CM.card_codes(res2))
    fin_ok = all(r["gates"]["ocf"]["s"] == "na" for r in res2["extra_rows"] if any(w in (r.get("industry") or "") for w in B.FIN_INDUSTRY_WORDS))
    check("extra cards: every long-term dividend company has a card; extra rows are outside ② and ② / ③ are unchanged; financial rows mark OCF n/a",
          lt_codes <= cards and not (xr & scr) and xr == lt_codes - scr and len(res2["screen"]) == n_screen and fin_ok,
          f"extra={len(res2['extra_codes'])} extra_rows={len(xr)} cards={len(cards)}")


if __name__ == "__main__":
    sys.exit(main())
