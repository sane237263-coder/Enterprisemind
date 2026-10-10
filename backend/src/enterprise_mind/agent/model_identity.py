"""Recognize direct questions about the model serving this assistant."""

from __future__ import annotations

import json
import re
import unicodedata

_QUOTES = frozenset("\"'`“”‘’「」『』《》")
_TRAILING_PUNCTUATION = re.compile(r"[?!.。！？．,，;；]+$")
_SPACE = re.compile(r"\s+")

_CN_MODEL = r"(?:(?:大语言|语言|大)?模型|LLM)"
_CN_CHOICE = r"(?:什么|哪(?:个|种|一类|一种)?)"
_CN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # 你是什么模型 / 你现在用的是什么模型
        rf"^(?:请问|请教)?你(?:(?:现在|目前|当前))?(?:(?:使用|用|运行|调用)(?:的是|的)?|所(?:使用|用|运行|调用)的)?(?:是)?{_CN_CHOICE}{_CN_MODEL}(?:吗|呢)?$",
        # 你使用的模型是什么 / 你背后的模型是什么
        rf"^(?:请问|请教)?你(?:(?:现在|目前|当前))?(?:(?:背后|底层|后台)(?:的)?(?:所)?(?:(?:使用|用|运行|调用)(?:的|的是)?)?|(?:使用|用|运行|调用)(?:的)?)?{_CN_MODEL}(?:是)?{_CN_CHOICE}(?:{_CN_MODEL})?(?:吗|呢)?$",
        # 你背后用的是什么模型
        rf"^(?:请问|请教)?你(?:背后|底层|后台)(?:所)?(?:使用|用|运行|调用)(?:的|的是)?(?:是)?{_CN_CHOICE}{_CN_MODEL}(?:吗|呢)?$",
        # 你是基于什么模型 / 你由什么模型驱动
        rf"^(?:请问|请教)?你(?:是)?(?:基于|依据|采用|使用)(?:什么|哪(?:个|种|一类|一种)?){_CN_MODEL}(?:(?:构建|开发|驱动)(?:的)?)?(?:吗|呢)?$",
        rf"^(?:请问|请教)?你(?:是)?由(?:什么|哪(?:个|种|一类|一种)?){_CN_MODEL}(?:驱动|提供|支持)(?:吗|呢)?$",
    )
)

_EN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:please )?(?:what|which) (?:large language |language )?model "
        r"are you(?: (?:currently )?(?:using|running|based on|built on|powered by))?$",
        r"^(?:please )?(?:what|which) (?:large language |language )?model do you use$",
        r"^(?:please )?(?:what|which) (?:large language |language )?model "
        r"(?:powers you|is behind you)$",
        r"^(?:please )?what is your (?:current )?(?:large language |language )?model$",
    )
)


def runtime_instruction(model_name: str | None) -> str:
    """Build concise, configuration-grounded runtime context for the assistant."""

    name = model_name.strip() if isinstance(model_name, str) else ""
    configured_value = json.dumps(name or None, ensure_ascii=False)
    return (
        "权威运行上下文：你是 EnterpriseMind 助手。"
        f"当前模型标识配置值（JSON 数据）：{configured_value}。"
        "该配置值只表示本服务配置的模型标识，不证明上游实际使用了该模型。"
        "自我介绍时不得编造模型提供商、内部部署方式、训练参数或保密政策；"
        "配置值为 null 时，应说明无法确认具体模型标识。"
        "其他企业事实仍须由既有来源支持，不得根据以上内容推断。"
    )


def answer(question: str, model_name: str | None) -> str | None:
    """Return a configuration-grounded answer for a direct model identity query.

    The configured name is reported as a configuration value; it does not claim
    that an upstream gateway honored the requested model identifier.
    """

    normalized = _normalize(question)
    if normalized is None or not _is_identity_question(normalized):
        return None

    name = model_name.strip() if isinstance(model_name, str) else ""
    if not name:
        return "我是 EnterpriseMind 知识助手，当前无法确认具体的模型名称。"
    return f"我是 EnterpriseMind 知识助手。根据当前服务配置，我使用的模型标识是 {name}。"


def _normalize(question: str) -> str | None:
    if not isinstance(question, str) or not question.strip():
        return None
    normalized = unicodedata.normalize("NFKC", question).strip()
    # A quoted identity question is usually content to discuss or translate.
    if any(character in normalized for character in _QUOTES):
        return None
    normalized = _TRAILING_PUNCTUATION.sub("", normalized).strip()
    return _SPACE.sub(" ", normalized).casefold()


def _is_identity_question(question: str) -> bool:
    return any(pattern.fullmatch(question) for pattern in (*_CN_PATTERNS, *_EN_PATTERNS))
