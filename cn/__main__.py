"""Command line entry point for the A-share pipeline (research tooling, not investment advice).

    python -m cn fetch   pull data via cn/em.py, save data/cn/snap_<ts>.json (then runs `cards`)
    python -m cn cards   per-stock extras for the evaluation cards, save data/cn/cards_<ts>.json
    python -m cn ranges  52-week qfq high / low for the screen rows, save data/cn/ranges_<ts>.json (optional: N codes)
    python -m cn holders tracked holders in the top-10 float holders, last 5 report dates, save data/cn/holders_<ts>.json
    python -m cn valuation  现金流估值 inputs (cash flow, revenue, net cash, China 10y yield), save data/cn/valuation_<ts>.json
    python -m cn ledger  估值台账: append default-parameter valuation records to data/cn/ledger/cn_ledger.jsonl (offline, idempotent)
    python -m cn forward forward tracking of the ledger tags (price after each record vs the same-date median), offline
    python -m cn build   render site/cn/index.html from the latest snapshot + cards + ranges + holders + valuation files + ledger (offline)
    python -m cn.smoke   data-layer smoke test
"""
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "fetch":
        from .build import fetch
        fetch()
    elif cmd == "cards":
        from .cards import fetch_cards
        if not fetch_cards(sys.argv[2] if len(sys.argv) > 2 else None):
            sys.exit(1)
    elif cmd == "ranges":
        from .ranges import fetch_ranges
        if not fetch_ranges(int(sys.argv[2]) if len(sys.argv) > 2 else None):
            sys.exit(1)
    elif cmd == "holders":
        from .holders import fetch_holders
        if not fetch_holders():
            sys.exit(1)
    elif cmd == "valuation":
        from .valuation import fetch_valuation
        if not fetch_valuation():
            sys.exit(1)
    elif cmd == "ledger":
        from .ledger import run
        if run() is None:
            sys.exit(1)
    elif cmd == "forward":
        from .forward import run
        run()
    elif cmd == "build":
        from .build import build
        build(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
