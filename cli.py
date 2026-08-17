#!/usr/bin/env python3
"""Command-line companion to the web app.

The web app handles Spotify sign-in; this covers the cases that need no auth:
dumping The List, and comparing it against artist names you already have on
disk (a plain text file, or a Spotify data export).

    python cli.py shows --days 14
    python cli.py compare --artists my_bands.txt
    python cli.py compare --history ~/my_spotify_data/Streaming_History_*.json

Add --json to any command for machine-readable output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List

from app.matching import Artist, find_matches
from app.spotify import parse_streaming_history
from app.thelist import BASE_URL, fetch_list


def _load_artists(args: argparse.Namespace) -> List[Artist]:
    artists: List[Artist] = []

    if args.artists:
        path = Path(args.artists)
        if not path.exists():
            sys.exit(f"No such file: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name and not name.startswith("#"):
                artists.append(Artist(name=name, sources={"file"}))

    if args.history:
        blobs = []
        for pattern in args.history:
            path = Path(pattern)
            if not path.exists():
                sys.exit(f"No such file: {path}")
            blobs.append(path.read_bytes())
        artists.extend(parse_streaming_history(blobs))

    return artists


def _within(iso: str, days: int) -> bool:
    if not days:
        return True
    today = date.today()
    try:
        when = date.fromisoformat(iso)
    except ValueError:
        return False
    return today <= when <= today + timedelta(days=days)


async def cmd_shows(args: argparse.Namespace) -> int:
    snapshot = await fetch_list(args.url)
    shows = [s for s in snapshot.shows if _within(s.date, args.days)]

    if args.json:
        print(json.dumps([s.as_dict() for s in shows], indent=2))
        return 0

    print(f"The List ({snapshot.updated_label}) — {len(shows)} shows")
    current = ""
    for show in shows:
        if show.date != current:
            current = show.date
            print(f"\n{show.weekday} {show.date}")
        extras = " ".join(x for x in (show.age, show.price, show.doors) if x)
        flag = " [SOLD OUT]" if show.sold_out else ""
        print(f"  {show.band} — {show.venue}  {extras}{flag}")
    return 0


async def cmd_compare(args: argparse.Namespace) -> int:
    artists = _load_artists(args)
    if not artists:
        sys.exit("Give me artists with --artists and/or --history. See --help.")

    snapshot = await fetch_list(args.url)
    shows = [s for s in snapshot.shows if _within(s.date, args.days)]
    matches = find_matches(shows, artists)

    if args.json:
        print(json.dumps([m.as_dict() for m in matches], indent=2))
        return 0

    print(
        f"{len(artists)} artists vs {len(shows)} shows "
        f"({snapshot.updated_label}) — {len(matches)} matches\n"
    )
    if not matches:
        print("No overlap. Try a wider --days window.")
        return 0

    current = ""
    for match in matches:
        if match.date != current:
            current = match.date
            print(f"\n{match.weekday} {match.date}")
        extras = " ".join(x for x in (match.age, match.price, match.doors) if x)
        flag = " [SOLD OUT]" if match.sold_out else ""
        print(f"  {match.band} — {match.venue}  {extras}{flag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=BASE_URL, help="The List base URL")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    shows = sub.add_parser("shows", help="dump upcoming shows")
    shows.add_argument("--days", type=int, default=30, help="0 for everything")
    shows.set_defaults(func=cmd_shows)

    compare = sub.add_parser("compare", help="compare artists against The List")
    compare.add_argument("--artists", help="text file, one artist name per line")
    compare.add_argument(
        "--history", nargs="+", help="Spotify data-export JSON file(s)"
    )
    compare.add_argument("--days", type=int, default=60, help="0 for everything")
    compare.set_defaults(func=cmd_compare)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
