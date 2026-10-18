from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    qwen_api_key: SecretStr = SecretStr("")
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    router_model: str = "qwen-turbo"
    generate_model: str = "qwen-plus"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "ecomrag"
    mysql_user: str = "ecomrag"
    mysql_password: SecretStr = SecretStr("")
    mysql_root_password: SecretStr = SecretStr("")
    redis_url: str = "redis://127.0.0.1:6379/0"
    milvus_uri: str = "http://127.0.0.1:19530"
    bge_m3_path: str = "models/bge-m3"
    reranker_path: str = "models/bge-reranker-v2-m3"
    embed_batch_size: int = Field(default=16, gt=0)
    child_chunk_size: int = Field(default=300, gt=0)
    child_chunk_overlap: int = Field(default=50, ge=0)
    parent_max_chars: int = Field(default=1200, gt=0)
    faq_threshold: float = Field(default=0.8, ge=0, le=1)
    rerank_enabled: bool = True
    rerank_candidates: int = Field(default=20, gt=0)
    rerank_top_k: int = Field(default=5, gt=0)
    refuse_threshold: float = Field(default=0.3, ge=0, le=1)
    llm_timeout_s: float = Field(default=20, gt=0)
    request_timeout_s: float = Field(default=40, gt=0)
    rate_limit_per_min: int = Field(default=10, gt=0)
    rate_limit_per_day: int = Field(default=200, gt=0)
    health_timeout_s: float = Field(default=1, gt=0)
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
