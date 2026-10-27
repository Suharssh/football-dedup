# football-dedup

A small content-addressable backup store that deduplicates soccer footage, built to compare **fixed-size chunking** with **content-defined chunking (CDC)**.

The test case: full matches plus highlight clips cut from them with `ffmpeg -c copy`. The clips contain the same compressed video bytes as the match, but at different offsets inside a new file. Fixed-size chunking can't line those up. CDC can.

## Setup

```bash
pip install numpy
brew install ffmpeg        # or apt install ffmpeg
```

Python 3.9+. No other dependencies.

## Quick start

```bash
# 1. Footage. Real matches go in matches/. No footage yet? Make fake ones
#    (use a different --seed for each, or they come out identical):
python make_clips.py --synthetic matches/fake_a.mp4 --minutes 4 --seed 1
python make_clips.py --synthetic matches/fake_b.mp4 --minutes 3 --seed 2

# 2. Cut highlight clips by stream copy, plus one re-encoded control clip
python make_clips.py matches/*.mp4 --out clips --per-match 8 --duration 30 --control

# 3. Compare fixed vs CDC (matches first, then clips)
python dedup.py bench matches/*.mp4 clips/* --sweep 4096,16384,32768 --csv results.csv

# 4. The boundary-shift demo on a single file
python dedup.py shiftdemo matches/fake_a.mp4
```

`bench` writes one full vault per chunker setting into a temp folder and deletes it afterwards, so you need free disk space of roughly (input size × number of settings). Use `--workdir DIR` to keep the vaults.

## Tests

```bash
python -m unittest -v
```

11 tests, under a second, no footage or ffmpeg needed (they use random bytes). They check that:
- the vectorized Gear hash equals the byte-by-byte loop exactly
- chunks reassemble to the original, stay within min/max size, and don't depend on read block size
- CDC re-syncs after a 1-byte insertion (>99% chunks shared) and fixed-size doesn't (<1%)
- a "clip" (a slice of a file inside new header/footer bytes) dedups >90% with CDC and <5% with fixed
- vaults restore byte-identical files, reopen correctly, reject a mismatched chunker, and detect a corrupted byte

## Using a vault directly

```bash
python dedup.py ingest  --vault myvault --chunker cdc:8192 matches/*.mp4 clips/*
python dedup.py stats   --vault myvault
python dedup.py restore --vault myvault match_a_clip02.mp4 restored.mp4
python dedup.py verify  --vault myvault     # rebuilds every file and checks SHA-256
```

A vault's chunker is fixed when it's created; later `ingest` calls can omit `--chunker`.

## Results on synthetic test footage

Two synthetic matches (4 min and 3 min, 720p H.264), 12 stream-copied 20-second clips, and 1 re-encoded control clip. 631.9 MB in total.

| | fixed 8 KB | CDC 8 KB avg |
|---|---|---|
| Stored | 631.9 MB | 399.1 MB |
| Space saved | 0.0% | 36.8% |
| Clips already in vault | 0.0% each | 99.6–99.9% each |
| Control clip already in vault | 0.0% | 0.0% |
| 1-byte-shifted copy already in vault | 0.0% | ~100% |
| Ingest speed | 127 MB/s | 53 MB/s |
| Restore verified | 15/15 | 15/15 |

Chunk-size sweep (CDC): 4 KB, 8 KB, 16 KB and 32 KB averages all saved about 36.8%, while the index shrank from 20.4 MB to 2.5 MB. On this data, bigger chunks are nearly free. **Run it on real broadcast footage before quoting any of these numbers**; real audio/video interleaving may break shared runs into shorter pieces, and that's where small chunks would start to matter.

## How it works

**Ingest.** Each file is split into chunks. Each chunk is SHA-256 hashed. If the hash is already in the index, its reference count goes up and nothing is written. Otherwise the chunk is appended to the current pack file. The file's manifest (ordered list of chunk hashes) and whole-file SHA-256 are recorded. Each file is one SQLite transaction, and pack bytes are flushed before the commit.

**Restore.** Read the manifest, fetch each chunk from its pack at its offset, concatenate, and check the whole-file SHA-256.

**Fixed-size chunking.** Cut every N bytes. One inserted byte shifts every later cut point, so nothing matches after it.

**Content-defined chunking.** A Gear rolling hash (`h = (h << 1) + GEAR[byte]`) runs over the data. A position is a candidate cut point when the hash's top bits are all zero. The hash depends only on the last 64 bytes, so identical content produces identical cut points wherever it sits in a file. After an insertion, chunking re-syncs within about one chunk.

- **Normalized chunking (FastCDC).** A stricter mask before the target size and a looser one after it, so chunk sizes cluster near the average. Min = avg/4, max = avg×8.
- **Vectorized hashing.** A per-byte Python loop would run at ~5 MB/s. `h[i] = Σ GEAR[data[i-k]] << k` can be computed with 6 numpy "doubling" passes (`S₂ₘ[i] = Sₘ[i] + (Sₘ[i-m] << m)`), processed in 64 KB slices to stay in CPU cache. Tests confirm it matches the byte-by-byte loop exactly, and that cut points don't depend on the read block size.

**Storage layout.** Chunks live in append-only 64 MB pack files instead of one file per chunk (a 1 GB file at 8 KB chunks would be 125,000 tiny files). The SQLite index maps `hash → (pack, offset, size, refcount)`.

## Known limits / next steps

- **Garbage collection.** Reference counts are tracked, but there's no `delete` or pack compaction yet. Deleting a file should decrement refcounts; compaction rewrites packs to drop chunks at zero.
- **Crash safety.** A crash mid-ingest can leave orphan bytes at the end of a pack (they're never referenced, so restores stay correct). A GC pass would reclaim them.
- **Compression.** Not applied. Video won't compress further, but tracking data (CSV/JSON) would; add zstd after dedup.
- **Speed.** Hashing is single-threaded. Chunking and hashing several files in parallel is the obvious next step.
- **Read-side integrity.** Restore checks the whole-file hash. Checking each chunk's hash on read would pinpoint corruption to a specific chunk.
