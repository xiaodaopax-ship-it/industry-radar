"""US data-layer self check (offline): python -m radar.smoke
Covers the value channel (radar/quality.py metrics / fails / channel) and the full-market lookup on the local files."""
import sys

N_FAIL = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())
    if not ok:
        N_FAIL.append(name)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from . import quality as Q, heat
    from .build import latest_raw, model
    ys = ["2021", "2022", "2023", "2024", "2025"]
    it = {"eq": {"2020": 90.0, "2021": 100.0, "2022": 110.0, "2023": -5.0, "2024": 120.0, "2025": 130.0},
          "bs": {"cash": 50.0, "ltd": 80.0, "gw": 10.0}}
    for i, y in enumerate(ys):
        it[y] = {"rev": 1000.0 * 1.05 ** i, "ni": 20.0, "ocf": 30.0, "cx": 10.0, "div": 5.0, "bb": 5.0}
    m = Q.metrics(it, ys)
    check("metrics: ROE on average equity, equity <= 0 years skipped, FCF, conversion, CAGR, net debt / FCF",
          abs(m["roe"][0] - 20 / 95 * 100) < 1e-9 and m["roe"][2] is None and abs(m["roe"][3] - 20 / 120 * 100) < 1e-9
          and m["n_roe"] == 4 and m["fcf_pos"] == 5
          and abs(m["conv"] - 1.5) < 1e-12 and abs(m["cagr"] - 5.0) < 1e-9 and abs(m["nd_fcf"] - 1.5) < 1e-12,
          str({k: m[k] for k in ("n_roe", "roe_med", "conv", "cagr", "nd_fcf")}))
    check("fails: revenue below $200M is the only failing rule of the synthetic company", Q.fails(m) == ["small"], str(Q.fails(m)))
    m2 = Q.metrics({**it, "2025": {**it["2025"], "ni": -1.0}}, ys)
    check("fails: a loss in the latest fiscal year fails 'np'", "np" in Q.fails(m2), str(Q.fails(m2)))
    doc, path = Q.latest()
    if not doc:
        check("quality file present (run python -m radar quality)", False)
    else:
        D = model(latest_raw(), with_ledger=False)
        V = D["vc"]
        R = Q.VALUE_RULES
        std = [r for r in V["rows"] if r["group"] == "std"]
        fin = [r for r in V["rows"] if r["group"] == "fin"]
        bad = [r["ticker"] for r in std if not (r["roe_med"] >= R["roe_median_min"] - 0.05 and r["roe_min"] >= R["roe_min_min"] - 0.05
               and r["fcf_pos"] >= R["fcf_pos_min"] and r["conv"] >= R["conv_min"] - 0.005 and r["cagr"] >= -0.05
               and (r["nd_fcf"] is None or r["nd_fcf"] <= R["nd_fcf_max"] + 0.05))]
        ranks = sorted(r["rank"] for r in std if r.get("rank"))
        check("value channel (real file): every standard row meets every rule, cyclical unranked, ranks 1..n, rows have market cap",
              not bad and ranks == list(range(1, V["n_ranked"] + 1)) and all(not r.get("rank") for r in std if r["cyclical"])
              and all(r["mcap"] for r in V["rows"]) and all(len(r["fy"]) == 5 for r in V["rows"]), f"pass={V['n_pass']} ranked={V['n_ranked']} bad={bad[:5]}")
        F = Q.FIN_RULES
        badf = [r["ticker"] for r in fin if not (r["eq"] >= F["eq_min"] and r["ni_pos"] >= F["ni_pos_min"] and r["roe_med"] >= F["roe_median_min"] - 0.05
                and r["roe_min"] >= F["roe_min_min"] - 0.05 and r["eq_cagr"] >= -0.05 and str(r["sic"]).zfill(4)[:2] in ("60", "61", "62", "63", "64", "67")
                and r["sic"] not in (6770, 6798, 6726))]
        fr = sorted(r["rank"] for r in fin if r.get("rank"))
        check("value channel financial (real file): every row is SIC 60-67 (not REIT / SPAC / fund) and meets the financial rules, ranks 1..n",
              not badf and fr == list(range(1, V["n_ranked_fin"] + 1)) and V["n_fin"] == len(fin), f"fin={len(fin)} bad={badf[:5]}")
        L = D["lookup"]
        ent = {e["ticker"] for e in D["entries"]}
        scr = {r[0] for r in L["rows"] if r[8] == "screen"}
        check("lookup: 'screen' rows == dashboard entries; every heat row with a US ticker is listed once",
              scr == ent and len({r[0] for r in L["rows"]}) == len(L["rows"]), f"rows={len(L['rows'])} screen={len(scr)} entries={len(ent)}")
        ko = next((r for r in L["rows"] if r[0] == "KO"), None)
        check("lookup real data: KO present and not in the screen (beverages are not a configured segment)",
              ko is not None and ko[8] == "no_seg", str(ko and ko[8]))
    print(f"SUMMARY: {'all' if not N_FAIL else len(N_FAIL)} {'checks passed' if not N_FAIL else 'checks FAILED: ' + '; '.join(N_FAIL)}")
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
