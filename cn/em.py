"""Eastmoney / cninfo client for A-share research data (standard library only).

Conventions
- Every public function returns a list of plain dicts with English keys and never raises:
  on failure it prints a WARNING (English, stderr) and returns [] (or whatever partial data it has).
- Monetary values are CNY (yuan), share counts are shares, ratios ending in `_pct` are percent (12.3 == 12.3%).
- Financial statement flows (revenue, profit, cash flow) are YEAR-TO-DATE CUMULATIVE as disclosed in China:
  report_date 03-31 = Q1, 06-30 = H1, 09-30 = Q1-Q3, 12-31 = full year. Derive single quarters by differencing.
- Raw Eastmoney field codes are documented next to each mapping so the source can be re-checked.

Politeness: one global rate limiter (<= 2 requests/second across threads), 3 attempts with backoff,
JSON file cache in industry-radar/data/cn/cache/ keyed by sha1(url + body) with a per-call TTL (days).
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cn", "cache")
CST = dt.timezone(dt.timedelta(hours=8))

EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "Origin": "https://www.cninfo.com.cn",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_MAX_PAGE = 500  # datacenter-web silently caps pageSize at 500
PUSH2_HOSTS = ["push2.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com"]  # rotated on retry (502s are intermittent)
PUSH2_MAX_PAGE = 100  # push2 clist silently caps pz at 100

_LOCK, _LAST = threading.Lock(), [0.0]
MIN_INTERVAL = 0.5  # seconds between any two network requests => <= 2 req/s
STATS = {"network": 0, "cache_hits": 0, "failures": 0}


def warn(msg):
    STATS["failures"] += 1
    print(f"WARNING: {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------- HTTP + cache
def _throttle():
    with _LOCK:
        wait = MIN_INTERVAL - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()


def _cache_path(key):
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def fetch_json(url, headers=None, data=None, ttl=1.0, retries=3, timeout=30, alt_urls=None, quiet=False):
    """GET (or POST when `data` is a dict) returning parsed JSON, or None on failure.

    ttl: cache lifetime in days (0 disables reading the cache). Failures are never cached.
    alt_urls: optional list of fallback URLs used on successive retries (host rotation).
    """
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    key = url + ("|" + body.decode() if body else "")
    path = _cache_path(key)
    if ttl and os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl * 86400:
        try:
            with open(path, encoding="utf-8") as f:
                STATS["cache_hits"] += 1
                return json.load(f)
        except (OSError, ValueError):
            pass
    last_err = None
    for attempt in range(retries):
        u = (alt_urls[attempt % len(alt_urls)] if alt_urls else url)
        _throttle()
        STATS["network"] += 1
        try:
            req = urllib.request.Request(u, headers=headers or EM_HEADERS, data=body)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            text = raw.decode("utf-8", "replace").strip()
            if text.startswith("jQuery") or (text.endswith(")") and "(" in text[:80]):  # JSONP guard
                text = text[text.index("(") + 1:text.rindex(")")]
            obj = json.loads(text)
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
            return obj
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # network error, timeout, bad JSON
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            time.sleep(1.5 * (2 ** attempt))  # 1.5s, 3s
    if not quiet:
        warn(f"request failed after {retries} attempts ({last_err}): {url[:160]}")
    return None


# ----------------------------------------------------------------------------- helpers
def num(v):
    """Eastmoney uses '-', '', None for missing values."""
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scale(v, k):
    x = num(v)
    return None if x is None else x * k


def day(v):
    """'2026-06-30 00:00:00' -> '2026-06-30'."""
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def market_of(code, secucode=None):
    if secucode and "." in secucode:
        s = secucode.split(".")[-1].upper()
        if s in ("SH", "SZ", "BJ"):
            return s
    if not code:
        return None
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return "SH"
    if code.startswith(("92", "8", "4")):
        return "BJ"
    return "SZ"


def board_of(code):
    """Listing board from the code prefix (used when the source does not say)."""
    if not code:
        return None
    if code.startswith(("688", "689")):
        return "STAR"        # 科创板
    if code.startswith(("300", "301", "302")):
        return "ChiNext"     # 创业板
    if code.startswith(("92", "8", "4")):
        return "BSE"         # 北交所
    if code.startswith("6"):
        return "SH_MAIN"     # 沪市主板
    return "SZ_MAIN"         # 深市主板


def is_a_code(code):
    return isinstance(code, str) and len(code) == 6 and code.isdigit()


def dc_report(report, columns="ALL", filt="", sort_columns="", sort_types="", page_size=DC_MAX_PAGE,
              max_pages=200, ttl=1.0, label=None, warn_truncated=True):
    """Fetch every page of a datacenter-web report. Returns (rows, ok).

    ok=False means at least one page failed (rows may be partial). An empty result
    (Eastmoney code 9201 '返回数据为空') is ok=True with rows=[].
    Always pass a deterministic sort (ending in SECURITY_CODE) so pages do not shift.
    """
    rows, page, pages = [], 1, 1
    label = label or report
    while page <= min(pages, max_pages):
        q = {"reportName": report, "columns": columns, "pageSize": min(page_size, DC_MAX_PAGE), "pageNumber": page,
             "source": "WEB", "client": "WEB"}
        if filt:
            q["filter"] = filt
        if sort_columns:
            q["sortColumns"], q["sortTypes"] = sort_columns, sort_types
        d = fetch_json(DC_URL + "?" + urllib.parse.urlencode(q), ttl=ttl)
        if d is None:
            warn(f"{label}: page {page} failed; returning {len(rows)} partial rows")
            return rows, False
        res = d.get("result")
        if not d.get("success") or not res:
            if d.get("code") == 9201 or "为空" in str(d.get("message")):
                return rows, True
            warn(f"{label}: unexpected response code={d.get('code')}")
            return rows, False
        rows.extend(res.get("data") or [])
        pages = res.get("pages") or 1
        page += 1
    if pages > max_pages and warn_truncated:
        warn(f"{label}: stopped at max_pages={max_pages} of {pages}")
    return rows, True


def _latest_by_code(rows, code_key="SECURITY_CODE", date_keys=("NOTICE_DATE", "UPDATE_DATE")):
    """Keep one row per code: the most recently published version (restatements supersede)."""
    best = {}
    for r in rows:
        c = r.get(code_key)
        if not is_a_code(c):
            continue
        k = tuple(str(r.get(x) or "") for x in date_keys)
        if c not in best or k > best[c][0]:
            best[c] = (k, r)
    return {c: v[1] for c, v in best.items()}


# ----------------------------------------------------------------------------- a. quotes
# push2 clist field codes (verified 2026-10-21 against 600519 / 300750 and RPT_VALUEANALYSIS_DET):
#   f12 code | f14 name | f13 market id (1=SH, 0=SZ/BJ) | f2 last price | f3 change % | f8 turnover %
#   f20 total mcap CNY | f21 float mcap CNY | f115 PE(TTM) | f9 PE(dynamic, annualised YTD) | f114 PE(static, last FY)
#   f23 PB(MRQ) | f26 listing date yyyymmdd | f100 Eastmoney industry (东财行业) | f6 turnover amount CNY
_QUOTE_FIELDS = "f2,f3,f6,f8,f9,f12,f13,f14,f20,f21,f23,f26,f100,f114,f115"
_BOARDS = [  # (fs filter, board, market)
    ("m:1+t:2", "SH_MAIN", "SH"),
    ("m:1+t:23", "STAR", "SH"),
    ("m:0+t:6", "SZ_MAIN", "SZ"),
    ("m:0+t:80", "ChiNext", "SZ"),
    ("m:0+t:81+s:2048", "BSE", "BJ"),
]


# EOD fallback: RPT_VALUEANALYSIS_DET filtered on the latest TRADE_DATE (all A shares incl. BSE, ~12 requests):
#   CLOSE_PRICE | CHANGE_RATE % | TOTAL_MARKET_CAP CNY | NOTLIMITED_MARKETCAP_A float A mcap CNY | PE_TTM | PE_LAR
#   PB_MRQ | PS_TTM | BOARD_NAME Eastmoney industry (same taxonomy as f100) | TOTAL_SHARES | FREE_SHARES_A
def _eod_snapshot(ttl):
    head, _ = dc_report("RPT_VALUEANALYSIS_DET", "TRADE_DATE", "", "TRADE_DATE", "-1", page_size=1, max_pages=1,
                        ttl=ttl, label="quotes/eod-date", warn_truncated=False)
    if not head:
        return {}, None
    as_of = day(head[0].get("TRADE_DATE"))
    rows, _ = dc_report("RPT_VALUEANALYSIS_DET",
                        "SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,CLOSE_PRICE,CHANGE_RATE,TOTAL_MARKET_CAP,"
                        "NOTLIMITED_MARKETCAP_A,PE_TTM,PE_LAR,PB_MRQ,PS_TTM,BOARD_NAME,TOTAL_SHARES,FREE_SHARES_A",
                        f"(TRADE_DATE='{as_of}')", "SECURITY_CODE", "1", ttl=ttl, label="quotes/eod")
    out = {}
    for r in rows:
        code = r.get("SECURITY_CODE")
        if not is_a_code(code):
            continue
        out[code] = {
            "code": code, "name": r.get("SECURITY_NAME_ABBR"), "market": market_of(code, r.get("SECUCODE")),
            "board": board_of(code), "price": num(r.get("CLOSE_PRICE")), "change_pct": num(r.get("CHANGE_RATE")),
            "total_mcap": num(r.get("TOTAL_MARKET_CAP")), "float_mcap": num(r.get("NOTLIMITED_MARKETCAP_A")),
            "pe_ttm": num(r.get("PE_TTM")), "pe_dynamic": None, "pe_static": num(r.get("PE_LAR")),
            "pb": num(r.get("PB_MRQ")), "ps_ttm": num(r.get("PS_TTM")), "turnover_pct": None, "amount": None,
            "industry": r.get("BOARD_NAME"), "list_date": None,
            "total_shares": num(r.get("TOTAL_SHARES")), "float_shares": num(r.get("FREE_SHARES_A")),
            "quote_source": "datacenter_eod", "as_of": as_of,
        }
    return out, as_of


def _push2_live(ttl):
    """push2 clist pages per board. Circuit breaker: the first page that fails after retries stops all
    push2 paging (push2 answers bursts with HTTP 502 / dropped connections; hammering it does not help)."""
    out = {}
    now = dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    for fs, board, market in _BOARDS:
        page = 1
        while True:
            path = (f"/api/qt/clist/get?pn={page}&pz={PUSH2_MAX_PAGE}&po=0&np=1&fltt=2&invt=2&fid=f12"
                    f"&fs={fs}&fields={_QUOTE_FIELDS}")
            urls = [f"https://{h}{path}" for h in PUSH2_HOSTS]
            d = fetch_json(urls[0], ttl=ttl, alt_urls=urls, quiet=True)
            if d is None:
                warn(f"quotes: push2 board {board} page {page} failed (HTTP 502/connection drop); "
                     f"discarding partial live data, using datacenter EOD snapshot for all codes")
                return {}, False
            try:  # cached pages: report when the page was actually fetched
                now = dt.datetime.fromtimestamp(os.path.getmtime(_cache_path(urls[0])), CST).strftime("%Y-%m-%d %H:%M")
            except OSError:
                pass
            data = d.get("data") or {}
            diff = data.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            for q in diff:
                code = q.get("f12")
                if not is_a_code(code):
                    continue
                ld = str(q.get("f26") or "")
                out[code] = {
                    "code": code, "name": q.get("f14"), "market": market, "board": board,
                    "price": num(q.get("f2")), "change_pct": num(q.get("f3")),
                    "total_mcap": num(q.get("f20")), "float_mcap": num(q.get("f21")),
                    "pe_ttm": num(q.get("f115")), "pe_dynamic": num(q.get("f9")), "pe_static": num(q.get("f114")),
                    "pb": num(q.get("f23")), "ps_ttm": None, "turnover_pct": num(q.get("f8")), "amount": num(q.get("f6")),
                    "industry": q.get("f100") if q.get("f100") not in ("-", "") else None,
                    "list_date": f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}" if len(ld) == 8 and ld.isdigit() else None,
                    "total_shares": None, "float_shares": None, "quote_source": "push2_live", "as_of": now,
                }
            if not diff or page * PUSH2_MAX_PAGE >= (data.get("total") or 0):
                break
            page += 1
    return out, True


def quotes(ttl=1.0, live=False):
    """All A-share stocks (SH main, STAR, SZ main, ChiNext, BSE) with latest price and valuation.

    Sources:
      1. (default) datacenter EOD snapshot (RPT_VALUEANALYSIS_DET, latest trading day) - reliable, ~12 requests;
      2. live=True: push2 live quotes (~60 requests, 100 rows/page) - adds turnover, amount, PE(dynamic),
         list_date. push2 answered most paged requests with HTTP 502 during verification (2026-10-21/22);
         if ANY page fails the partial live data is discarded so the snapshot is never mixed-time,
         and all rows come from the EOD snapshot.
    Keys: code, name, market (SH/SZ/BJ), board (SH_MAIN/STAR/SZ_MAIN/ChiNext/BSE), price, change_pct,
    total_mcap, float_mcap (CNY), pe_ttm, pe_dynamic, pe_static, pb, ps_ttm, turnover_pct, amount (CNY),
    industry (Eastmoney 行业 = f100 / BOARD_NAME), list_date, total_shares, float_shares,
    quote_source ('push2_live' | 'datacenter_eod'), as_of. Fields a source lacks are None (merged from the other).
    """
    try:
        eod, as_of = _eod_snapshot(ttl)
        if not eod:
            warn("quotes: datacenter EOD snapshot unavailable")
        live_rows, _ = _push2_live(ttl) if live else ({}, True)
        out = []
        for code in sorted(set(eod) | set(live_rows)):
            e, l = eod.get(code), live_rows.get(code)
            if l and e:
                row = dict(l)
                for k, v in e.items():  # fill gaps (ps_ttm, shares) from EOD
                    if row.get(k) is None:
                        row[k] = v
            else:
                row = dict(l or e)
            out.append(row)
        return out
    except Exception as e:
        warn(f"quotes: unexpected error {type(e).__name__}: {e}")
        return []


# ----------------------------------------------------------------------------- b. financials
# RPT_LICO_FN_CPD (业绩报表): TOTAL_OPERATE_INCOME revenue | YSTZ revenue yoy % | PARENT_NETPROFIT 归母净利润
#   SJLTZ net profit yoy % | XSMLL gross margin % | BASIC_EPS | WEIGHTAVG_ROE 加权ROE % (YTD, not annualised)
#   BPS 每股净资产 | MGJYXJJE 每股经营现金流 | TRADE_MARKET listing board | PUBLISHNAME industry
_CPD_COLS = ("SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,REPORTDATE,NOTICE_DATE,UPDATE_DATE,TRADE_MARKET,PUBLISHNAME,"
             "TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,SJLTZ,XSMLL,BASIC_EPS,DEDUCT_BASIC_EPS,WEIGHTAVG_ROE,BPS,MGJYXJJE")
# RPT_DMSK_FN_CASHFLOW (现金流量表摘要): NETCASH_OPERATE 经营活动现金流净额 | NETCASH_INVEST | NETCASH_FINANCE
#   CONSTRUCT_LONG_ASSET 购建长期资产支付的现金 (capex proxy)
_CF_COLS = "SECURITY_CODE,NOTICE_DATE,REPORT_DATE,NETCASH_OPERATE,NETCASH_INVEST,NETCASH_FINANCE,CONSTRUCT_LONG_ASSET"
# RPT_F10_FINANCE_GBALANCE (F10 资产负债表, general-template companies): ACCOUNTS_RECE 应收账款 | INVENTORY 存货
#   GOODWILL 商誉 | TOTAL_PARENT_EQUITY 归母股东权益 | MONETARYFUNDS 货币资金 | SHORT_LOAN 短期借款
#   LONG_LOAN 长期借款 | BOND_PAYABLE 应付债券 | NONCURRENT_LIAB_1YEAR 一年内到期的非流动负债
#   TOTAL_ASSETS | TOTAL_LIABILITIES | SHARE_CAPITAL 股本 (shares, 1 CNY par) | CONTRACT_LIAB 合同负债
_BAL_COLS = ("SECURITY_CODE,SECUCODE,NOTICE_DATE,UPDATE_DATE,REPORT_DATE,TOTAL_ASSETS,TOTAL_LIABILITIES,MONETARYFUNDS,"
             "ACCOUNTS_RECE,INVENTORY,GOODWILL,SHORT_LOAN,LONG_LOAN,BOND_PAYABLE,NONCURRENT_LIAB_1YEAR,"
             "TOTAL_PARENT_EQUITY,SHARE_CAPITAL,CONTRACT_LIAB")
_BOARD_CN = {"上交所主板": "SH_MAIN", "上交所科创板": "STAR", "深交所主板": "SZ_MAIN", "深交所创业板": "ChiNext",
             "北京证券交易所": "BSE", "北交所": "BSE"}


def _sum_present(*vals):
    xs = [v for v in vals if v is not None]
    return sum(xs) if xs else None


def financials(report_date, ttl=1.0):
    """Per-company fundamentals for one report date ('YYYY-MM-DD', a quarter end).

    IMPORTANT: income and cash-flow items are YEAR-TO-DATE CUMULATIVE (Q1 / H1 / Q1-Q3 / FY), as reported
    in China; balance-sheet items are point-in-time at report_date. All amounts CNY; *_pct are percent.

    Keys: code, name, market, board, industry, report_date, notice_date,
      revenue, revenue_yoy_pct, parent_net_profit, net_profit_yoy_pct, gross_margin_pct, eps, eps_deducted,
      roe_pct (weighted, YTD - not annualised), bps, operating_cash_flow_per_share,
      operating_cash_flow, investing_cash_flow, financing_cash_flow, capex,
      accounts_receivable, inventory, goodwill, parent_equity, cash, total_assets, total_liabilities,
      short_term_loans, long_term_loans, bonds_payable, current_portion_noncurrent_liab,
      interest_bearing_debt (= short_term_loans + long_term_loans + bonds_payable; None if all missing),
      share_capital, contract_liabilities, has_balance_sheet (False for banks/insurers/brokers - see SOURCES.md).
    Sources: RPT_LICO_FN_CPD + RPT_DMSK_FN_CASHFLOW + RPT_F10_FINANCE_GBALANCE (~40 requests per date).
    """
    try:
        rd = str(report_date)[:10]
        # SECURITY_TYPE_CODE 058001001 = A股; without it the report also returns ~5.8k NEEQ (新三板) companies.
        cpd, ok1 = dc_report("RPT_LICO_FN_CPD", _CPD_COLS, f"(REPORTDATE='{rd}')(SECURITY_TYPE_CODE=\"058001001\")",
                             "SECURITY_CODE", "1", ttl=ttl, label=f"financials/CPD {rd}")
        cf, ok2 = dc_report("RPT_DMSK_FN_CASHFLOW", _CF_COLS, f"(REPORT_DATE='{rd}')", "SECURITY_CODE", "1", ttl=ttl,
                            label=f"financials/cashflow {rd}")
        bal, ok3 = dc_report("RPT_F10_FINANCE_GBALANCE", _BAL_COLS,
                             f"(REPORT_DATE='{rd}')(SECURITY_TYPE_CODE=\"058001001\")", "SECURITY_CODE", "1", ttl=ttl,
                             label=f"financials/balance {rd}")
        cpd_m = _latest_by_code(cpd, date_keys=("UPDATE_DATE", "NOTICE_DATE"))
        cf_m = _latest_by_code(cf)
        bal_m = _latest_by_code(bal, date_keys=("UPDATE_DATE", "NOTICE_DATE"))
        out = []
        for code in sorted(set(cpd_m) | set(cf_m) | set(bal_m)):
            p, c, b = cpd_m.get(code, {}), cf_m.get(code, {}), bal_m.get(code, {})
            st, lt, bd = num(b.get("SHORT_LOAN")), num(b.get("LONG_LOAN")), num(b.get("BOND_PAYABLE"))
            out.append({
                "code": code, "name": p.get("SECURITY_NAME_ABBR"),
                "market": market_of(code, p.get("SECUCODE") or b.get("SECUCODE")),
                "board": _BOARD_CN.get(p.get("TRADE_MARKET"), board_of(code)),
                "risk_warning": "风险警示" in (p.get("TRADE_MARKET") or ""),  # ST / *ST board
                "industry": p.get("PUBLISHNAME"), "report_date": rd,
                "notice_date": day(p.get("NOTICE_DATE") or c.get("NOTICE_DATE") or b.get("NOTICE_DATE")),
                "revenue": num(p.get("TOTAL_OPERATE_INCOME")), "revenue_yoy_pct": num(p.get("YSTZ")),
                "parent_net_profit": num(p.get("PARENT_NETPROFIT")), "net_profit_yoy_pct": num(p.get("SJLTZ")),
                "gross_margin_pct": num(p.get("XSMLL")), "eps": num(p.get("BASIC_EPS")),
                "eps_deducted": num(p.get("DEDUCT_BASIC_EPS")), "roe_pct": num(p.get("WEIGHTAVG_ROE")),
                "bps": num(p.get("BPS")), "operating_cash_flow_per_share": num(p.get("MGJYXJJE")),
                "operating_cash_flow": num(c.get("NETCASH_OPERATE")), "investing_cash_flow": num(c.get("NETCASH_INVEST")),
                "financing_cash_flow": num(c.get("NETCASH_FINANCE")), "capex": num(c.get("CONSTRUCT_LONG_ASSET")),
                "accounts_receivable": num(b.get("ACCOUNTS_RECE")), "inventory": num(b.get("INVENTORY")),
                "goodwill": num(b.get("GOODWILL")), "parent_equity": num(b.get("TOTAL_PARENT_EQUITY")),
                "cash": num(b.get("MONETARYFUNDS")), "total_assets": num(b.get("TOTAL_ASSETS")),
                "total_liabilities": num(b.get("TOTAL_LIABILITIES")),
                "short_term_loans": st, "long_term_loans": lt, "bonds_payable": bd,
                "current_portion_noncurrent_liab": num(b.get("NONCURRENT_LIAB_1YEAR")),
                "interest_bearing_debt": _sum_present(st, lt, bd) if b else None,
                "share_capital": num(b.get("SHARE_CAPITAL")), "contract_liabilities": num(b.get("CONTRACT_LIAB")),
                "has_balance_sheet": bool(b),
            })
        if not (ok1 and ok2 and ok3):
            warn(f"financials {rd}: one or more sources incomplete (cpd={ok1}, cashflow={ok2}, balance={ok3})")
        return out
    except Exception as e:
        warn(f"financials: unexpected error {type(e).__name__}: {e}")
        return []


def income_statement(code, report_date, ttl=1.0):
    """RPT_DMSK_FN_INCOME (利润表摘要) for one company - used for cross-checks. YTD cumulative, CNY.
    Fields: TOTAL_OPERATE_INCOME revenue | PARENT_NETPROFIT | DEDUCT_PARENT_NETPROFIT 扣非归母 | OPERATE_PROFIT."""
    try:
        rows, _ = dc_report("RPT_DMSK_FN_INCOME", "SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,"
                            "TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT,OPERATE_PROFIT",
                            f"(SECURITY_CODE=\"{code}\")(REPORT_DATE='{str(report_date)[:10]}')", ttl=ttl,
                            label=f"income {code}")
        return [{"code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"), "report_date": day(r.get("REPORT_DATE")),
                 "revenue": num(r.get("TOTAL_OPERATE_INCOME")), "parent_net_profit": num(r.get("PARENT_NETPROFIT")),
                 "deducted_net_profit": num(r.get("DEDUCT_PARENT_NETPROFIT")), "operating_profit": num(r.get("OPERATE_PROFIT"))}
                for r in rows]
    except Exception as e:
        warn(f"income_statement: unexpected error {type(e).__name__}: {e}")
        return []


def deducted_history(code, start, end, ttl=1.0):
    """扣非 (non-recurring items excluded) earnings for one company, every report date in [start, end].
    Two requests: RPT_LICO_FN_CPD (BASIC_EPS, DEDUCT_BASIC_EPS - published to 2 decimals) and RPT_DMSK_FN_INCOME
    (PARENT_NETPROFIT, DEDUCT_PARENT_NETPROFIT, CNY). YTD cumulative. When a period appears more than once the most
    recently updated row wins (restated comparatives).
    Returns {report_date: {eps, eps_deducted, parent_net_profit, deducted_net_profit}}; {} on failure."""
    out = {}
    try:
        a, b = str(start)[:10], str(end)[:10]
        cpd, _ = dc_report("RPT_LICO_FN_CPD", "SECURITY_CODE,REPORTDATE,UPDATE_DATE,NOTICE_DATE,BASIC_EPS,DEDUCT_BASIC_EPS",
                           f"(SECURITY_CODE=\"{code}\")(REPORTDATE>='{a}')(REPORTDATE<='{b}')", "REPORTDATE", "-1",
                           page_size=50, max_pages=1, ttl=ttl, label=f"deducted/CPD {code}", warn_truncated=False)
        inc, _ = dc_report("RPT_DMSK_FN_INCOME", "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT",
                           f"(SECURITY_CODE=\"{code}\")(REPORT_DATE>='{a}')(REPORT_DATE<='{b}')", "REPORT_DATE", "-1",
                           page_size=50, max_pages=1, ttl=ttl, label=f"deducted/income {code}", warn_truncated=False)
        seen = {}
        for r in cpd:
            d, u = day(r.get("REPORTDATE")), str(r.get("UPDATE_DATE") or r.get("NOTICE_DATE") or "")
            if d and u >= seen.get(("c", d), ""):
                seen[("c", d)] = u
                out.setdefault(d, {}).update(eps=num(r.get("BASIC_EPS")), eps_deducted=num(r.get("DEDUCT_BASIC_EPS")))
        for r in inc:
            d, u = day(r.get("REPORT_DATE")), str(r.get("NOTICE_DATE") or "")
            if d and u >= seen.get(("i", d), ""):
                seen[("i", d)] = u
                out.setdefault(d, {}).update(parent_net_profit=num(r.get("PARENT_NETPROFIT")),
                                             deducted_net_profit=num(r.get("DEDUCT_PARENT_NETPROFIT")))
        return out
    except Exception as e:
        warn(f"deducted_history: unexpected error {type(e).__name__}: {e}")
        return out


# ----------------------------------------------------------------------------- c. forecasts
# RPT_PUBLIC_OP_NEWPREDICT (业绩预告): NOTICE_DATE | REPORT_DATE period | PREDICT_FINANCE metric (归母净利润/扣非/营收...)
#   PREDICT_TYPE 预增/预减/扭亏/首亏/续亏/续盈/略增/略减/不确定 | PREDICT_AMT_LOWER/UPPER CNY | ADD_AMP_LOWER/UPPER yoy %
#   PREYEAR_SAME_PERIOD last-year same period CNY | PREDICT_CONTENT text | CHANGE_REASON_EXPLAIN text
_FORECAST_TYPE_EN = {"预增": "increase", "略增": "slight_increase", "续盈": "continued_profit", "扭亏": "turnaround",
                     "预减": "decrease", "略减": "slight_decrease", "首亏": "first_loss", "续亏": "continued_loss",
                     "增亏": "larger_loss", "减亏": "smaller_loss", "不确定": "uncertain"}


def forecasts(since, ttl=1.0):
    """业绩预告 published on/after `since` (NOTICE_DATE). One row per company x period x metric.

    Keys: code, name, notice_date, report_period, metric_cn (e.g. 归属于上市公司股东的净利润), metric_code
    (004 = parent net profit), forecast_type_cn, forecast_type, profit_low, profit_high (CNY, YTD cumulative),
    yoy_low_pct, yoy_high_pct, last_year_same_period, content_cn, reason_cn.
    Filter metric_code == '004' for the net-profit forecast only.
    """
    try:
        rows, _ = dc_report("RPT_PUBLIC_OP_NEWPREDICT", "ALL", f"(NOTICE_DATE>='{since}')",
                            "NOTICE_DATE,SECURITY_CODE", "-1,1", ttl=ttl, label="forecasts")
        out = []
        for r in rows:
            if not is_a_code(r.get("SECURITY_CODE")):
                continue
            t = r.get("PREDICT_TYPE")
            out.append({
                "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"), "notice_date": day(r.get("NOTICE_DATE")),
                "report_period": day(r.get("REPORT_DATE")), "metric_cn": r.get("PREDICT_FINANCE"),
                "metric_code": r.get("PREDICT_FINANCE_CODE"), "forecast_type_cn": t,
                "forecast_type": _FORECAST_TYPE_EN.get(t, "other"),
                "profit_low": num(r.get("PREDICT_AMT_LOWER")), "profit_high": num(r.get("PREDICT_AMT_UPPER")),
                "yoy_low_pct": num(r.get("ADD_AMP_LOWER")), "yoy_high_pct": num(r.get("ADD_AMP_UPPER")),
                "last_year_same_period": num(r.get("PREYEAR_SAME_PERIOD")),
                "content_cn": r.get("PREDICT_CONTENT"), "reason_cn": r.get("CHANGE_REASON_EXPLAIN"),
            })
        return out
    except Exception as e:
        warn(f"forecasts: unexpected error {type(e).__name__}: {e}")
        return []


# ----------------------------------------------------------------------------- d. holder changes
# RPT_EXECUTIVE_HOLD_DETAILS (董监高持股变动): CHANGE_DATE | PERSON_NAME (holder) | DSE_PERSON_NAME (related 董监高)
#   PERSON_DSE_RELATION 本人/配偶/... | POSITION_NAME | CHANGE_SHARES shares (signed, <0 = sell)
#   AVERAGE_PRICE CNY | CHANGE_AMOUNT CNY | CHANGE_REASON 集中竞价/大宗交易/股权激励... | CHANGE_RATIO % of total shares
# RPT_SHARE_HOLDER_INCREASE (股东增减持, mostly >=5% holders): START_DATE/END_DATE change period | NOTICE_DATE
#   HOLDER_NAME | DIRECTION 增持/减持 | CHANGE_NUM 10k shares (unsigned) | CHANGE_NUM_SYMBOL 10k shares (signed)
#   CHANGE_RATE % of total shares (signed) | HOLD_RATIO % after | TRADE_AVERAGE_PRICE CNY (often null)
#   CLOSE_PRICE CNY | MARKET method (二级市场/大宗交易/...)
def holder_changes(since, ttl=1.0):
    """董监高 and major-shareholder 增持/减持 records with change date on/after `since`.

    Keys: source ('executive' | 'major_shareholder'), code, name, date (change date; for major_shareholder the
    END_DATE of the change period), start_date, notice_date, holder, related_executive, relation, position,
    direction ('increase'|'decrease'), shares (signed shares), avg_price, value_cny (signed; None if unknown),
    ratio_pct (change as % of total shares, signed), hold_ratio_after_pct, method_cn, ref_close_price.
    """
    out = []
    try:
        ex, _ = dc_report("RPT_EXECUTIVE_HOLD_DETAILS", "ALL", f"(CHANGE_DATE>='{since}')", "CHANGE_DATE,SECURITY_CODE",
                          "-1,1", ttl=ttl, label="holder_changes/executive")
        for r in ex:
            sh = num(r.get("CHANGE_SHARES"))
            if sh is None or not is_a_code(r.get("SECURITY_CODE")):
                continue
            amt = num(r.get("CHANGE_AMOUNT"))
            sign = -1 if sh < 0 else 1
            ratio = num(r.get("CHANGE_RATIO"))
            out.append({
                "source": "executive", "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME"),
                "date": day(r.get("CHANGE_DATE")), "start_date": None, "notice_date": None,
                "holder": r.get("PERSON_NAME"), "related_executive": r.get("DSE_PERSON_NAME"),
                "relation": r.get("PERSON_DSE_RELATION"), "position": r.get("POSITION_NAME"),
                "direction": "decrease" if sh < 0 else "increase", "shares": sh, "avg_price": num(r.get("AVERAGE_PRICE")),
                "value_cny": None if amt is None else sign * abs(amt),
                "ratio_pct": None if ratio is None else sign * abs(ratio), "hold_ratio_after_pct": None,
                "method_cn": r.get("CHANGE_REASON"), "ref_close_price": None,
            })
    except Exception as e:
        warn(f"holder_changes/executive: unexpected error {type(e).__name__}: {e}")
    try:
        mh, _ = dc_report("RPT_SHARE_HOLDER_INCREASE", "ALL", f"(END_DATE>='{since}')", "END_DATE,SECURITY_CODE",
                          "-1,1", ttl=ttl, label="holder_changes/major_shareholder")
        for r in mh:
            if not is_a_code(r.get("SECURITY_CODE")):
                continue
            d = r.get("DIRECTION")
            sh = scale(r.get("CHANGE_NUM_SYMBOL"), 1e4)
            if sh is None:
                n = scale(r.get("CHANGE_NUM"), 1e4)
                sh = None if n is None else (-abs(n) if d == "减持" else abs(n))
            px = num(r.get("TRADE_AVERAGE_PRICE"))
            out.append({
                "source": "major_shareholder", "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"),
                "date": day(r.get("END_DATE")), "start_date": day(r.get("START_DATE")), "notice_date": day(r.get("NOTICE_DATE")),
                "holder": r.get("HOLDER_NAME"), "related_executive": None, "relation": None, "position": None,
                "direction": "decrease" if d == "减持" else "increase" if d == "增持" else "unknown",
                "shares": sh, "avg_price": px, "value_cny": (sh * px) if (sh is not None and px) else None,
                "ratio_pct": num(r.get("CHANGE_RATE")), "hold_ratio_after_pct": num(r.get("HOLD_RATIO")),
                "method_cn": r.get("MARKET"), "ref_close_price": num(r.get("CLOSE_PRICE")),
            })
    except Exception as e:
        warn(f"holder_changes/major_shareholder: unexpected error {type(e).__name__}: {e}")
    return out


# ----------------------------------------------------------------------------- e. pledge
# RPT_CSDC_LIST (中登股权质押, weekly): TRADE_DATE stats date | PLEDGE_RATIO % of total shares
#   REPURCHASE_BALANCE pledged shares in 10k | PLEDGE_DEAL_NUM number of pledges | PLEDGE_MARKET_CAP 10k CNY
#   REPURCHASE_UNLIMITED_BALANCE / REPURCHASE_LIMITED_BALANCE unrestricted / restricted pledged (10k shares)
def pledge(ttl=1.0):
    """Latest weekly 股权质押 snapshot (ChinaClear statistics via Eastmoney). Only companies with pledges appear.

    Keys: code, name, stats_date, pledge_ratio_pct, pledged_shares, pledged_unrestricted_shares,
    pledged_restricted_shares, pledge_count, pledged_mcap (CNY), industry.
    """
    try:
        head, _ = dc_report("RPT_CSDC_LIST", "TRADE_DATE", "", "TRADE_DATE", "-1", page_size=1, max_pages=1,
                            ttl=ttl, label="pledge/latest-date", warn_truncated=False)
        if not head:
            warn("pledge: could not determine latest stats date")
            return []
        latest = day(head[0].get("TRADE_DATE"))
        rows, _ = dc_report("RPT_CSDC_LIST", "ALL", f"(TRADE_DATE='{latest}')", "SECURITY_CODE", "1", ttl=ttl,
                            label="pledge")
        return [{
            "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"), "stats_date": day(r.get("TRADE_DATE")),
            "pledge_ratio_pct": num(r.get("PLEDGE_RATIO")), "pledged_shares": scale(r.get("REPURCHASE_BALANCE"), 1e4),
            "pledged_unrestricted_shares": scale(r.get("REPURCHASE_UNLIMITED_BALANCE"), 1e4),
            "pledged_restricted_shares": scale(r.get("REPURCHASE_LIMITED_BALANCE"), 1e4),
            "pledge_count": num(r.get("PLEDGE_DEAL_NUM")), "pledged_mcap": scale(r.get("PLEDGE_MARKET_CAP"), 1e4),
            "industry": r.get("INDUSTRY"),
        } for r in rows if is_a_code(r.get("SECURITY_CODE"))]
    except Exception as e:
        warn(f"pledge: unexpected error {type(e).__name__}: {e}")
        return []


# ----------------------------------------------------------------------------- f. unlocks
# RPT_LIFT_STAGE (限售股解禁): FREE_DATE | CURRENT_FREE_SHARES 10k shares unlocking | ABLE_FREE_SHARES 10k actually tradable
#   TOTAL_RATIO fraction of total shares (0.0357 = 3.57%) | FREE_RATIO fraction of float | LIFT_MARKET_CAP 10k CNY
#   FREE_SHARES_TYPE 首发原股东限售/定增/股权激励... | BATCH_HOLDER_NUM holders in batch | NEW reference price
def unlocks(start, end, ttl=1.0):
    """限售股解禁 schedule with FREE_DATE in [start, end].

    Keys: code, name, date, shares, tradable_shares, ratio_total_pct, ratio_float_pct, unlock_value (CNY at the
    reference price Eastmoney used), holders, type_cn.
    """
    try:
        rows, _ = dc_report("RPT_LIFT_STAGE", "ALL", f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')",
                            "FREE_DATE,SECURITY_CODE", "1,1", ttl=ttl, label="unlocks")
        return [{
            "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"), "date": day(r.get("FREE_DATE")),
            "shares": scale(r.get("CURRENT_FREE_SHARES"), 1e4), "tradable_shares": scale(r.get("ABLE_FREE_SHARES"), 1e4),
            "ratio_total_pct": scale(r.get("TOTAL_RATIO"), 100), "ratio_float_pct": scale(r.get("FREE_RATIO"), 100),
            "unlock_value": scale(r.get("LIFT_MARKET_CAP"), 1e4), "holders": num(r.get("BATCH_HOLDER_NUM")),
            "type_cn": r.get("FREE_SHARES_TYPE"),
        } for r in rows if is_a_code(r.get("SECURITY_CODE"))]
    except Exception as e:
        warn(f"unlocks: unexpected error {type(e).__name__}: {e}")
        return []


# ----------------------------------------------------------------------------- g. regulatory (cninfo)
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
# Title keyword -> kind. Title search only (tabName=fulltext + searchkey matches titles); classified afterwards.
REG_KEYWORDS = ["问询函", "关注函", "监管函", "警示函", "立案", "纪律处分", "行政处罚"]


def _reg_kind(title):
    t = title
    if "立案" in t:
        return "investigation" if ("调查" in t or "证监会" in t or "监督管理委员会" in t) else "case_filing_other"
    if "审核问询" in t or "审核中心意见" in t:
        return "review_inquiry"  # IPO / refinancing review questions - routine, not a disciplinary signal
    if "问询函" in t:
        return "inquiry_letter"
    if "关注函" in t:
        return "concern_letter"
    if "监管函" in t or "监管工作函" in t:
        return "regulatory_letter"
    if "警示函" in t:
        return "warning_letter"
    if "纪律处分" in t:
        return "disciplinary_action"
    if "行政处罚" in t:
        return "administrative_penalty"
    return "other"


def regulatory(since, until=None, keywords=None, max_pages_per_keyword=20, ttl=1.0):
    """交易所问询函 / 关注函 / 监管函 / 警示函 / 立案调查 / 纪律处分 / 行政处罚 announcements since a date (cninfo).

    Company-side announcements whose TITLE contains a keyword (the company discloses the letter, its reply, or
    the investigation notice). Exchange-side letters not re-published by the company are NOT covered.
    Keys: code, name, date, title_cn, kind, keyword_cn, is_reply, announcement_id, url.
    `kind` = investigation | inquiry_letter | review_inquiry | concern_letter | regulatory_letter | warning_letter |
    disciplinary_action | administrative_penalty | case_filing_other | other.
    cninfo returns 30 rows/page; capped at `max_pages_per_keyword` pages per keyword (warning if truncated).
    """
    until = until or dt.datetime.now(CST).strftime("%Y-%m-%d")
    out, seen = [], set()
    for kw in keywords or REG_KEYWORDS:
        try:
            page, total_pages = 1, 1
            while page <= min(total_pages, max_pages_per_keyword):
                form = {"pageNum": page, "pageSize": 30, "column": "szse", "tabName": "fulltext", "plate": "",
                        "stock": "", "searchkey": kw, "secid": "", "category": "", "trade": "",
                        "seDate": f"{since}~{until}", "sortName": "time", "sortType": "desc", "isHLtitle": "true"}
                d = fetch_json(CNINFO_QUERY, headers=CNINFO_HEADERS, data=form, ttl=ttl)
                if d is None:
                    warn(f"regulatory: keyword page {page} failed")
                    break
                anns = d.get("announcements") or []
                total = d.get("totalAnnouncement") or d.get("totalRecordNum") or 0
                total_pages = (total + 29) // 30
                for a in anns:
                    aid = a.get("announcementId")
                    code = a.get("secCode")
                    if aid in seen or not is_a_code(code):
                        continue
                    seen.add(aid)
                    title = re.sub(r"</?em>", "", a.get("announcementTitle") or "")
                    ts = a.get("announcementTime")
                    date = dt.datetime.fromtimestamp(ts / 1000, CST).strftime("%Y-%m-%d") if isinstance(ts, (int, float)) else None
                    out.append({
                        "code": code, "name": a.get("secName"), "date": date, "title_cn": title,
                        "kind": _reg_kind(title), "keyword_cn": kw, "is_reply": "回复" in title,
                        "announcement_id": aid,
                        "url": ("https://static.cninfo.com.cn/" + a["adjunctUrl"]) if a.get("adjunctUrl") else None,
                    })
                if not anns:
                    break
                page += 1
            if total_pages > max_pages_per_keyword:
                warn(f"regulatory: a keyword was truncated at {max_pages_per_keyword} of {total_pages} pages")
        except Exception as e:
            warn(f"regulatory: unexpected error {type(e).__name__}: {e}")
    return out


# ----------------------------------------------------------------------------- h. valuation history
# RPT_VALUEANALYSIS_DET (估值分析, daily): TRADE_DATE | CLOSE_PRICE | PE_TTM | PE_LAR 静态PE | PB_MRQ | PS_TTM
#   PCF_OCF_TTM 市现率(经营现金流TTM) | PEG_CAR | TOTAL_MARKET_CAP CNY | NOTLIMITED_MARKETCAP_A float A mcap CNY
def valuation_history(code, years=5, ttl=1.0):
    """Daily PE(TTM)/PE(static)/PB(MRQ)/PS(TTM)/PCF(TTM) history for one stock, oldest first.

    Keys: date, close, pe_ttm, pe_static, pb, ps_ttm, pcf_ocf_ttm, total_mcap. Available history starts ~2018.
    Negative PE means losses (kept as-is; see valuation_percentiles).
    """
    try:
        start = (dt.date.today() - dt.timedelta(days=int(years * 365.25))).isoformat()
        rows, _ = dc_report("RPT_VALUEANALYSIS_DET",
                            "TRADE_DATE,CLOSE_PRICE,PE_TTM,PE_LAR,PB_MRQ,PS_TTM,PCF_OCF_TTM,TOTAL_MARKET_CAP",
                            f"(SECURITY_CODE=\"{code}\")(TRADE_DATE>='{start}')", "TRADE_DATE", "1", ttl=ttl,
                            label=f"valuation_history {code}")
        return [{"date": day(r.get("TRADE_DATE")), "close": num(r.get("CLOSE_PRICE")), "pe_ttm": num(r.get("PE_TTM")),
                 "pe_static": num(r.get("PE_LAR")), "pb": num(r.get("PB_MRQ")), "ps_ttm": num(r.get("PS_TTM")),
                 "pcf_ocf_ttm": num(r.get("PCF_OCF_TTM")), "total_mcap": num(r.get("TOTAL_MARKET_CAP"))} for r in rows]
    except Exception as e:
        warn(f"valuation_history: unexpected error {type(e).__name__}: {e}")
        return []


def valuation_percentiles(history, keys=("pe_ttm", "pb", "ps_ttm")):
    """Percentile (0-100) of the latest value within the history, per metric.

    Non-positive values are excluded (PE < 0 = loss-making, not 'cheap'); `n` reports the sample size and
    `latest_valid` is False when the latest value itself is non-positive (percentile then None).
    Returns a list with one dict per metric: metric, latest, percentile, n, min, median, max, latest_valid.
    """
    out = []
    for k in keys:
        vals = [h[k] for h in history if h.get(k) is not None and h[k] > 0]
        latest = history[-1].get(k) if history else None
        if not vals:
            out.append({"metric": k, "latest": latest, "percentile": None, "n": 0, "min": None, "median": None,
                        "max": None, "latest_valid": False})
            continue
        s = sorted(vals)
        valid = latest is not None and latest > 0
        pct = 100.0 * sum(1 for v in s if v <= latest) / len(s) if valid else None
        out.append({"metric": k, "latest": latest, "percentile": None if pct is None else round(pct, 1), "n": len(s),
                    "min": s[0], "median": s[len(s) // 2], "max": s[-1], "latest_valid": valid})
    return out


# ----------------------------------------------------------------------------- i. per-stock F10 data (evaluation cards)
def secucode(code):
    """'300191' -> '300191.SZ' (datacenter F10 reports filter on SECUCODE)."""
    return f"{code}.{market_of(code)}"


def _clean_text(s):
    return re.sub(r"\s+", " ", s).strip() if isinstance(s, str) else None


# RPT_F10_BASIC_ORGINFO (F10 公司概况): ORG_NAME 公司全称 | MAIN_BUSINESS 主营业务 (one or two sentences)
#   ORG_PROFILE 公司简介 (long text) | BUSINESS_SCOPE 经营范围 | EM2016 东财行业 (3 levels) | INDUSTRYCSRC1 证监会行业
#   LISTING_DATE | FOUND_DATE | EMP_NUM 员工人数 | CHAIRMAN 董事长 | PRESIDENT 总经理 | SECRETARY 董秘
#   ACTUAL_HOLDER 实际控制人 | PROVINCE | ORG_WEB | ACCOUNTFIRM_NAME 会计师事务所
def company_profile(code, ttl=30.0):
    """F10 company survey for one stock. Returns [] or [dict] with keys: code, name, org_name, main_business,
    profile, business_scope, industry_em, industry_csrc, listing_date, found_date, employees, chairman, president,
    secretary, actual_holder, province, website, auditor. Texts are as published by Eastmoney F10 (Chinese)."""
    try:
        rows, _ = dc_report("RPT_F10_BASIC_ORGINFO", "ALL", f"(SECUCODE=\"{secucode(code)}\")", page_size=5, max_pages=1,
                            ttl=ttl, label=f"company_profile {code}", warn_truncated=False)
        return [{
            "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"), "org_name": r.get("ORG_NAME"),
            "main_business": _clean_text(r.get("MAIN_BUSINESS")), "profile": _clean_text(r.get("ORG_PROFILE")),
            "business_scope": _clean_text(r.get("BUSINESS_SCOPE")), "industry_em": r.get("EM2016"),
            "industry_csrc": r.get("INDUSTRYCSRC1"), "listing_date": day(r.get("LISTING_DATE")),
            "found_date": day(r.get("FOUND_DATE")), "employees": num(r.get("EMP_NUM")), "chairman": r.get("CHAIRMAN"),
            "president": r.get("PRESIDENT"), "secretary": r.get("SECRETARY"), "actual_holder": r.get("ACTUAL_HOLDER"),
            "province": r.get("PROVINCE"), "website": r.get("ORG_WEB"), "auditor": r.get("ACCOUNTFIRM_NAME"),
        } for r in rows[:1]]
    except Exception as e:
        warn(f"company_profile: unexpected error {type(e).__name__}: {e}")
        return []


# RPT_F10_FN_MAINOP (F10 主营构成): REPORT_DATE | REPORT_NAME (e.g. 2026中报) | MAINOP_TYPE 1=按行业 2=按产品 3=按地区
#   ITEM_NAME | MAIN_BUSINESS_INCOME revenue CNY | MBI_RATIO share of revenue (fraction) | MAIN_BUSINESS_COST CNY
#   GROSS_RPOFIT_RATIO gross margin (fraction; sic) | RANK within type
_MAINOP_TYPE = {"1": "industry", "2": "product", "3": "region"}


def main_business_composition(code, ttl=7.0, history_rows=120):
    """主营构成 for one stock: for EACH breakdown type (industry / product / region) the rows of the latest report
    date that has that type (interim reports often only break down by product, so dates can differ by type).

    Keys: code, report_date, report_name, type ('industry'|'product'|'region'), item, revenue, cost,
    share_pct, gross_margin_pct, rank. Amounts CNY; share/margin converted from fractions to percent.
    """
    try:
        rows, _ = dc_report("RPT_F10_FN_MAINOP", "SECURITY_CODE,REPORT_DATE,REPORT_NAME,MAINOP_TYPE,ITEM_NAME,"
                            "MAIN_BUSINESS_INCOME,MBI_RATIO,MAIN_BUSINESS_COST,GROSS_RPOFIT_RATIO,RANK",
                            f"(SECUCODE=\"{secucode(code)}\")", "REPORT_DATE,MAINOP_TYPE,RANK", "-1,1,1",
                            page_size=history_rows, max_pages=1, ttl=ttl, label=f"main_business {code}", warn_truncated=False)
        latest = {}
        for r in rows:
            t = _MAINOP_TYPE.get(str(r.get("MAINOP_TYPE")))
            d = day(r.get("REPORT_DATE"))
            if t and d and d > latest.get(t, ""):
                latest[t] = d
        out = []
        for r in rows:
            t = _MAINOP_TYPE.get(str(r.get("MAINOP_TYPE")))
            if not t or day(r.get("REPORT_DATE")) != latest.get(t):
                continue
            sh, gm = num(r.get("MBI_RATIO")), num(r.get("GROSS_RPOFIT_RATIO"))
            out.append({"code": r.get("SECURITY_CODE"), "report_date": day(r.get("REPORT_DATE")),
                        "report_name": r.get("REPORT_NAME"), "type": t, "item": r.get("ITEM_NAME"),
                        "revenue": num(r.get("MAIN_BUSINESS_INCOME")), "cost": num(r.get("MAIN_BUSINESS_COST")),
                        "share_pct": None if sh is None else sh * 100, "gross_margin_pct": None if gm is None else gm * 100,
                        "rank": num(r.get("RANK"))})
        return out
    except Exception as e:
        warn(f"main_business_composition: unexpected error {type(e).__name__}: {e}")
        return []


# RPT_F10_EH_FREEHOLDERS (F10 前十大流通股东): END_DATE period end | NOTICE_DATE | HOLDER_RANK | HOLDER_NAME
#   HOLDER_TYPE (个人/证券投资基金/...) | HOLD_NUM shares | FREE_HOLDNUM_RATIO % of float A shares
#   HOLD_NUM_CHANGE text: '不变' | '新进' | signed share delta | XZCHANGE signed share delta (None for 新进)
#   CHANGE_RATIO % change of the holding vs prior period
def float_holders(code, ttl=7.0):
    """Top-10 float shareholders, latest period, with change vs the prior period.

    Returns rows of the latest END_DATE (status 'new' | 'unchanged' | 'increase' | 'decrease') plus rows with
    status 'exited' for holders that were top-10 in the prior period but not in the latest (their prior holding).
    Keys: code, end_date, prior_end_date, notice_date, rank, holder, holder_type, shares, float_ratio_pct,
    change_shares (signed; None for new/exited), change_pct, status.
    """
    try:
        rows, _ = dc_report("RPT_F10_EH_FREEHOLDERS", "SECURITY_CODE,END_DATE,NOTICE_DATE,HOLDER_RANK,HOLDER_NAME,"
                            "HOLDER_TYPE,HOLD_NUM,FREE_HOLDNUM_RATIO,HOLD_NUM_CHANGE,XZCHANGE,CHANGE_RATIO",
                            f"(SECUCODE=\"{secucode(code)}\")", "END_DATE,HOLDER_RANK", "-1,1", page_size=30, max_pages=1,
                            ttl=ttl, label=f"float_holders {code}", warn_truncated=False)
        dates = sorted({day(r.get("END_DATE")) for r in rows if day(r.get("END_DATE"))}, reverse=True)
        if not dates:
            return []
        cur, prior = dates[0], (dates[1] if len(dates) > 1 else None)
        cur_rows = [r for r in rows if day(r.get("END_DATE")) == cur]
        names = {r.get("HOLDER_NAME") for r in cur_rows}
        out = []
        for r in cur_rows:
            raw = str(r.get("HOLD_NUM_CHANGE") or "")
            delta = num(r.get("XZCHANGE"))
            if delta is None:
                delta = num(raw)
            if raw == "新进":
                status, delta = "new", None
            elif raw == "不变" or delta == 0:
                status = "unchanged"
            elif delta is not None:
                status = "increase" if delta > 0 else "decrease"
            else:
                status = "unknown"
            out.append({"code": r.get("SECURITY_CODE"), "end_date": cur, "prior_end_date": prior,
                        "notice_date": day(r.get("NOTICE_DATE")), "rank": num(r.get("HOLDER_RANK")),
                        "holder": r.get("HOLDER_NAME"), "holder_type": r.get("HOLDER_TYPE"), "shares": num(r.get("HOLD_NUM")),
                        "float_ratio_pct": num(r.get("FREE_HOLDNUM_RATIO")), "change_shares": delta,
                        "change_pct": num(r.get("CHANGE_RATIO")), "status": status})
        if prior:
            for r in rows:
                if day(r.get("END_DATE")) == prior and r.get("HOLDER_NAME") not in names:
                    out.append({"code": r.get("SECURITY_CODE"), "end_date": prior, "prior_end_date": prior,
                                "notice_date": day(r.get("NOTICE_DATE")), "rank": num(r.get("HOLDER_RANK")),
                                "holder": r.get("HOLDER_NAME"), "holder_type": r.get("HOLDER_TYPE"),
                                "shares": num(r.get("HOLD_NUM")), "float_ratio_pct": num(r.get("FREE_HOLDNUM_RATIO")),
                                "change_shares": None, "change_pct": None, "status": "exited"})
        return out
    except Exception as e:
        warn(f"float_holders: unexpected error {type(e).__name__}: {e}")
        return []


# RPT_WEB_RESPREDICT (机构盈利预测汇总, one row per stock): RATING_ORG_NUM institutions with a rating
#   RATING_BUY_NUM / RATING_ADD_NUM / RATING_NEUTRAL_NUM / RATING_REDUCE_NUM / RATING_SALE_NUM rating counts
#   YEAR1..YEAR4 + YEAR_MARK1..4 ('A' actual, 'E' estimate) + EPS1..EPS4 (consensus mean EPS, CNY)
#   DEC_AIMPRICEMAX / DEC_AIMPRICEMIN target-price range (kept in the data layer; the dashboard does not show it)
def analyst_consensus(code, ttl=1.0):
    """Consensus EPS forecasts and rating counts. [] when no institution covers the stock (common for small caps).
    Keys: code, org_num, rating_counts {buy, add, neutral, reduce, sell}, eps [{year, kind ('actual'|'estimate'), eps}],
    target_max, target_min. The look-back window Eastmoney uses for the aggregation is not documented."""
    try:
        rows, _ = dc_report("RPT_WEB_RESPREDICT", "ALL", f"(SECUCODE=\"{secucode(code)}\")", page_size=5, max_pages=1,
                            ttl=ttl, label=f"analyst_consensus {code}", warn_truncated=False)
        out = []
        for r in rows[:1]:
            eps = []
            for i in range(1, 5):
                y, e = r.get(f"YEAR{i}"), num(r.get(f"EPS{i}"))
                if y is not None and e is not None:
                    eps.append({"year": int(y), "kind": "actual" if r.get(f"YEAR_MARK{i}") == "A" else "estimate", "eps": e})
            out.append({"code": r.get("SECURITY_CODE"), "org_num": num(r.get("RATING_ORG_NUM")),
                        "rating_counts": {k: num(r.get(f)) or 0 for k, f in (
                            ("buy", "RATING_BUY_NUM"), ("add", "RATING_ADD_NUM"), ("neutral", "RATING_NEUTRAL_NUM"),
                            ("reduce", "RATING_REDUCE_NUM"), ("sell", "RATING_SALE_NUM"))},
                        "eps": eps, "target_max": num(r.get("DEC_AIMPRICEMAX")), "target_min": num(r.get("DEC_AIMPRICEMIN"))})
        return out
    except Exception as e:
        warn(f"analyst_consensus: unexpected error {type(e).__name__}: {e}")
        return []


# ----------------------------------------------------------------------------- j. forward-adjusted daily kline (52-week range)
# push2his /api/qt/stock/kline/get (verified 2026-10-22 against 600519): klt=101 daily, fqt=1 前复权 (qfq: the latest
#   bar is the actual traded price, earlier bars are scaled for dividends / 送转), secid = market id . code
#   (1 = SH, 0 = SZ / BJ). fields2: f51 date | f52 open | f53 close | f54 high | f55 low (intraday, qfq).
#   During verification the HTTPS endpoint (and push2 / push2delay hosts) answered every request with a dropped
#   connection / HTTP 502 while plain HTTP on push2his.eastmoney.com worked, so HTTP is tried first. The response is
#   public market data; no credentials are sent.
KLINE_PATH = ("/api/qt/stock/kline/get?secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3"
              "&fields2=f51,f52,f53,f54,f55&klt=101&fqt=1&beg={beg}&end={end}&lmt=1000")
KLINE_HOSTS = ["http://push2his.eastmoney.com", "https://push2his.eastmoney.com"]


def secid(code):
    return f"{1 if market_of(code) == 'SH' else 0}.{code}"


def kline_url(code, beg, end, host=0):
    return KLINE_HOSTS[host] + KLINE_PATH.format(secid=secid(code), beg=beg.replace("-", ""), end=end.replace("-", ""))


def daily_kline_qfq(code, beg, end, ttl=1.0, quiet=False):
    """Forward-adjusted (前复权) daily bars for one stock between beg and end ('YYYY-MM-DD', inclusive), oldest first.

    Returns (rows, ok). rows: [{date, open, close, high, low}] (CNY, qfq basis anchored at the latest bar the server
    knows, i.e. at fetch time). ok=False => request failed (rows []); ok=True with rows [] => no bars in the window
    (not listed yet / suspended for the whole window)."""
    urls = [kline_url(code, beg, end, i) for i in range(len(KLINE_HOSTS))]
    d = fetch_json(urls[0], ttl=ttl, alt_urls=urls + urls[:1], quiet=quiet)
    if d is None:
        return [], False
    try:
        out = []
        for line in ((d.get("data") or {}).get("klines") or []):
            p = line.split(",")
            if len(p) < 5:
                continue
            out.append({"date": p[0], "open": num(p[1]), "close": num(p[2]), "high": num(p[3]), "low": num(p[4])})
        return out, True
    except Exception as e:
        warn(f"daily_kline_qfq {code}: unexpected error {type(e).__name__}: {e}")
        return [], False


# ----------------------------------------------------------------------------- k. per-report analyst targets
# reportapi.eastmoney.com/report/list qType=0 (个股研报, verified 2026-10-22 against 600519): one row per report.
#   publishDate | orgCode | orgSName 机构简称 | infoCode (report page data.eastmoney.com/report/info/<infoCode>.html)
#   indvAimPriceT / indvAimPriceL 目标价（上 / 下限，CNY; '' when the report gives no target - most reports)
#   emRatingName 评级 (kept in the data layer only; the dashboard does not show rating words) | title
REPORT_URL = "https://reportapi.eastmoney.com/report/list"


def research_reports(code, begin, end, ttl=1.0, max_pages=5):
    """Individual-stock research reports published between begin and end ('YYYY-MM-DD'), newest first.
    Keys: code, date, org_code, org, title, info_code, url, target_hi, target_lo (None when not given), rating.
    Returns (rows, ok)."""
    out, page, pages = [], 1, 1
    try:
        while page <= min(pages, max_pages):
            q = {"industryCode": "*", "pageSize": 100, "industry": "*", "rating": "*", "ratingChange": "*",
                 "beginTime": begin, "endTime": end, "pageNo": page, "fields": "", "qType": 0, "orgCode": "",
                 "code": code, "rcode": "", "p": page, "pageNum": page, "pageNumber": page}
            d = fetch_json(REPORT_URL + "?" + urllib.parse.urlencode(q), ttl=ttl)
            if d is None:
                return out, False
            for r in d.get("data") or []:
                if r.get("stockCode") != code:
                    continue
                ic = r.get("infoCode")
                out.append({"code": code, "date": day(r.get("publishDate")), "org_code": r.get("orgCode"),
                            "org": r.get("orgSName") or r.get("orgName"), "title": r.get("title"), "info_code": ic,
                            "url": f"https://data.eastmoney.com/report/info/{ic}.html" if ic else None,
                            "target_hi": num(r.get("indvAimPriceT")), "target_lo": num(r.get("indvAimPriceL")),
                            "rating": r.get("emRatingName")})
            pages = d.get("TotalPage") or 1
            page += 1
        out.sort(key=lambda r: r.get("date") or "", reverse=True)
        return out, True
    except Exception as e:
        warn(f"research_reports {code}: unexpected error {type(e).__name__}: {e}")
        return out, False


# RPT_SHAREBONUS_DET (分红送配): EX_DIVIDEND_DATE 除权除息日 | BONUS_IT_RATIO 每 10 股送转合计 | BONUS_RATIO 送股
#   IT_RATIO 转增 | PRETAX_BONUS_RMB 每 10 股派现（税前，CNY）| ASSIGN_PROGRESS 方案进度 | IMPL_PLAN_PROFILE 方案文字
def share_bonus(code, since, ttl=7.0):
    """Implemented dividend / 送转 events with an ex-date >= since. Keys: code, ex_date, bonus_it_per10,
    cash_per10, plan. Used to detect price-basis breaks for analyst targets. Returns (rows, ok)."""
    try:
        rows, ok = dc_report("RPT_SHAREBONUS_DET", "SECURITY_CODE,EX_DIVIDEND_DATE,BONUS_IT_RATIO,PRETAX_BONUS_RMB,"
                             "ASSIGN_PROGRESS,IMPL_PLAN_PROFILE", f"(SECURITY_CODE=\"{code}\")(EX_DIVIDEND_DATE>='{since}')",
                             "EX_DIVIDEND_DATE", "-1", page_size=20, max_pages=1, ttl=ttl, label=f"share_bonus {code}",
                             warn_truncated=False)
        return [{"code": code, "ex_date": day(r.get("EX_DIVIDEND_DATE")), "bonus_it_per10": num(r.get("BONUS_IT_RATIO")),
                 "cash_per10": num(r.get("PRETAX_BONUS_RMB")), "plan": r.get("IMPL_PLAN_PROFILE")}
                for r in rows if day(r.get("EX_DIVIDEND_DATE"))], ok
    except Exception as e:
        warn(f"share_bonus {code}: unexpected error {type(e).__name__}: {e}")
        return [], False
