from app.services.dedup import (
    compute_simhash,
    dedupe_batch,
    hamming,
    to_signed,
    to_unsigned,
)


def test_signed_unsigned_roundtrip():
    for v in [0, 1, (1 << 62), (1 << 63), (1 << 64) - 1]:
        assert to_unsigned(to_signed(v)) == v


def test_hamming_identity():
    h = compute_simhash("Senior Python Engineer", "Acme", "We need a python dev to build backends")
    assert hamming(to_signed(h), to_signed(h)) == 0


def test_near_duplicates_are_close():
    a = compute_simhash(
        "Senior Python Engineer",
        "Acme Corp",
        "We are hiring a senior python engineer to build async backend services with postgres and redis.",
    )
    b = compute_simhash(
        "Senior Python Engineer",
        "Acme Corp",
        "We're hiring a senior python engineer to build backend services with postgres and redis, async.",
    )
    # slight wording differences should land within the default threshold.
    assert hamming(to_signed(a), to_signed(b)) <= 8


def test_unrelated_jobs_are_far():
    a = compute_simhash("Frontend React Developer", "Foo Inc", "React TypeScript UI accessibility")
    b = compute_simhash("Site Reliability Engineer", "Bar LLC", "Kubernetes terraform on-call incident response")
    assert hamming(to_signed(a), to_signed(b)) > 15


def test_dedupe_batch_collapses_near_duplicates():
    items = [
        ("Senior Python Engineer", "Acme", "build async backends with postgres and redis"),
        ("Senior Python Engineer", "Acme", "build async backends with postgres and redis!"),
        ("Frontend React Developer", "Foo", "react typescript ui work"),
    ]
    kept = dedupe_batch(items, threshold=3)
    assert 0 in kept
    assert 2 in kept
    assert 1 not in kept
