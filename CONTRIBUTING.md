# Contributing to Talash

Thanks for your interest in contributing! Talash is an open-source job
aggregation engine and we welcome contributions of all kinds.

## Getting started

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (dependency management)
- PostgreSQL 14+ and Redis 7+ (for full stack — tests run on SQLite)

### Setup

```bash
git clone https://github.com/iamrahulroyy/talash.git
cd talash
uv sync
cp .env.example .env
```

### Running locally

```bash
uv run task api       # FastAPI dev server
uv run task worker    # Celery worker (separate terminal)
uv run task beat      # Celery beat scheduler (separate terminal)
```

### Running tests

```bash
uv run task test      # pytest -q (runs on SQLite, no Postgres needed)
```

### Linting & formatting

```bash
uv run task lint      # ruff check
uv run task fmt       # ruff format
```

## Adding a new crawler

This is the most impactful contribution you can make! See the
[Adding a new source](https://github.com/iamrahulroyy/talash#adding-a-new-source)
section in the README.

In short:

1. Create `app/crawlers/<name>.py` subclassing `BaseCrawler`.
2. Implement `async def fetch(self) -> AsyncIterator[NormalizedJob]`.
3. Register the instance in `app/crawlers/__init__.py`.
4. Write tests — monkeypatch `_client()` so tests never hit the network.

## Submitting a pull request

1. Fork the repo and create a feature branch from `main`.
2. Make your changes with clear, descriptive commits.
3. Run `uv run task lint` and `uv run task test` — both must pass.
4. Open a PR against `main` with a clear description of what and why.

## Reporting bugs

Use the [bug report template](https://github.com/iamrahulroyy/talash/issues/new?template=bug_report.md)
and include:

- Steps to reproduce
- Expected vs actual behavior
- Python version, OS, and Docker version (if applicable)

## Code style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.
- Line length: 100 characters.
- Type hints are encouraged but not yet enforced via mypy.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
