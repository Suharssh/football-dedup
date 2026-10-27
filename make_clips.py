#!/usr/bin/env python3
"""
make_clips.py - cut highlight clips from full matches with ffmpeg stream copy.

Stream copy (-c copy) keeps the compressed video/audio packets byte-for-byte,
so each clip shares real bytes with its match - just at different offsets
inside a new container. That is the case CDC should handle and fixed-size
chunking should not.

  # 8 random 30 s clips per match, plus one re-encoded control clip
  python make_clips.py matches/*.mp4 --out clips --per-match 8 --duration 30 --control

  # no footage yet? make fake "matches" to smoke-test the pipeline
  # (use a different --seed for each, or they'll be identical files)
  python make_clips.py --synthetic matches/fake_a.mp4 --minutes 4 --seed 1
  python make_clips.py --synthetic matches/fake_b.mp4 --minutes 3 --seed 2

The control clip is re-encoded, so it shares no bytes with anything. It should
deduplicate at ~0% under both chunkers. If it doesn't, something is wrong.

Requires ffmpeg and ffprobe on PATH.
"""
import argparse
import random
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())


def cut_copy(src, start, dur, out):
    # -ss before -i: fast seek; with -c copy the clip starts at the keyframe
    # at or before `start`, so clips run a little longer than asked.
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{dur:.2f}",
         "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
         "-avoid_negative_ts", "make_zero", str(out)])


def cut_reencode(src, start, dur, out):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{dur:.2f}",
         "-map", "0:v:0", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", str(out)])


def make_synthetic(out, minutes, seed):
    """A noisy test pattern + tone. Noise keeps the video incompressible-ish,
    like real footage, so dedup numbers aren't flattered by repetition.
    Different seeds give different noise, so two synthetic matches share no
    bytes (same seed = byte-identical videos)."""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    secs = int(minutes * 60)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
         "-f", "lavfi", "-i", f"sine=frequency={330 + 10 * (seed % 20)}:sample_rate=48000",
         "-t", str(secs), "-vf", f"noise=alls=8:allf=t:all_seed={seed}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "50",
         "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)])
    print(f"wrote {out} ({Path(out).stat().st_size / 1e6:.1f} MB, {secs}s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("matches", nargs="*", help="full-match video files")
    ap.add_argument("--out", default="clips", help="folder for the clips")
    ap.add_argument("--per-match", type=int, default=8)
    ap.add_argument("--duration", type=float, default=30.0, help="clip length in seconds")
    ap.add_argument("--seed", type=int, default=1,
                    help="same seed = same clips (and, with --synthetic, the same fake video)")
    ap.add_argument("--control", action="store_true", help="also make one re-encoded clip")
    ap.add_argument("--synthetic", metavar="OUT", help="generate a fake match video and exit")
    ap.add_argument("--minutes", type=float, default=5.0, help="length of the synthetic match")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} not found on PATH (macOS: brew install ffmpeg)")

    if a.synthetic:
        make_synthetic(a.synthetic, a.minutes, a.seed)
        return
    if not a.matches:
        ap.error("give at least one match file (or use --synthetic)")

    stems = [Path(m).stem for m in a.matches]
    dupes = sorted({s for s in stems if stems.count(s) > 1})
    if dupes:
        sys.exit(f"several matches are named {dupes}, so their clips would overwrite each other. "
                 "Rename them first (download_soccernet.py gives SoccerNet files unique names).")

    rng = random.Random(a.seed)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for m in map(Path, a.matches):
        total = duration(m)
        latest = total - a.duration - 5
        if latest <= 5:
            print(f"skip {m.name}: shorter than a clip")
            continue
        for i in range(1, a.per_match + 1):
            start = rng.uniform(5, latest)
            out = out_dir / f"{m.stem}_clip{i:02d}{m.suffix}"
            cut_copy(m, start, a.duration, out)
            made.append(out)
            print(f"{out.name:<40} from {start:7.1f}s  ({out.stat().st_size / 1e6:6.1f} MB)  stream copy")

    if a.control and a.matches:
        m = Path(a.matches[0])
        start = rng.uniform(5, max(6, duration(m) - a.duration - 5))
        out = out_dir / f"{m.stem}_CONTROL_reencoded.mp4"
        cut_reencode(m, start, a.duration, out)
        made.append(out)
        print(f"{out.name:<40} from {start:7.1f}s  ({out.stat().st_size / 1e6:6.1f} MB)  re-encoded")

    print(f"\n{len(made)} clips in {out_dir}/")
    print(f"next: python dedup.py bench {' '.join(a.matches)} {out_dir}/*")


if __name__ == "__main__":
    main()
