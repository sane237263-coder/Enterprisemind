"""Agent planning and input contracts without external services."""

import asyncio
import sys

import pytest
import uvicorn
from langchain_core.messages import HumanMessage
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from enterprise_mind.agent.config import AgentSettings, load_agent
from enterprise_mind.agent.events import EventBus
from enterprise_mind.agent.runner import (
    ground_retrieval_scope,
    missing_joint_tools,
    requested_comparison_documents,
    requested_document_summary,
    requested_joint_sources,
)
from enterprise_mind.agent.schemas import MessageCreate, Plan, ProposedTool, RetrievalInput
from enterprise_mind.agent.state import merge_ids
from enterprise_mind.rag.config import RAGSettings, load_rag
from enterprise_mind.rag.repository import Scope


def test_api_loop_factory_builds_an_event_loop_without_changing_worker_policy():
    worker_loop = asyncio.new_event_loop()
    before = type(worker_loop)
    worker_loop.close()
    factory = uvicorn.Config(
        "enterprise_mind.app:create_app",
        loop="enterprise_mind.agent.checkpoints:agent_event_loop",
    ).get_loop_factory()
    loop = factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
        worker_loop = asyncio.new_event_loop()
        try:
            assert type(worker_loop) is before
        finally:
            worker_loop.close()
    finally:
        loop.close()


def test_document_names_are_bounded_exact_scope_not_identifiers():
    body = RetrievalInput(question="样本数", document_names=[" Report.MD ", "report.md"])
    assert body.scope().document_names == ("report.md",)
    assert body.scope().intersect(Scope(document_names=())).document_names == ()
    assert body.scope().intersect(Scope(document_names=("other.md",))).document_names == ()
    for names in (["   "], ["x" * 256], ["a\x00.txt"]):
        with pytest.raises(ValidationError):
            RetrievalInput(question="内容", document_names=names)
    with pytest.raises(ValidationError):
        RetrievalInput(question="内容", document_ids=["report.md"])


def test_explicit_joint_source_request_requires_both_evidence_kinds():
    assert requested_joint_sources("检索文档说明，再查询数据库，并分别给出来源")
    assert requested_joint_sources("结合知识库政策与业务表查询结果")
    assert not requested_joint_sources("仅查询数据库中的订单数量")
    assert not requested_joint_sources("把文档第二章翻译成英文")
    assert not requested_joint_sources(
        "根据 local-persistence-check.txt，EnterpriseMind 使用的 PostgreSQL 端口是多少？"
        "请引用文档来源。"
    )
    assert not requested_joint_sources("根据文档介绍数据库连接配置，并引用原文。")
    assert requested_joint_sources("同时检索文档与业务库记录，分别给出来源")
    available = {"knowledge_retrieve", "database_query"}
    joint_question = "结合知识库政策与数据库订单统计回答"
    knowledge_only = Plan(
        intent_kind="knowledge",
        requires_enterprise_facts=True,
        tools=[ProposedTool(key="read", tool_name="knowledge_retrieve", arguments={})],
    )
    assert missing_joint_tools(joint_question, knowledge_only, available) == {"database_query"}
    assert missing_joint_tools("只检索知识库政策", knowledge_only, available) == set()
    both = Plan(
        intent_kind="knowledge",
        requires_enterprise_facts=True,
        tools=[
            *knowledge_only.tools,
            ProposedTool(key="query", tool_name="database_query", arguments={}),
        ],
    )
    assert missing_joint_tools(joint_question, both, available) == set()


def test_explicit_comparison_preserves_both_exact_document_names():
    question = "对比文档 Report-2025.MD 和 report-2026.md 的差异"
    assert requested_comparison_documents(question) == {"report-2025.md", "report-2026.md"}
    assert requested_comparison_documents("总结 Report-2025.MD") == set()
    assert requested_comparison_documents("对比 差旅2025.md 和 差旅2026.md") == set()


def test_document_summary_intent_requires_a_document_reference():
    assert requested_document_summary("总结文档 report-2025.md 和 report-2026.md")
    assert requested_document_summary("概括 report-2025.md 的要求")
    assert not requested_document_summary("总结本轮聊天")


def test_retrieval_scope_discards_unmentioned_model_guesses_but_keeps_explicit_empty():
    guessed = RetrievalInput(
        question="差旅政策",
        document_names=["travel-2025.md"],
        document_ids=["2025"],
        extensions=["pdf"],
    )
    generic = [HumanMessage("归纳 2025 和 2026 年差旅政策")]
    grounded = ground_retrieval_scope(guessed, generic)
    assert grounded.scope().document_names is None
    assert grounded.scope().document_ids is None
    assert grounded.scope().extensions is None

    explicit = [HumanMessage("比较文档 travel-2025.md 和文档 ID 42 的规定")]
    named = RetrievalInput(question="规定", document_names=["travel-2025.md"], document_ids=["42"])
    assert ground_retrieval_scope(named, explicit).scope() == named.scope()
    adjacent = RetrievalInput(question="差旅", document_names=["travel-2025.md"])
    assert ground_retrieval_scope(
        adjacent, [HumanMessage("对比文档travel-2025.md和其他制度")]
    ).scope().document_names == ("travel-2025.md",)
    chinese = RetrievalInput(question="差旅", document_names=["差旅2025.md"])
    assert ground_retrieval_scope(
        chinese, [HumanMessage("总结文档《差旅2025.md》")]
    ).scope().document_names == ("差旅2025.md",)
    suffix_guess = RetrievalInput(question="差旅", document_names=["2025.md"])
    assert (
        ground_retrieval_scope(suffix_guess, [HumanMessage("总结文档《差旅2025.md》")])
        .scope()
        .document_names
        is None
    )
    empty = RetrievalInput(question="资料", document_names=[], document_ids=[])
    assert ground_retrieval_scope(empty, generic).scope().document_names == ()
    assert ground_retrieval_scope(empty, generic).scope().document_ids == ()


async def test_redis_outage_allows_sse_database_snapshot_polling():
    class Subscription:
        async def subscribe(self, channel):
            raise RedisConnectionError("private connection details")

        async def aclose(self):
            pass

    class BrokenRedis:
        def pubsub(self):
            return Subscription()

    bus = EventBus(BrokenRedis())
    async with bus.subscribe(1) as read:
        assert await read(0.001) is None


def test_agent_models_remain_unconfigured_and_limits_are_independent():
    _, providers = load_rag(RAGSettings())
    assert providers.llm is providers.embedding is providers.rerank is None
    config = load_agent(AgentSettings())
    assert (config.max_tool_calls, config.max_replans, config.tool_max_retries) == (12, 2, 3)


def test_dag_waves_and_duplicate_result_reducer():
    plan = Plan(
        intent_kind="knowledge",
        requires_enterprise_facts=True,
        tools=[
            ProposedTool(key="a", tool_name="knowledge_retrieve", arguments={"question": "a"}),
            ProposedTool(key="b", tool_name="knowledge_retrieve", arguments={"question": "b"}),
            ProposedTool(
                key="c",
                tool_name="knowledge_retrieve",
                arguments={"question": "c"},
                depends_on=["a", "b"],
            ),
        ],
    )
    assert plan.waves() == {"a": 0, "b": 0, "c": 1}
    assert merge_ids([3, 1], [2, 3]) == [1, 2, 3]
    with pytest.raises(ValueError):
        merge_ids([True], [])
    with pytest.raises(ValidationError):
        Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            tools=[
                ProposedTool(
                    key="a", tool_name="knowledge_retrieve", arguments={}, depends_on=["a"]
                ),
            ],
        )


def test_tool_schema_cannot_override_actor_permission_or_execution_limits():
    with pytest.raises(ValidationError):
        RetrievalInput(question="内容", actor_user_id="2")
    with pytest.raises(ValidationError):
        RetrievalInput(question="内容", document_ids=[2])
    assert RetrievalInput(question="内容", document_ids=[]).scope().document_ids == ()
    with pytest.raises(ValidationError):
        MessageCreate(content_text="Bearer SECRET_TOKEN_VALUE")


def test_retrieval_filter_supports_all_uploaded_office_and_image_formats():
    from enterprise_mind.ingestion.uploads import MIMES

    formats = list(MIMES)
    assert set(RetrievalInput(question="查找资料", extensions=formats).scope().extensions) == set(
        formats
    )
