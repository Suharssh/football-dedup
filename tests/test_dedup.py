"""
Tests for dedup.py. No footage or ffmpeg needed - they use random bytes.

    python -m unittest -v
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dedup  # noqa: E402

RNG = np.random.default_rng(7)


def random_bytes(n):
    return RNG.integers(0, 256, n, dtype=np.uint8).tobytes()


def chunk_list(chunker, data):
    return list(chunker.chunks(io.BytesIO(data)))


class TestGearHash(unittest.TestCase):
    def test_matches_byte_by_byte_loop(self):
        """The vectorized hash must equal the classic h = (h << 1) + GEAR[b]."""
        data = np.frombuffer(random_bytes(20_000), dtype=np.uint8)
        gear = [int(x) for x in dedup.GEAR]
        h, ref = 0, []
        for b in data.tolist():
            h = ((h << 1) + gear[b]) & (2**64 - 1)
            ref.append(h)
        ref = np.array(ref, dtype=np.uint64)
        old = dedup.HASH_SLICE
        try:
            for size in (64 << 10, 1000, 97):        # slice size must not change results
                dedup.HASH_SLICE = size
                self.assertTrue(np.array_equal(dedup.gear_hashes(data), ref), f"slice {size}")
        finally:
            dedup.HASH_SLICE = old


class TestCDCChunker(unittest.TestCase):
    def setUp(self):
        self.data = random_bytes(3_000_000)
        self.c = dedup.CDCChunker(8192)

    def test_reassembles_exactly(self):
        self.assertEqual(b"".join(chunk_list(self.c, self.data)), self.data)

    def test_chunk_sizes_within_bounds(self):
        sizes = [len(x) for x in chunk_list(self.c, self.data)]
        self.assertTrue(all(self.c.min <= s <= self.c.max for s in sizes[:-1]))
        self.assertTrue(4096 < np.mean(sizes) < 16384, f"avg {np.mean(sizes):.0f}")

    def test_independent_of_read_block_size(self):
        old = dedup.READ_BLOCK
        try:
            results = []
            for block in (1 << 20, 100_000, 70_001):
                dedup.READ_BLOCK = block
                results.append([len(x) for x in chunk_list(self.c, self.data)])
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0], results[2])
        finally:
            dedup.READ_BLOCK = old

    def test_resyncs_after_insertion(self):
        """Inserting 1 byte at the start should change only the first chunk or two."""
        original = set(chunk_list(self.c, self.data))
        shifted = chunk_list(self.c, b"X" + self.data)
        shared = sum(ch in original for ch in shifted) / len(shifted)
        self.assertGreater(shared, 0.99)

    def test_fixed_size_does_not_resync(self):
        """The contrast case: fixed-size chunks share ~nothing after a 1-byte shift."""
        f = dedup.FixedChunker(8192)
        original = set(chunk_list(f, self.data))
        shifted = chunk_list(f, b"X" + self.data)
        self.assertLess(sum(ch in original for ch in shifted) / len(shifted), 0.01)

    def test_tiny_and_empty_inputs(self):
        self.assertEqual(chunk_list(self.c, b""), [])
        self.assertEqual(chunk_list(self.c, b"abc"), [b"abc"])


class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.big = self.dir / "match.bin"
        self.big.write_bytes(random_bytes(2_000_000))
        payload = self.big.read_bytes()
        # a "clip": a slice of the match wrapped in new header/footer bytes,
        # like a stream-copied clip inside a new container
        self.clip = self.dir / "clip.bin"
        self.clip.write_bytes(b"HEADER" * 50 + payload[700_000:1_300_000] + b"FOOTER")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_dedup(self):
        for spec, expect_dedup in (("cdc:8192", True), ("fixed:8192", False)):
            with self.subTest(spec=spec):
                v = dedup.Vault(self.dir / f"vault-{spec.replace(':', '-')}", spec)
                v.ingest(self.big)
                r = v.ingest(self.clip)
                already = 1 - r["new"] / r["size"]
                if expect_dedup:
                    self.assertGreater(already, 0.9)
                else:
                    self.assertLess(already, 0.05)
                out = self.dir / "restored.bin"
                self.assertTrue(v.restore("clip.bin", out))
                self.assertEqual(out.read_bytes(), self.clip.read_bytes())
                n, bad = v.verify()
                self.assertEqual((n, bad), (2, []))
                v.close()

    def test_reopen_and_skip_duplicate_name(self):
        path = self.dir / "vault"
        v = dedup.Vault(path, "cdc:8192")
        v.ingest(self.big)
        v.close()
        v = dedup.Vault(path)                         # chunker read from the vault
        self.assertIsNone(v.ingest(self.big))         # same name: skipped
        v.ingest(self.clip)
        self.assertEqual(v.verify(), (2, []))
        v.close()

    def test_rejects_different_chunker(self):
        path = self.dir / "vault"
        dedup.Vault(path, "cdc:8192").close()
        with self.assertRaises(SystemExit):
            dedup.Vault(path, "fixed:8192")

    def test_detects_corruption(self):
        v = dedup.Vault(self.dir / "vault", "cdc:8192")
        v.ingest(self.big)
        v.close()
        pack = next((self.dir / "vault" / "packs").iterdir())
        with open(pack, "r+b") as fh:                 # flip one byte
            fh.seek(12345)
            b = fh.read(1)
            fh.seek(12345)
            fh.write(bytes([b[0] ^ 0xFF]))
        v = dedup.Vault(self.dir / "vault")
        self.assertFalse(v.restore("match.bin"))
        v.close()


if __name__ == "__main__":
    unittest.main()
