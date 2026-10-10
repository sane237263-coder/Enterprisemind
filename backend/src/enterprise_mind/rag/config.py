from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from enterprise_mind.infrastructure.settings import ConfigurationError, StrictConfig


class ChunkConfig(StrictConfig):
    target_tokens: int = Field(default=400, ge=1)
    max_tokens: int = Field(default=600, ge=2, le=32000)
    min_tokens: int = Field(default=80, ge=0)
    overlap_tokens: int = Field(default=80, ge=0)

    @model_validator(mode="after")
    def lengths(self) -> "ChunkConfig":
        if not (
            self.min_tokens <= self.target_tokens <= self.max_tokens
            and self.overlap_tokens < self.max_tokens
        ):
            raise ValueError("invalid chunk limits")
        return self


class KeywordConfig(StrictConfig):
    content_limit: int = Field(default=50, ge=1, le=200)
    metadata_document_limit: int = Field(default=10, ge=1, le=50)
    metadata_chunks_per_document: int = Field(default=2, ge=1, le=10)
    trigram_threshold: float = Field(default=0.30, gt=0, le=1)


class VectorConfig(StrictConfig):
    candidate_limit: int = Field(default=50, ge=1, le=200)
    exact_scan_max_chunks: int = Field(default=5000, ge=1)
    distance: Literal["cosine"] = "cosine"


class RRFConfig(StrictConfig):
    k: int = Field(default=60, ge=1)
    keyword_weight: float = Field(default=1.0, gt=0)
    vector_weight: float = Field(default=1.0, gt=0)
    metadata_weight: float = Field(default=0.5, gt=0)
    fused_limit: int = Field(default=30, ge=1, le=200)


class RerankConfig(StrictConfig):
    candidate_limit: int = Field(default=30, ge=1, le=200)
    model_key: str | None = None
    min_score: float | None = Field(default=None, allow_inf_nan=False)


class ContextConfig(StrictConfig):
    max_blocks: int = Field(default=10, ge=1, le=10)
    max_token_ratio: float = Field(default=0.35, gt=0, le=1)
    absolute_max_tokens: int = Field(default=6000, ge=1, le=32000)
    neighbor_each_side: int = Field(default=1, ge=0, le=1)


class DedupConfig(StrictConfig):
    anchor_overlap_ratio: float = Field(default=0.80, gt=0, le=1)


class FallbackConfig(StrictConfig):
    consensus_rank: int = Field(default=20, ge=1, le=200)


class HNSWConfig(StrictConfig):
    m: int = Field(default=16, ge=2, le=100)
    ef_construction: int = Field(default=64, ge=4, le=1000)
    ef_search: int = Field(default=100, ge=1, le=1000)
    iterative_scan: Literal["strict_order"] = "strict_order"
    max_scan_tuples: int = Field(default=20000, ge=1, le=1000000)
    scan_mem_multiplier: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def construction(self) -> "HNSWConfig":
        if self.ef_construction < self.m * 2:
            raise ValueError("ef_construction must be at least twice m")
        return self


class RAGConfig(StrictConfig):
    chunk: ChunkConfig = Field(default_factory=ChunkConfig)
    keyword: KeywordConfig = Field(default_factory=KeywordConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    rrf: RRFConfig = Field(default_factory=RRFConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    hnsw: HNSWConfig = Field(default_factory=HNSWConfig)

    @model_validator(mode="after")
    def limits(self) -> "RAGConfig":
        if self.rerank.candidate_limit > self.rrf.fused_limit:
            raise ValueError("rerank exceeds fused candidate limit")
        if (self.rerank.model_key is None) != (self.rerank.min_score is None):
            raise ValueError("threshold must be paired with the calibrated model key")
        return self


class ModelEndpointConfig(StrictConfig):
    provider_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    model_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,200}$")
    timeout_seconds: int = Field(ge=1, le=120)
    max_retries: int = Field(default=3, ge=0, le=3)
    max_input_tokens: int = Field(ge=2, le=1000000)


class EndpointConfig(ModelEndpointConfig):
    tokenizer_file: str
    tokenizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EmbeddingConfig(EndpointConfig):
    protocol: Literal["openai_compatible"] = "openai_compatible"
    dimensions: int = Field(ge=1, le=16000)
    send_dimensions: bool = False
    max_batch_size: int = Field(default=32, ge=1, le=256)
    secret_file_env: Literal["EM_EMBEDDING_API_KEY_FILE"] = "EM_EMBEDDING_API_KEY_FILE"


class RerankProviderConfig(EndpointConfig):
    protocol: Literal["jina_compatible"] = "jina_compatible"
    score_direction: Literal["higher"] = "higher"
    score_min: float = Field(allow_inf_nan=False)
    score_max: float = Field(allow_inf_nan=False)
    secret_file_env: Literal["EM_RERANK_API_KEY_FILE"] = "EM_RERANK_API_KEY_FILE"

    @model_validator(mode="after")
    def scores(self) -> "RerankProviderConfig":
        if self.score_min >= self.score_max:
            raise ValueError("invalid score range")
        return self


class ThinkingConfig(StrictConfig):
    type: Literal["enabled", "disabled"]


class ChatRequestOptions(StrictConfig):
    # Only reviewed protocol extensions may be forwarded to the provider.
    thinking: ThinkingConfig | None = None
    enable_thinking: bool | None = None

    @model_validator(mode="after")
    def one_thinking_protocol(self) -> "ChatRequestOptions":
        if self.thinking is not None and self.enable_thinking is not None:
            raise ValueError("select only one thinking protocol")
        return self


class LLMConfig(ModelEndpointConfig):
    protocol: Literal["openai_compatible"] = "openai_compatible"
    max_output_tokens: int = Field(ge=128, le=32000)
    plan_timeout_seconds: int = Field(default=45, ge=1, le=120)
    summary_timeout_seconds: int = Field(default=10, ge=1, le=30)
    answer_idle_timeout_seconds: int = Field(default=45, ge=1, le=120)
    secret_file_env: Literal["EM_LLM_API_KEY_FILE"] = "EM_LLM_API_KEY_FILE"
    request_options: ChatRequestOptions = Field(default_factory=ChatRequestOptions)
    structured_output_method: Literal["function_calling", "json_mode"] = "function_calling"
    token_counter: Literal["local_tokenizer", "utf8_bytes"] = "local_tokenizer"
    tokenizer_file: str | None = None
    tokenizer_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def output_window(self) -> "LLMConfig":
        if self.max_output_tokens + 2048 >= self.max_input_tokens:
            raise ValueError("context window needs space for output, instructions, and user input")
        if self.token_counter == "local_tokenizer":
            if not self.tokenizer_file or not self.tokenizer_sha256:
                raise ValueError("local tokenizer requires a file and digest")
        elif self.tokenizer_file is not None or self.tokenizer_sha256 is not None:
            raise ValueError("byte budget must not retain a different model's tokenizer")
        return self


class ProvidersConfig(StrictConfig):
    allowed_origins: list[str] = Field(default_factory=list)
    llm: LLMConfig | None = None
    embedding: EmbeddingConfig | None = None
    rerank: RerankProviderConfig | None = None
    ocr: "OCRProviderConfig | TencentOCRConfig | None" = None
    vision: "VisionProviderConfig | None" = None

    @model_validator(mode="after")
    def endpoints(self) -> "ProvidersConfig":
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowlist entries must be HTTPS origins")
        for provider in (self.llm, self.embedding, self.rerank, self.ocr, self.vision):
            if provider is None:
                continue
            parsed = urlsplit(provider.base_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if (
                origin not in self.allowed_origins
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("provider endpoint is not allowed")
        return self


class MediaProviderConfig(StrictConfig):
    provider_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    model_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,200}$")
    timeout_seconds: int = Field(default=120, ge=1, le=120)
    max_retries: int = Field(default=3, ge=0, le=3)


class OCRProviderConfig(MediaProviderConfig):
    protocol: Literal["enterprise_mind_ocr_v1"] = "enterprise_mind_ocr_v1"
    secret_file_env: Literal["EM_OCR_API_KEY_FILE"] = "EM_OCR_API_KEY_FILE"


class TencentOCRConfig(MediaProviderConfig):
    protocol: Literal["tencent_tc3_ocr"]
    # model names the text-recognition Action; neither Action is chosen in code.
    api_version: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    region: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,50}$")
    table_action: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9]{0,99}$")
    max_image_base64_bytes: int = Field(ge=1024, le=10 * 1024 * 1024)
    secret_id_file_env: Literal["EM_OCR_SECRET_ID_FILE"] = "EM_OCR_SECRET_ID_FILE"
    secret_key_file_env: Literal["EM_OCR_SECRET_KEY_FILE"] = "EM_OCR_SECRET_KEY_FILE"

    @model_validator(mode="after")
    def signature_endpoint(self) -> "TencentOCRConfig":
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or not self.model.isascii()
            or not self.model.isalnum()
        ):
            raise ValueError("invalid OCR signing endpoint or action")
        return self


class VisionProviderConfig(MediaProviderConfig):
    protocol: Literal["openai_compatible"] = "openai_compatible"
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    secret_file_env: Literal["EM_VISION_API_KEY_FILE"] = "EM_VISION_API_KEY_FILE"
    request_options: ChatRequestOptions = Field(default_factory=ChatRequestOptions)
    response_format: Literal["json_object", "text"] = "json_object"


ProvidersConfig.model_rebuild()


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EM_", extra="ignore", hide_input_in_errors=True)
    rag_config: Path = Path(__file__).resolve().parents[4] / "config/rag.yaml"
    providers_config: Path = Path(__file__).resolve().parents[4] / "config/providers.yaml"


def load_rag(settings: RAGSettings) -> tuple[RAGConfig, ProvidersConfig]:
    try:
        raw = yaml.safe_load(settings.rag_config.read_text(encoding="utf-8"))
        config = RAGConfig.model_validate(raw["rag"])
        if set(raw) != {"rag"}:
            raise ValueError("unknown root key")
        providers = ProvidersConfig.model_validate(
            yaml.safe_load(settings.providers_config.read_text(encoding="utf-8"))
        )
        for provider in (providers.llm, providers.embedding, providers.rerank):
            if (
                provider is not None
                and provider.tokenizer_file
                and not Path(provider.tokenizer_file).is_absolute()
            ):
                provider.tokenizer_file = str(
                    (settings.providers_config.resolve().parent / provider.tokenizer_file).resolve()
                )
        if providers.rerank is not None:
            if (
                config.rerank.model_key != providers.rerank.model_key
                or config.rerank.min_score is None
                or not providers.rerank.score_min
                <= config.rerank.min_score
                <= providers.rerank.score_max
            ):
                raise ValueError("rerank calibration does not match the provider")
        elif config.rerank.model_key is not None:
            raise ValueError("calibrated rerank provider missing")
        if providers.embedding and config.chunk.max_tokens > providers.embedding.max_input_tokens:
            raise ValueError("chunk exceeds provider input limit")
        return config, providers
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ValidationError,
        ValueError,
        KeyError,
        TypeError,
    ):
        raise ConfigurationError("RAG 或 Provider 配置不完整、不匹配或超出限制。") from None
