"""Map companies to sector segments and apply each segment's screening thresholds."""
import re

from .paths import CFG, load


def sectors():
    return load("sectors.json")["sectors"]


def thresholds(sector, segment):
    t = {k: v for k, v in CFG["screen"].items() if not k.startswith("_")}
    t.update(sector.get("screen", {}))
    t.update(segment.get("screen", {}))
    return t


def loosest():
    ts = [thresholds(s, g) for s in sectors() for g in s["segments"]]
    keys = {"ttm_revenue_min": min, "ttm_revenue_max": max, "yoy_growth_min": min, "prior_quarter_revenue_min": min}
    return {k: f(t[k] for t in ts) for k, f in keys.items()}


def _excluded(sector, ticker, name):
    ex = sector.get("exclude", {})
    if ticker in ex.get("tickers", {}):
        return ex["tickers"][ticker]
    if ex.get("name_regex") and re.search(ex["name_regex"], name or "", re.I):
        return f"名称命中本行业排除规则（{ex['name_regex']}）"
    return None


def _segment(sector, ticker, name, sic=None, industry=None):
    ov = sector.get("overrides", {}).get(ticker)
    if ov:
        return next(g for g in sector["segments"] if g["id"] == ov["segment"]), ov["why"]
    us = sic is not None
    fits = lambda g: (not g.get("sic") or sic in g["sic"]) if us else (not g.get("yahoo_industry") or industry in g["yahoo_industry"])
    if not us and industry not in sector.get("intl", {}).get("industries", []):
        return None, None
    for g in sector["segments"]:  # keyword rules first, so a 3674 "photonics" company lands in optical
        if g["name_regex"] and fits(g) and re.search(g["name_regex"], name or "", re.I):
            return g, None
    for g in sector["segments"]:
        if not g["name_regex"] and ((us and g.get("sic") and sic in g["sic"]) or (not us and industry in g.get("yahoo_industry", []))):
            return g, None
    return None, None


def profile(g):
    return g.get("profile", "standard")


def no_peg(g):
    """Reason text when the segment sets "peg": false (PEG not meaningful there), otherwise None."""
    return (g.get("peg_why") or "本环节不适用 PEG") if g.get("peg") is False else None


def view(r, prof):
    """The row as seen by one evaluation profile.

    Financial profile: revenue comes from the financial tag list when it differs (r["rev_fin"], SEC only);
    gross margin is not meaningful, and operating margin is dropped when it was computed on the other revenue figure.
    """
    if prof != "financial":
        return r
    v = {**r, **(r.get("rev_fin") or {}), "gm": None, "gm_ly": None}
    if r.get("rev_fin"):
        v["opm"] = v["opm_ly"] = None
    return v


def passes(r, t):
    return bool(r.get("rev_ttm") and t["ttm_revenue_min"] <= r["rev_ttm"] <= t["ttm_revenue_max"]
                and r["rev_q_ly"] >= t["prior_quarter_revenue_min"] and t["yoy_growth_min"] <= r["yoy"] <= t["yoy_growth_max_sanity"])


def memberships(r, only_sector=None):
    """All (sector, segment) pairs a company qualifies for, plus exclusions for the audit table."""
    out, excluded = [], []
    for s in sectors():
        if only_sector and s["id"] != only_sector:
            continue
        g, why_override = _segment(s, r["ticker"], r["name"], r.get("sic"), r.get("industry"))
        if not g:
            continue
        why_ex = _excluded(s, r["ticker"], r["name"])
        if why_ex:
            excluded.append({"sector": s["id"], "why": why_ex})
            continue
        t = thresholds(s, g)
        if passes(view(r, profile(g)), t):
            out.append({"sector": s["id"], "segment": g["id"], "override": why_override,
                        "cyclical": bool(g.get("cyclical")), "mcap_max": t["market_cap_max"],
                        "profile": profile(g), "credit": bool(g.get("credit")), "verify": g.get("verify", []), "no_peg": no_peg(g)})
    return out, excluded
