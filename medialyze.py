#!/usr/bin/env python3
"""
Medialyze — Plex/arr media management toolkit

Commands:
  upgrade   Trigger Radarr/Sonarr searches for files below a quality threshold
  analyze   Rank Plex media by size × staleness to surface removal candidates

Run `medialyze.py <command> --help` for per-command options.

Docker examples:
    docker run --rm ghcr.io/compactly8274/upgrademedia analyze \\
        --plex-url http://192.168.1.x:32400 --plex-token YOUR_TOKEN

    docker run --rm -v $(pwd):/data ghcr.io/compactly8274/upgrademedia upgrade \\
        --csv /data/export.csv --radarr-url http://192.168.1.x:7878 --radarr-api-key KEY
"""

import sys


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd = sys.argv.pop(1)

    if cmd == "upgrade":
        import medialyze_upgrade
        return medialyze_upgrade.main()
    if cmd == "analyze":
        import medialyze_analyze
        return medialyze_analyze.main()

    print(f"Unknown command: {cmd!r}\nChoose from: upgrade, analyze", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
