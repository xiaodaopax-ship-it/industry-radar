"""Command line entry point for the A-share pipeline (research tooling, not investment advice).

    python -m cn fetch   pull data via cn/em.py, save data/cn/snap_<ts>.json (then runs `cards`)
    python -m cn cards   per-stock extras for the evaluation cards, save data/cn/cards_<ts>.json
    python -m cn build   render site/cn/index.html from the latest snapshot + cards file (offline)
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
    elif cmd == "build":
        from .build import build
        build(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
