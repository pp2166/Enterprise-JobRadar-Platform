from datetime import datetime, timezone

from app.services.normalize import (
    ensure_utc,
    infer_experience,
    infer_remote,
    parse_salary,
    squish,
    strip_html,
)


def test_strip_html_removes_tags_and_collapses_whitespace():
    assert strip_html("<p>Hello  <b>world</b></p>") == "Hello world"


def test_squish():
    assert squish("  a   b\tc\n") == "a b c"
    assert squish(None) == ""


def test_parse_salary_with_k():
    low, high, cur = parse_salary("Salary: $120k - $150k per year")
    assert (low, high, cur) == (120000, 150000, "USD")


def test_parse_salary_without_k():
    low, high, cur = parse_salary("Range: 80,000 to 110,000 USD")
    assert low == 80000 and high == 110000


def test_parse_salary_none():
    assert parse_salary("unspecified") == (None, None, None)
    assert parse_salary(None) == (None, None, None)


def test_infer_experience():
    assert infer_experience("Senior Python Engineer", "") == "senior"
    assert infer_experience("Junior Developer", "") == "junior"
    assert infer_experience("Software Engineer", "intermediate experience preferred") == "mid"
    assert infer_experience("Software Engineer", "no level mentioned here") is None


def test_infer_remote():
    assert infer_remote("Remote - US", "Engineer", []) is True
    assert infer_remote(None, "Engineer - Anywhere", []) is True
    assert infer_remote("New York", "Engineer", []) is None


def test_ensure_utc():
    naive = datetime(2025, 1, 1, 12, 0)
    assert ensure_utc(naive).tzinfo == timezone.utc
    assert ensure_utc(None) is None
