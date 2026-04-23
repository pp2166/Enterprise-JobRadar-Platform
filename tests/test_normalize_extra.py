"""Extra normalize-layer coverage.

Covers edge cases that the core test_normalize doesn't touch: malformed HTML,
bare tags, unicode, weird salary formats, timezone-aware inputs, and the
NormalizedJob dataclass defaults.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.normalize import (
    NormalizedJob,
    ensure_utc,
    infer_experience,
    infer_remote,
    parse_salary,
    squish,
    strip_html,
)


class TestStripHtml:
    def test_none_returns_empty_string(self):
        assert strip_html(None) == ""

    def test_empty_returns_empty(self):
        assert strip_html("") == ""

    def test_plain_text_passes_through(self):
        assert strip_html("just plain text") == "just plain text"

    def test_nested_tags_flattened(self):
        assert strip_html("<div><p>a</p><p>b</p></div>") == "a b"

    def test_self_closing_tags_removed(self):
        out = strip_html("line one<br/>line two<hr/>line three")
        assert "<br" not in out and "<hr" not in out
        assert "line one" in out and "line three" in out

    def test_tags_dropped_but_text_kept(self):
        # selectolax keeps script bodies as text; we only promise tag removal.
        out = strip_html("<p>hello</p><script>alert(1)</script><p>world</p>")
        assert "<" not in out and ">" not in out
        assert "hello" in out and "world" in out

    def test_entities_decoded(self):
        assert "AT&T" in strip_html("<p>AT&amp;T</p>")

    def test_unicode_preserved(self):
        assert strip_html("<p>café — 日本語</p>") == "café — 日本語"

    def test_malformed_html_does_not_raise(self):
        # Unclosed/broken tags must not crash the pipeline.
        out = strip_html("<p>hello <b>world")
        assert "hello" in out and "world" in out


class TestSquish:
    def test_empty_and_none(self):
        assert squish("") == ""
        assert squish(None) == ""

    def test_mixed_whitespace_collapsed(self):
        assert squish(" a\t\tb\n\nc\r d ") == "a b c d"

    def test_already_clean_unchanged(self):
        assert squish("clean text") == "clean text"


class TestParseSalary:
    def test_k_with_dash(self):
        assert parse_salary("$100k-$120k") == (100000, 120000, "USD")

    def test_k_with_word_to(self):
        assert parse_salary("$90k to $110k") == (90000, 110000, "USD")

    def test_k_with_en_dash(self):
        low, high, cur = parse_salary("$120k – $150k")
        assert (low, high, cur) == (120000, 150000, "USD")

    def test_uppercase_k(self):
        low, high, cur = parse_salary("$100K - $130K")
        assert (low, high, cur) == (100000, 130000, "USD")

    def test_swaps_when_high_comes_first(self):
        low, high, _ = parse_salary("$150k - $100k")
        assert low == 100000 and high == 150000

    def test_no_dollar_no_currency(self):
        low, high, cur = parse_salary("80,000 to 100,000")
        assert low == 80000 and high == 100000
        assert cur is None

    def test_commas_stripped(self):
        low, high, _ = parse_salary("120,000 - 150,000 USD")
        assert low == 120000 and high == 150000

    def test_nothing_matches(self):
        assert parse_salary("competitive salary") == (None, None, None)

    def test_empty_and_none(self):
        assert parse_salary("") == (None, None, None)
        assert parse_salary(None) == (None, None, None)


class TestInferExperience:
    def test_senior_in_title(self):
        assert infer_experience("Senior Platform Engineer", "") == "senior"

    def test_staff_is_senior(self):
        assert infer_experience("Staff Engineer", "") == "senior"

    def test_principal_is_senior(self):
        assert infer_experience("Principal Scientist", "") == "senior"

    def test_lead_is_senior(self):
        assert infer_experience("Lead Designer", "") == "senior"

    def test_intern_is_junior(self):
        assert infer_experience("Software Intern", "") == "junior"

    def test_graduate_is_junior(self):
        assert infer_experience("Graduate Developer", "") == "junior"

    def test_mid_in_description(self):
        assert infer_experience("Developer", "We need mid-level experience") == "mid"

    def test_case_insensitive(self):
        assert infer_experience("SENIOR ENGINEER", "") == "senior"

    def test_senior_beats_junior_when_both_present(self):
        # Implementation checks senior tokens first.
        assert infer_experience("Senior Engineer (no junior devs)", "") == "senior"

    def test_no_match_returns_none(self):
        assert infer_experience("Developer", "work with cool people") is None


class TestInferRemote:
    def test_remote_in_location(self):
        assert infer_remote("Remote - EU", "Engineer", []) is True

    def test_anywhere_in_title(self):
        assert infer_remote(None, "Developer (anywhere)", []) is True

    def test_distributed_in_tags(self):
        assert infer_remote(None, "Engineer", ["distributed"]) is True

    def test_none_when_onsite(self):
        assert infer_remote("San Francisco", "Engineer", ["python"]) is None

    def test_handles_null_inputs(self):
        assert infer_remote(None, "", []) is None


class TestEnsureUtc:
    def test_naive_gets_utc(self):
        dt = ensure_utc(datetime(2026, 1, 1, 12))
        assert dt.tzinfo == timezone.utc

    def test_aware_is_converted(self):
        from datetime import timedelta
        tz = timezone(timedelta(hours=5))
        dt = ensure_utc(datetime(2026, 1, 1, 12, tzinfo=tz))
        # 12:00 +05:00 == 07:00 UTC
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 7

    def test_none_stays_none(self):
        assert ensure_utc(None) is None


class TestNormalizedJobDefaults:
    def test_required_fields_only(self):
        j = NormalizedJob(
            source="x", source_id="1", url="u", title="t",
            company="c", description="d",
        )
        assert j.location is None
        assert j.remote is None
        assert j.salary_min is None
        assert j.tags == []
        assert j.posted_at is None

    def test_tags_are_independent_per_instance(self):
        # Default factory prevents shared-mutable-default bugs.
        a = NormalizedJob(source="x", source_id="1", url="u", title="t", company="c", description="d")
        b = NormalizedJob(source="x", source_id="2", url="u", title="t", company="c", description="d")
        a.tags.append("python")
        assert b.tags == []
