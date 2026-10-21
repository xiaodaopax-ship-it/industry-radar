"""HTTP helpers: SEC (rate-limited, identified) and Yahoo Finance (cookie + crumb session)."""
import hashlib
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .paths import CACHE, CFG


def sec_user_agent():
    ua = os.environ.get("SEC_USER_AGENT") or CFG.get("sec_user_agent", "")
    if not ua or "example.com" in ua:
        raise SystemExit("Set SEC_USER_AGENT, e.g. 'industry-radar you@yourmail.com' (SEC requires a contact).")
    return ua


def _open(opener, req, timeout):
    return opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)


def get(url, headers=None, as_json=True, retries=3, opener=None, data=None, timeout=60):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"}, data=data)
            with _open(opener, req, timeout) as r:
                body = r.read()
            return json.loads(body) if as_json else body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))


_SEC_LOCK, _SEC_LAST = threading.Lock(), [0.0]


def sec(url, as_json=True):
    """SEC fair access: identify yourself and stay under 10 requests/second (shared across threads)."""
    with _SEC_LOCK:
        wait = 0.12 - (time.time() - _SEC_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _SEC_LAST[0] = time.time()
    return get(url, {"User-Agent": sec_user_agent()}, as_json)


class Yahoo:
    """Unofficial Yahoo Finance endpoints. They can change without notice; every caller handles None."""

    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("User-Agent", "Mozilla/5.0")]
        try:
            self.op.open("https://fc.yahoo.com", timeout=15)
        except Exception:
            pass  # 404 is expected; the session cookie is still set
        self.crumb = self.op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode()
        self._lock, self._last = threading.Lock(), 0.0

    def _get(self, url, data=None):
        key = hashlib.sha1((url + (data.decode() if data else "")).encode()).hexdigest()
        path = os.path.join(CACHE, "yh", key + ".json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < CFG["cache_days"]["yahoo"] * 86400:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        d = self._fetch(url, data)
        if d is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        return d

    def _fetch(self, url, data=None):
        with self._lock:  # be polite: ~4 requests/second
            wait = 0.25 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        sep = "&" if "?" in url else "?"
        headers = {"User-Agent": "Mozilla/5.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            return get(f"{url}{sep}crumb={urllib.parse.quote(self.crumb)}", headers, opener=self.op, data=data, retries=2, timeout=25)
        except Exception:
            return None

    def summary(self, symbol, modules):
        d = self._get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}?modules={modules}")
        try:
            return d["quoteSummary"]["result"][0]
        except (TypeError, KeyError, IndexError):
            return None

    def timeseries(self, symbol, types, years=3):
        p2 = int(time.time()) // 86400 * 86400 + 86400  # rounded to the day so the URL (and cache key) is stable
        p1 = p2 - years * 366 * 86400
        d = self._get(f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{urllib.parse.quote(symbol)}"
                      f"?type={','.join(types)}&period1={p1}&period2={p2}")
        out = {}
        for s in ((d or {}).get("timeseries") or {}).get("result") or []:
            k = s["meta"]["type"][0]
            out[k] = [{"date": v["asOfDate"], "val": v["reportedValue"]["raw"], "ccy": v.get("currencyCode")}
                      for v in (s.get(k) or []) if v and v.get("reportedValue")]
        return out

    def chart(self, symbol, rng="1y"):
        d = self._get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
        try:
            r = d["chart"]["result"][0]
        except (TypeError, KeyError, IndexError):
            return None
        off = r["meta"].get("gmtoffset", 0)
        closes = r["indicators"]["quote"][0]["close"]
        import datetime as dt
        return {"meta": {"price": r["meta"].get("regularMarketPrice"), "ccy": r["meta"].get("currency")},
                "px": [[dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d"), round(c, 4)]
                       for t, c in zip(r.get("timestamp", []), closes) if c is not None]}

    def screener(self, region, industry, size=250):
        body = {"size": size, "offset": 0, "sortField": "intradaymarketcap", "sortType": "DESC", "quoteType": "EQUITY",
                "query": {"operator": "AND", "operands": [{"operator": "eq", "operands": ["region", region]},
                                                          {"operator": "eq", "operands": ["industry", industry]}]}}
        d = self._get("https://query2.finance.yahoo.com/v1/finance/screener", data=json.dumps(body).encode())
        try:
            return d["finance"]["result"][0]["quotes"]
        except (TypeError, KeyError, IndexError):
            return []


def raw(d, k):
    """Yahoo wraps numbers as {"raw": .., "fmt": ..}."""
    v = (d or {}).get(k)
    return v.get("raw") if isinstance(v, dict) else None
