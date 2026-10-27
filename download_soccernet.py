#!/usr/bin/env python3
"""
download_soccernet.py - fetch a few full SoccerNet broadcast matches into matches/.

SoccerNet videos are hosted on Hugging Face as a gated dataset, so you need,
once, on your own machine:

  1. A free Hugging Face account
  2. Access granted to https://huggingface.co/datasets/SoccerNet/SoccerNet_raw_HQ
     (open the page while logged in and request access; this involves agreeing
     to SoccerNet's terms, so read them yourself)
  3. A login on this machine:  hf auth login   (paste a read token when asked)

Then:

  python download_soccernet.py --list                  # show available games
  python download_soccernet.py --games 2               # first 2 test games, half 1, 720p
  python download_soccernet.py --pick 3,17 --quality 224p   # specific games, smaller files
  python download_soccernet.py --games 2 --half both   # both halves

Each file is saved under a readable name (e.g.
matches/2015-05-17_Manchester_United_1-1_Arsenal_h1_720p.mkv), so clips from
different games never collide. Full halves are large: start with 2 games and
check your free disk space.
"""
import argparse
import os
import re
import shutil
import sys
from pathlib import Path

QUALITIES = {"224p": "_224p.mkv", "720p": "_720p.mkv", "HQ": "_HQ.mkv"}


def slug(game: str) -> str:
    """'england_epl/2014-2015/2015-05-17 - 18-00 Manchester United 1 - 1 Arsenal'
    -> '2015-05-17_Manchester_United_1-1_Arsenal'"""
    name = game.split("/")[-1]
    name = re.sub(r"^(\d{4}-\d{2}-\d{2}) - \d{2}-\d{2} ", r"\1 ", name)   # drop kickoff time
    name = re.sub(r"(\d+) - (\d+)", r"\1-\2", name)                       # score
    return re.sub(r"[^A-Za-z0-9-]+", "_", name).strip("_")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="test", choices=["train", "valid", "test"])
    ap.add_argument("--list", action="store_true", help="list games in the split and exit")
    ap.add_argument("--games", type=int, default=2, help="download the first N games")
    ap.add_argument("--pick", help="comma-separated game numbers from --list (overrides --games)")
    ap.add_argument("--quality", default="720p", choices=list(QUALITIES))
    ap.add_argument("--half", default="1", choices=["1", "2", "both"])
    ap.add_argument("--out", default="matches")
    ap.add_argument("--cache", default=".soccernet_cache", help="where the SoccerNet tool downloads to")
    a = ap.parse_args()

    try:
        from SoccerNet.Downloader import SoccerNetDownloader
        from SoccerNet.utils import getListGames
    except ImportError:
        sys.exit("SoccerNet package missing: pip install SoccerNet")

    games = getListGames(a.split)
    if a.list:
        for i, g in enumerate(games):
            print(f"{i:4d}  {g}")
        return

    if a.pick:
        try:
            chosen = [games[int(i)] for i in a.pick.split(",")]
        except (ValueError, IndexError):
            sys.exit(f"--pick takes numbers between 0 and {len(games) - 1}; see --list")
    else:
        chosen = games[:a.games]

    halves = ["1", "2"] if a.half == "both" else [a.half]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dl = SoccerNetDownloader(LocalDirectory=a.cache)

    for game in chosen:
        for h in halves:
            fname = h + QUALITIES[a.quality]
            target = out / f"{slug(game)}_h{h}_{a.quality}.mkv"
            if target.exists():
                print(f"have  {target.name}")
                continue
            print(f"get   {game} / {fname}")
            try:
                dl.downloadGame(game=game, files=[fname], spl=a.split)
            except PermissionError as e:
                sys.exit(
                    f"\nHugging Face refused the download:\n  {e}\n\n"
                    "Check that (1) you requested and were granted access to "
                    "https://huggingface.co/datasets/SoccerNet/SoccerNet_raw_HQ and "
                    "(2) you ran `hf auth login` on this machine with that account.")
            except Exception as e:
                sys.exit(f"\ndownload failed ({type(e).__name__}): {e}\n"
                         "Check your internet connection and try again; finished files are kept.")
            src = Path(a.cache) / game / fname
            if not src.exists():
                sys.exit(f"download finished but {src} is missing; try again or use a different game")
            shutil.move(str(src), target)
            print(f"saved {target} ({target.stat().st_size / 1e6:,.0f} MB)")

    print(f"\nnext: python make_clips.py {a.out}/*.mkv --out clips --per-match 8 --duration 30 --control")


if __name__ == "__main__":
    main()
