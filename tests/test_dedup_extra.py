"""Extra dedup-layer coverage: threshold behaviour, DB lookups, edge cases."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dedup import (
    _MAX_I64,
    _MAX_U64,
    compute_simhash,
    dedupe_batch,
    fingerprint_text,
    find_duplicate,
    hamming,
    to_signed,
    to_unsigned,
)
from app.services.ingest import ingest_jobs


class TestSimhashMath:
    def test_to_signed_preserves_small_values(self):
        assert to_signed(0) == 0
        assert to_signed(1) == 1
        assert to_signed(_MAX_I64 - 1) == _MAX_I64 - 1

    def test_to_signed_wraps_high_bit(self):
        assert to_signed(_MAX_I64) == -_MAX_I64
        assert to_signed(_MAX_U64 - 1) == -1

    def test_to_unsigned_inverse(self):
        assert to_unsigned(-1) == _MAX_U64 - 1
        assert to_unsigned(0) == 0
        assert to_unsigned(42) == 42

    def test_hamming_known_values(self):
        # 0b1010 vs 0b0101 = 4 differing bits
        assert hamming(0b1010, 0b0101) == 4

    def test_hamming_zero_for_identical(self):
        assert hamming(123456789, 123456789) == 0

    def test_hamming_handles_signed_boundary(self):
        # Distance must be computed on the unsigned representation.
        a, b = to_signed(_MAX_I64), to_signed(_MAX_I64 + 1)
        assert hamming(a, b) == 1


class TestFingerprint:
    def test_is_lowercase_and_trimmed(self):
        fp = fingerprint_text("  Senior Python  ", "  Acme  ", "Build backends")
        assert fp.startswith("senior python")
        assert "acme" in fp

    def test_body_word_limit_applied(self):
        long = " ".join(["word"] * 500)
        fp = fingerprint_text("T", "C", long, body_words=10)
        # 10 words from body => count occurrences of "word"
        assert fp.count("word") == 10

    def test_non_alnum_tokens_dropped(self):
        fp = fingerprint_text("T!!!", "C???", "hello, world!!!")
        # tokenizer drops punctuation, so the fingerprint contains plain words.
        assert "hello" in fp and "world" in fp


class TestComputeSimhash:
    def test_empty_inputs_return_zero(self):
        assert compute_simhash("", "", "") == 0

    def test_determinism(self):
        a = compute_simhash("t", "c", "d word word word")
        b = compute_simhash("t", "c", "d word word word")
        assert a == b

    def test_is_unsigned_64bit(self):
        h = compute_simhash("Senior Engineer", "Acme", "build stuff")
        assert 0 <= h < _MAX_U64


class TestDedupeBatch:
    def test_empty_batch_returns_empty(self):
        assert dedupe_batch([], threshold=3) == []

    def test_single_item_always_kept(self):
        assert dedupe_batch([("t", "c", "d")], threshold=3) == [0]

    def test_distinct_all_kept(self):
        items = [
            ("Python Engineer", "A", "python backend"),
            ("Rust Engineer", "B", "systems rust"),
            ("Frontend Dev", "C", "react ui"),
        ]
        assert dedupe_batch(items, threshold=3) == [0, 1, 2]

    def test_higher_threshold_collapses_more(self):
        items = [
            ("Python Engineer", "Acme", "build async backends"),
            ("Python Developer", "Acme", "build sync backends"),
        ]
        strict = dedupe_batch(items, threshold=1)
        loose = dedupe_batch(items, threshold=32)
        assert len(loose) <= len(strict)
        assert len(loose) == 1  # everything collapses at huge threshold


@pytest.mark.asyncio
class TestFindDuplicateDB:
    async def test_returns_none_when_table_empty(self, session: AsyncSession, make_job):
        j = make_job()
        h = to_signed(compute_simhash(j.title, j.company, j.description))
        match = await find_duplicate(
            session, title=j.title, company=j.company,
            simhash_signed=h, threshold=3,
        )
        assert match is None

    async def test_different_company_not_matched(self, session: AsyncSession, make_job):
        j1 = make_job(title="Senior Rust Engineer", company="Acme",
                      description="build a rust backend with tokio")
        await ingest_jobs(session, [j1])

        probe = make_job(title="Senior Rust Engineer", company="OtherCo",
                         description="build a rust backend with tokio")
        h = to_signed(compute_simhash(probe.title, probe.company, probe.description))

        match = await find_duplicate(
            session, title=probe.title, company=probe.company,
            simhash_signed=h, threshold=3,
        )
        # Different company => prefilter excludes it entirely.
        assert match is None

    async def test_threshold_zero_requires_exact_hash_match(self, session: AsyncSession, make_job):
        j1 = make_job(title="Python Engineer", company="Acme",
                      description="totally unique description blob")
        await ingest_jobs(session, [j1])

        # A completely different description → different hash → no dup at t=0.
        probe_hash = to_signed(
            compute_simhash("Python Engineer", "Acme", "entirely unrelated content")
        )
        match = await find_duplicate(
            session, title="Python Engineer", company="Acme",
            simhash_signed=probe_hash, threshold=0,
        )
        assert match is None
