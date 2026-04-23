from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://jobhunt:jobhunt@localhost:5432/jobhunt"
    sync_database_url: str = "postgresql+psycopg2://jobhunt:jobhunt@localhost:5432/jobhunt"
    redis_url: str = "redis://localhost:6379/0"

    crawl_user_agent: str = "jobhunt-crawler/0.1"
    crawl_concurrency: int = 8
    crawl_timeout: int = 20

    # SimHash Hamming distance threshold; lower = stricter dedup.
    # 3 is a common starting point for 64-bit SimHash over short text.
    simhash_threshold: int = 3

    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
