#!/usr/bin/env python3
"""
dedup.py - a small content-addressable backup store with two chunkers:

  fixed : cut every N bytes (the naive approach)
  cdc   : content-defined chunking with a Gear rolling hash and FastCDC-style
          normalized chunking (cut points depend on the bytes, not offsets)

Chunks are SHA-256 addressed, stored once in append-only pack files, and
tracked in a SQLite index with reference counts. Each ingested file gets a
manifest (ordered list of chunk hashes) so it can be rebuilt exactly.

Commands
  ingest     add files to a vault
  restore    rebuild one file and check its SHA-256
  verify     rebuild every file in a vault and check it
  stats      storage numbers for a vault
  bench      ingest the same files with fixed and CDC chunking, compare
  shiftdemo  the boundary-shift problem: one file vs. a 1-byte-shifted copy

Only dependency: numpy.
"""
import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

READ_BLOCK = 1 << 20      # bytes read (and hashed) per step
WINDOW = 64               # Gear hash window: a 64-bit hash "remembers" 64 bytes
PACK_LIMIT = 64 << 20     # start a new pack file after this many bytes

# 256 fixed pseudo-random 64-bit values, one per byte value. Fixed seed and
# explicit little-endian so every machine computes identical cut points.
GEAR = np.frombuffer(np.random.default_rng(0x50CCE7).bytes(256 * 8), dtype="<u8").astype(np.uint64)


# --------------------------------------------------------------------------- #
# Chunkers
# --------------------------------------------------------------------------- #

HASH_SLICE = 64 << 10     # hash in cache-sized slices (much faster than one big array)


def _gear_slice(data: np.ndarray) -> np.ndarray:
    """Gear rolling hash after every byte of `data` (uint8 array).

    The classic loop is   h = (h << 1) + GEAR[byte]   (mod 2^64),
    which equals  h[i] = sum_{k<64} GEAR[data[i-k]] << k.

    A Python loop over every byte would run at ~5 MB/s, so this computes the
    same values with 6 vectorized "doubling" passes:
        S_1 = GEAR[data],   S_2m[i] = S_m[i] + (S_m[i-m] << m)
    after which S_64 is the hash over a full 64-byte window.
    """
    h = GEAR[data]
    tmp = np.empty_like(h)
    n, m = len(h), 1
    while m < WINDOW and m < n:          # m >= n would make n - m negative
        np.left_shift(h[:n - m], np.uint64(m), out=tmp[:n - m])
        np.add(h[m:], tmp[:n - m], out=h[m:])     # uint64 wraps mod 2^64
        m *= 2
    return h


def gear_hashes(data: np.ndarray) -> np.ndarray:
    """Same result as _gear_slice(data), computed slice by slice. Each slice
    re-reads the 63 bytes before it, so every hash still sees its full window."""
    out = np.empty(len(data), np.uint64)
    for s in range(0, len(data), HASH_SLICE):
        lo = max(0, s - (WINDOW - 1))
        out[s:s + HASH_SLICE] = _gear_slice(data[lo:s + HASH_SLICE])[s - lo:]
    return out


def top_bits_mask(n: int) -> np.uint64:
    """Mask on the top n bits; the top bits depend on all 64 window bytes."""
    return np.uint64(((1 << n) - 1) << (64 - n))


class FixedChunker:
    def __init__(self, size: int):
        self.size = size
        self.spec = f"fixed:{size}"

    def chunks(self, f):
        while True:
            block = f.read(self.size)
            if not block:
                return
            yield block


class CDCChunker:
    """Content-defined chunking, FastCDC style.

    A position is a candidate cut point when the rolling hash's top bits are
    all zero. Because the hash only looks at the last 64 bytes, the same
    content produces the same cut points wherever it sits in a file. That is
    what lets a highlight clip line up with the full match it came from.

    Normalized chunking: before the target size we use a stricter mask (fewer
    cut points), after it a looser one, so chunk sizes cluster around `avg`.
    """

    def __init__(self, avg: int = 8192):
        if avg & (avg - 1) or avg < 1024:
            raise SystemExit("--avg must be a power of two >= 1024")
        bits = avg.bit_length() - 1
        self.min, self.avg, self.max = avg // 4, avg, avg * 8
        self.strict = top_bits_mask(bits + 2)
        self.loose = top_bits_mask(bits - 2)
        self.spec = f"cdc:{avg}"

    def _pick(self, start, end, strict, loose):
        """Choose where the chunk that begins at `start` ends."""
        if end - start <= self.min:
            return end
        lo = start + self.min
        mid = min(start + self.avg, end)
        hi = min(start + self.max, end)
        i = np.searchsorted(strict, lo)
        if i < len(strict) and strict[i] < mid:
            return int(strict[i])
        j = np.searchsorted(loose, mid)
        if j < len(loose) and loose[j] < hi:
            return int(loose[j])
        return hi

    def chunks(self, f):
        ctx = b""                    # last 63 bytes, so hashing continues across reads
        buf = bytearray()            # bytes read but not yet emitted
        used = 0                     # how much of buf has been emitted
        start = 0                    # file offset of buf[used]
        read_pos = 0                 # file offset of the next byte to read
        strict = np.empty(0, np.int64)   # candidate cut positions (file offsets)
        loose = np.empty(0, np.int64)
        eof = False
        while not eof:
            block = f.read(READ_BLOCK)
            if block:
                joined = ctx + block
                h = gear_hashes(np.frombuffer(joined, dtype=np.uint8))[len(ctx):]
                # a hash match after byte i means "cut before byte i+1".
                # Every strict match is also a loose match, so filter in two steps.
                lo_idx = np.flatnonzero((h & self.loose) == 0)
                st_idx = lo_idx[(h[lo_idx] & self.strict) == 0]
                base = read_pos + 1
                loose = np.concatenate([loose, lo_idx + base])
                strict = np.concatenate([strict, st_idx + base])
                buf += block
                read_pos += len(block)
                ctx = joined[-(WINDOW - 1):]
            else:
                eof = True
            # emit chunks once we can see a full max-size window (or at EOF)
            while len(buf) - used > 0 and (eof or len(buf) - used >= self.max):
                cut = self._pick(start, read_pos, strict, loose)
                n = cut - start
                yield bytes(buf[used:used + n])
                used += n
                start = cut
            del buf[:used]
            used = 0
            strict = strict[np.searchsorted(strict, start, side="right"):]
            loose = loose[np.searchsorted(loose, start, side="right"):]


def make_chunker(spec: str):
    kind, size = spec.split(":")
    return CDCChunker(int(size)) if kind == "cdc" else FixedChunker(int(size))


# --------------------------------------------------------------------------- #
# Vault: pack files + SQLite index
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chunks(
    hash BLOB PRIMARY KEY, pack INTEGER, offset INTEGER, size INTEGER, refs INTEGER);
CREATE TABLE IF NOT EXISTS files(
    name TEXT PRIMARY KEY, size INTEGER, sha256 TEXT,
    new_bytes INTEGER, chunks INTEGER, seconds REAL);
CREATE TABLE IF NOT EXISTS file_chunks(
    name TEXT, seq INTEGER, hash BLOB, PRIMARY KEY(name, seq));
"""


class Vault:
    def __init__(self, path, chunker_spec=None):
        self.root = Path(path)
        (self.root / "packs").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.sqlite")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='chunker'").fetchone()
        if row is None:
            if chunker_spec is None:
                raise SystemExit(f"{path} is a new vault: pass --chunker")
            with self.db:
                self.db.execute("INSERT INTO meta VALUES('chunker', ?)", (chunker_spec,))
        elif chunker_spec and chunker_spec != row[0]:
            raise SystemExit(f"{path} was created with {row[0]}, not {chunker_spec}")
        self.chunker = make_chunker(chunker_spec or row[0])
        last = self.db.execute("SELECT MAX(pack) FROM chunks").fetchone()[0]
        self.pack_id = last or 1
        self._writer = None
        self._readers = {}

    # -- packs --
    def _pack_path(self, pack_id):
        return self.root / "packs" / f"pack-{pack_id:05d}.bin"

    def _append(self, chunk):
        if self._writer is None:
            self._writer = open(self._pack_path(self.pack_id), "ab")
        if self._writer.tell() >= PACK_LIMIT:
            self._writer.close()
            self.pack_id += 1
            self._writer = open(self._pack_path(self.pack_id), "ab")
        offset = self._writer.tell()
        self._writer.write(chunk)
        return self.pack_id, offset

    def _read(self, pack_id, offset, size):
        fh = self._readers.get(pack_id)
        if fh is None:
            fh = self._readers[pack_id] = open(self._pack_path(pack_id), "rb")
        fh.seek(offset)
        return fh.read(size)

    def close(self):
        if self._writer:
            self._writer.close()
        for fh in self._readers.values():
            fh.close()
        self.db.close()

    # -- ingest / restore --
    def ingest(self, path, name=None):
        name = name or Path(path).name
        if self.db.execute("SELECT 1 FROM files WHERE name=?", (name,)).fetchone():
            print(f"  skip {name}: already in vault")
            return None
        whole = hashlib.sha256()
        size = new = seq = 0
        t0 = time.perf_counter()
        with open(path, "rb") as f, self.db:          # one transaction per file
            for seq, chunk in enumerate(self.chunker.chunks(f), 1):
                whole.update(chunk)
                size += len(chunk)
                h = hashlib.sha256(chunk).digest()
                hit = self.db.execute("UPDATE chunks SET refs = refs + 1 WHERE hash=?", (h,))
                if hit.rowcount == 0:                 # never seen: store it
                    pack, off = self._append(chunk)
                    self.db.execute("INSERT INTO chunks VALUES(?,?,?,?,1)",
                                    (h, pack, off, len(chunk)))
                    new += len(chunk)
                self.db.execute("INSERT INTO file_chunks VALUES(?,?,?)", (name, seq, h))
            if self._writer:
                self._writer.flush()                  # chunk bytes on disk before commit
            secs = time.perf_counter() - t0
            self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?)",
                            (name, size, whole.hexdigest(), new, seq, secs))
        return {"name": name, "size": size, "new": new, "chunks": seq, "seconds": secs}

    def restore(self, name, out_path=None):
        """Rebuild `name` from its chunks. Returns True if the SHA-256 matches."""
        row = self.db.execute("SELECT sha256 FROM files WHERE name=?", (name,)).fetchone()
        if row is None:
            raise SystemExit(f"no file named {name!r} in vault")
        if self._writer:
            self._writer.flush()
        whole = hashlib.sha256()
        out = open(out_path, "wb") if out_path else None
        try:
            for pack, off, size in self.db.execute(
                    "SELECT c.pack, c.offset, c.size FROM file_chunks fc "
                    "JOIN chunks c ON c.hash = fc.hash WHERE fc.name=? ORDER BY fc.seq",
                    (name,)):
                data = self._read(pack, off, size)
                whole.update(data)
                if out:
                    out.write(data)
        finally:
            if out:
                out.close()
        return whole.hexdigest() == row[0]

    def verify(self):
        names = [r[0] for r in self.db.execute("SELECT name FROM files ORDER BY name")]
        bad = [n for n in names if not self.restore(n)]
        return len(names), bad

    def stats(self):
        logical, nfiles = self.db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM files").fetchone()
        physical, nchunks = self.db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM chunks").fetchone()
        secs = self.db.execute("SELECT COALESCE(SUM(seconds),0) FROM files").fetchone()[0]
        return {
            "chunker": self.chunker.spec,
            "files": nfiles,
            "logical": logical,
            "physical": physical,
            "saved_pct": 100 * (1 - physical / logical) if logical else 0.0,
            "ratio": logical / physical if physical else 0.0,
            "chunks": nchunks,
            "avg_chunk": physical / nchunks if nchunks else 0,
            "index_bytes": os.path.getsize(self.root / "index.sqlite"),
            "mb_per_s": logical / secs / 1e6 if secs else 0.0,
        }


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #

def require_unique_names(files):
    """Files are stored by base name, so two inputs called 1_720p.mkv in
    different folders would collide. Stop instead of silently skipping one."""
    seen = {}
    for p in files:
        name = Path(p).name
        if name in seen:
            raise SystemExit(f"two inputs share the name {name!r}:\n  {seen[name]}\n  {p}\n"
                             "rename one (download_soccernet.py does this for you)")
        seen[name] = p


def mb(n):
    return f"{n / 1e6:,.1f} MB"


def pct(part, whole):
    return f"{100 * part / whole:5.1f}%" if whole else "   - "


def run_into_vault(vault_dir, spec, files, quiet=False):
    v = Vault(vault_dir, spec)
    per_file = {}
    for p in files:
        r = v.ingest(p)
        if r:
            per_file[r["name"]] = r
            if not quiet:
                print(f"  [{spec:>10}] {r['name']:<32} {mb(r['size']):>12}  "
                      f"new {pct(r['new'], r['size'])}")
    n, bad = v.verify()
    st = v.stats()
    st["verified"] = f"{n - len(bad)}/{n}"
    v.close()
    return per_file, st


def print_totals(rows):
    """rows: list of (label, stats)"""
    print()
    print(f"{'':<22}" + "".join(f"{label:>18}" for label, _ in rows))
    lines = [
        ("Logical (input)", lambda s: mb(s["logical"])),
        ("Physical (stored)", lambda s: mb(s["physical"])),
        ("Space saved", lambda s: f"{s['saved_pct']:.1f}%"),
        ("Dedup ratio", lambda s: f"{s['ratio']:.2f}x"),
        ("Unique chunks", lambda s: f"{s['chunks']:,}"),
        ("Avg chunk size", lambda s: f"{s['avg_chunk'] / 1024:.1f} KB"),
        ("Index size", lambda s: mb(s["index_bytes"])),
        ("Ingest speed", lambda s: f"{s['mb_per_s']:.0f} MB/s"),
        ("Restore verified", lambda s: s["verified"]),
    ]
    for label, fmt in lines:
        print(f"{label:<22}" + "".join(f"{fmt(s):>18}" for _, s in rows))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_ingest(a):
    require_unique_names(a.files)
    v = Vault(a.vault, a.chunker)
    for p in a.files:
        r = v.ingest(p)
        if r:
            print(f"{r['name']:<32} {mb(r['size']):>12}  new {pct(r['new'], r['size'])}  "
                  f"{r['chunks']:,} chunks")
    v.close()


def cmd_restore(a):
    v = Vault(a.vault)
    ok = v.restore(a.name, a.out)
    v.close()
    print(f"{a.name} -> {a.out}: {'SHA-256 OK' if ok else 'SHA-256 MISMATCH'}")
    sys.exit(0 if ok else 1)


def cmd_verify(a):
    v = Vault(a.vault)
    n, bad = v.verify()
    v.close()
    print(f"{n - len(bad)}/{n} files restored with matching SHA-256")
    for b in bad:
        print(f"  FAILED: {b}")
    sys.exit(1 if bad else 0)


def cmd_stats(a):
    v = Vault(a.vault)
    s = v.stats()
    v.close()
    s["verified"] = "-"
    print_totals([(s["chunker"], s)])


def cmd_bench(a):
    require_unique_names(a.files)
    work = Path(a.workdir or tempfile.mkdtemp(prefix="dedup-bench-"))
    specs = [f"fixed:{a.fixed_size}", f"cdc:{a.avg}"]
    sweep = [int(x) for x in a.sweep.split(",")] if a.sweep else []
    results = {}
    try:
        for spec in specs:
            print(f"\nIngesting with {spec} ...")
            results[spec] = run_into_vault(work / spec.replace(":", "-"), spec, a.files)

        names = list(results[specs[0]][0])
        print("\nPer file: share of each file that was ALREADY in the vault (higher = more dedup)")
        print(f"{'file':<34}{'size':>12}{'fixed':>10}{'cdc':>10}")
        for n in names:
            f, c = results[specs[0]][0][n], results[specs[1]][0][n]
            print(f"{n:<34}{mb(f['size']):>12}"
                  f"{pct(f['size'] - f['new'], f['size']):>10}{pct(c['size'] - c['new'], c['size']):>10}")

        rows = [(s, results[s][1]) for s in specs]
        for avg in sweep:
            spec = f"cdc:{avg}"
            if spec in results:
                continue
            print(f"\nSweep: ingesting with {spec} ...")
            results[spec] = run_into_vault(work / spec.replace(":", "-"), spec, a.files, quiet=True)
            rows.append((spec, results[spec][1]))
        print_totals(rows)

        if a.csv:
            with open(a.csv, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["chunker", "file", "size_bytes", "new_bytes", "already_stored_pct"])
                for spec, (per_file, _) in results.items():
                    for n, r in per_file.items():
                        w.writerow([spec, n, r["size"], r["new"],
                                    round(100 * (r["size"] - r["new"]) / r["size"], 2) if r["size"] else 0])
            print(f"\nPer-file results written to {a.csv}")
    finally:
        if a.workdir:
            print(f"\nVaults kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def cmd_shiftdemo(a):
    work = Path(tempfile.mkdtemp(prefix="dedup-shift-"))
    try:
        src = Path(a.file)
        shifted = work / f"{src.stem}.shifted{src.suffix}"
        with open(src, "rb") as fi, open(shifted, "wb") as fo:
            fo.write(b"\x00")                       # one extra byte at the very start
            shutil.copyfileobj(fi, fo, 8 << 20)
        print(f"Original: {src.name}   Copy: same bytes with 1 byte inserted at the start\n")
        print(f"{'chunker':<14}{'shifted copy already stored':>30}")
        for spec in (f"fixed:{a.fixed_size}", f"cdc:{a.avg}"):
            per_file, _ = run_into_vault(work / spec.replace(":", "-"), spec, [src, shifted], quiet=True)
            r = per_file[shifted.name]
            print(f"{spec:<14}{pct(r['size'] - r['new'], r['size']):>30}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="add files to a vault")
    p.add_argument("--vault", required=True)
    p.add_argument("--chunker", help="cdc:<avg> or fixed:<size>, e.g. cdc:8192 (needed for a new vault)")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("restore", help="rebuild one file and check its SHA-256")
    p.add_argument("--vault", required=True)
    p.add_argument("name")
    p.add_argument("out")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("verify", help="rebuild every file in a vault and check it")
    p.add_argument("--vault", required=True)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("stats", help="storage numbers for a vault")
    p.add_argument("--vault", required=True)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("bench", help="compare fixed-size and CDC chunking on the same files")
    p.add_argument("files", nargs="+", help="put full matches first, then clips")
    p.add_argument("--avg", type=int, default=8192, help="CDC average chunk size (power of two)")
    p.add_argument("--fixed-size", type=int, default=8192, help="fixed chunk size")
    p.add_argument("--sweep", help="extra CDC average sizes to try, e.g. 4096,16384,32768")
    p.add_argument("--csv", help="write per-file results to this CSV")
    p.add_argument("--workdir", help="keep the vaults here instead of a temp dir")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("shiftdemo", help="1-byte shift: fixed vs CDC")
    p.add_argument("file")
    p.add_argument("--avg", type=int, default=8192)
    p.add_argument("--fixed-size", type=int, default=8192)
    p.set_defaults(fn=cmd_shiftdemo)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
