"""前向跟踪 (forward tracking) of the A-share ledger tags: price return after each ledger record vs the same-date median.
    python -m cn forward   data/cn/ledger/cn_ledger.jsonl -> data/cn/ledger/cn_forward.json (samples + aggregates), offline
Display / research only: nothing here enters the ③ priority score, the 价值 / 博弈 label, warn_n or the 对比池 bkScore.
The statistics are radar/forward.py evaluate() (one implementation for both markets; method and caveats documented there).
A-share specifics:
  * start date = the record's quote date; price = the unadjusted snapshot quote (not 前复权), dividends not included;
  * a sample is excluded when the share count moved > 25% between the two records (送转 / 拆股 / 大额增发: unadjusted prices
    would show a fake drop);
  * peers = the same industry (申万-style industry of the screen row); no index benchmark (沪深300 is not fetched), the
    baseline is the equal-weight median of all ledger companies on the same start date.
stdout is English only.
"""
import datetime as dt
import json
import os
from radar import forward as F
from . import ledger as L
OUT = os.path.join(L.LEDGER_DIR, "cn_forward.json")


def series(recs):
    by = {}
    for x in recs:
        d = x.get("date")
        if not d:
            continue
        by.setdefault(x["code"], {})[d] = {"d": d, "p": x.get("price"), "sh": x.get("shares"), "ccy": None, "grp": x.get("industry"),
                                            "tags": [t[1] for t in L.tags(L.tag_input(x))], "run": f"{x.get('snap_file')}|{x.get('val_file')}"}
    return {c: list(v.values()) for c, v in by.items()}


def compute(path=L.LEDGER_FILE):
    return F.evaluate(series(L.load(path)))


def run(path=L.LEDGER_FILE):
    res = compute(path)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({**res, "computed_at": dt.datetime.now().isoformat(timespec="seconds"), "ledger": os.path.basename(path)}, f, ensure_ascii=False)
    print(F.summary(res), flush=True)
    return res


def page_payload(path=L.LEDGER_FILE):
    return F.page(compute(path))
