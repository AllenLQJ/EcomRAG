import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    llm_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="LLM_API_KEY")
    llm_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="LLM_BASE_URL")
    # 单独读取过渡别名，保留来源信息；避免 AliasChoices 在配置源中规范化后丢失旧名。
    # 兼容字段不出现在 repr / model_dump 中，也不会被业务代码使用。
    legacy_llm_api_key: SecretStr | None = Field(
        default=None, validation_alias="QWEN_API_KEY", exclude=True, repr=False)
    legacy_llm_base_url: str | None = Field(
        default=None, validation_alias="QWEN_BASE_URL", exclude=True, repr=False)
    router_model: str = "qwen-turbo"
    generate_model: str = "qwen-plus"
    router_max_tokens: int = Field(default=64, gt=0)
    generate_max_tokens: int = Field(default=600, gt=0)
    # 禁止环境源提前 JSON 解码，统一在 validator 中报清晰错误，避免重复解码。
    llm_extra_body: Annotated[dict[str, object], NoDecode] = Field(default_factory=dict, repr=False)
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "ecomrag"
    mysql_user: str = "ecomrag"
    mysql_password: SecretStr = SecretStr("")
    mysql_root_password: SecretStr = SecretStr("")
    redis_url: str = "redis://127.0.0.1:6379/0"
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_timeout_s: float = Field(default=10, gt=0)
    bge_m3_path: str = "models/bge-m3"
    reranker_path: str = "models/bge-reranker-v2-m3"
    embed_batch_size: int = Field(default=16, gt=0)
    child_chunk_size: int = Field(default=300, gt=0)
    child_chunk_overlap: int = Field(default=50, ge=0)
    parent_max_chars: int = Field(default=1200, gt=0)
    faq_threshold: float = Field(default=0.9, ge=0, le=1)
    faq_cache_ttl_s: int = Field(default=86400, gt=0)
    rerank_enabled: bool = True
    rerank_candidates: int = Field(default=10, gt=0)
    rerank_max_length: int = Field(default=512, gt=0)
    rerank_batch_size: int = Field(default=16, gt=0)
    rerank_max_chars: int = Field(default=600, ge=0)
    rerank_text_mode: Literal["child", "parent"] = "child"
    rerank_fp16: bool = False
    rerank_top_k: int = Field(default=5, gt=0)
    speculative_retrieval: bool = True
    refuse_threshold: float = Field(default=0.5, ge=0, le=1)
    llm_timeout_s: float = Field(default=20, gt=0)
    llm_max_concurrency: int = Field(default=4, gt=0)
    llm_max_retries: int = Field(default=1, ge=0)
    llm_retry_backoff_s: float = Field(default=0.5, ge=0)
    warmup_on_start: bool = True
    allow_trace: bool = False
    trust_proxy_headers: bool = False
    request_timeout_s: float = Field(default=40, gt=0)
    model_io_workers: int = Field(default=1, gt=0)
    model_io_max_in_flight: int = 1
    rate_limit_io_workers: int = Field(default=2, gt=0)
    rate_limit_io_max_in_flight: int = Field(default=16, gt=0)
    rate_limit_per_min: int = Field(default=10, gt=0)
    rate_limit_per_day: int = Field(default=200, gt=0)
    health_timeout_s: float = Field(default=1, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: str = "logs"
    log_backup_days: int = Field(default=14, ge=0)
    log_to_console: bool = True
    chat_log_enabled: bool = True
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    debug: bool = False

    @model_validator(mode="after")
    def validate_model_io_capacity(self):
        if not 1 <= self.model_io_max_in_flight <= self.model_io_workers:
            raise ValueError("MODEL_IO_MAX_IN_FLIGHT 必须至少为 1，且不得大于 MODEL_IO_WORKERS")
        return self

    @model_validator(mode="after")
    def validate_chunk_overlap(self):
        if self.child_chunk_overlap >= self.child_chunk_size - 1:
            raise ValueError("CHILD_CHUNK_OVERLAP 必须小于 CHILD_CHUNK_SIZE - 1，需为标题前缀预留空间")
        return self

    @model_validator(mode="after")
    def apply_legacy_llm_names(self):
        legacy_used = False
        conflict = False
        for current, legacy in (
            ("llm_api_key", "legacy_llm_api_key"),
            ("llm_base_url", "legacy_llm_base_url"),
        ):
            value, old = getattr(self, current), getattr(self, legacy)
            plain = value.get_secret_value() if hasattr(value, "get_secret_value") else value
            previous = old.get_secret_value() if hasattr(old, "get_secret_value") else old
            supplied = current in self.model_fields_set and bool(plain.strip())
            if not supplied and previous and previous.strip():
                setattr(self, current, old)
                legacy_used = True
            elif supplied and previous and previous.strip() and plain != previous:
                conflict = True
            elif current == "llm_base_url" and not plain.strip():
                self.llm_base_url = type(self).model_fields[current].default
        if legacy_used:
            logger.warning("QWEN_* 配置名已弃用，请改用 LLM_*")
        if conflict:
            logger.warning("LLM_* 与 QWEN_* 配置冲突，以 LLM_* 为准")
        return self

    @field_validator("llm_extra_body", mode="before")
    @classmethod
    def parse_llm_extra_body(cls, value):
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                def invalid_constant(value):
                    raise ValueError
                value = json.loads(value, parse_constant=invalid_constant)
            except ValueError:
                raise ValueError("LLM_EXTRA_BODY 必须为合法 JSON 对象；空字符串表示不添加参数") from None
        if not isinstance(value, dict):
            raise ValueError("LLM_EXTRA_BODY 必须为 JSON 对象，不能是数组、字符串或 null")
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("LLM_EXTRA_BODY 必须为可序列化的合法 JSON 对象") from None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
