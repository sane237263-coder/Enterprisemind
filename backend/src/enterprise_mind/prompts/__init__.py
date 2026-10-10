"""Versioned, packaged prompts; names are selected by application code only."""

from functools import lru_cache
from importlib.resources import files

from enterprise_mind.infrastructure.settings import ConfigurationError

_NAMES = frozenset(
    {
        "agent/plan",
        "agent/plan_json",
        "agent/answer",
        "agent/translate",
        "agent/translate_summary",
        "agent/translation_terms",
        "agent/database",
        "agent/database_select",
        "agent/database_generate",
        "rag/citations",
        "rag/no_sources",
        "conversation/summary",
        "memory_extract",
        "vision/describe",
    }
)


@lru_cache(maxsize=len(_NAMES))
def load_prompt(name: str) -> str:
    if name not in _NAMES:
        raise ConfigurationError("未知的内置 Prompt。")
    try:
        value = files(__package__).joinpath(name + ".txt").read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError("empty prompt")
        return value
    except (OSError, ValueError):
        raise ConfigurationError("内置 Prompt 缺失或无效。") from None
