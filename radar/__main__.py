"""Command line entry point.

    python -m radar fetch    fetch live data, save a snapshot, build the site
    python -m radar build    rebuild the site from the latest snapshot (no network)
    python -m radar heat     market-wide industry heat map (all SEC filers grouped by SIC), then rebuild
    python -m radar research extract filing evidence (10-K, 8-K, XBRL R&D, Yahoo profile) for
                             research-priority pools, leaders and companies with research files
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
    elif cmd == "heat":
        from .heat import collect
        from .build import build, latest_raw
        collect()
        build(latest_raw())
    elif cmd == "research":
        from .research import run
        from .build import build, latest_raw
        run()
        build(latest_raw())
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
