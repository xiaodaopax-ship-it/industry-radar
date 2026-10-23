"""Smoke test for cn/em.py. Run from the industry-radar root:  python -m cn.smoke

Calls every function with small, realistic arguments, prints counts + one sample row (ASCII-escaped),
and cross-checks 600519 / 300750 between independent Eastmoney reports. Exit code 1 if any check fails.
"""
import collections
import datetime as dt
import json
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


if __name__ == "__main__":
    sys.exit(main())
