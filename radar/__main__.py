"""Command line entry point.

    python -m radar fetch    fetch live data, save a snapshot, build the site
    python -m radar build    rebuild the site from the latest snapshot (no network)
"""
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "fetch":
        from .collect import run
        from .build import build
        build(run())
    elif cmd == "build":
        from .build import build, latest_raw
        build(latest_raw())
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
