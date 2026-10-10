import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from enterprise_mind.agent.knowledge_catalog import (
    CatalogInput,
    direct_request,
    read_catalog,
    render,
    tool,
)
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.security import BIGINT_MAX
from enterprise_mind.identity.service import CurrentIdentity


@pytest.mark.parametrize(
    "question",
    [
        "我当前有权限的知识库有哪些",
        "当前知识库有哪些？",
        "当前有哪些知识库",
        "列出知识库",
        "有哪些知识库。",
        "我有哪些知识库",
        "我能访问哪些知识库",
        "请问我现在有权限访问的知识库有哪些",
        "列出所有我能访问的知识库！",
    ],
)
def test_direct_request_recognizes_only_explicit_accessible_catalog_lists(question):
    assert direct_request(question) == CatalogInput()


@pytest.mark.parametrize(
    "question",
    [
        "",
        None,
        "当前知识库包含什么内容？",
        "搜索知识库里的采购制度",
        "当前知识库有多少文档？",
        "知识库里有多少份文件",
        "“列出知识库”",
        "列出知识库并删除旧库",
        "列出知识库，再搜索差旅报销制度",
        "列出财务知识库",
    ],
)
def test_direct_request_rejects_content_count_quotes_and_mixed_intents(question):
    assert direct_request(question) is None


@pytest.mark.parametrize(
    ("question", "after_id"),
    [
        ("列出 ID 14 之后的知识库", 14),
        ("列出ID 14之后的知识库。", 14),
    ],
)
def test_direct_request_recognizes_unambiguous_next_page(question, after_id):
    assert direct_request(question) == CatalogInput(after_id=after_id)


@pytest.mark.parametrize(
    "question",
    [
        "列出 ID 14 之后的知识库并搜索政策",
        "列出 ID 14 之后的文档",
        "列出 ID 9223372036854775808 之后的知识库",
        "“列出 ID 14 之后的知识库”",
    ],
)
def test_direct_request_rejects_ambiguous_or_invalid_continuations(question):
    assert direct_request(question) is None


def test_catalog_input_is_strict_and_bounded():
    assert CatalogInput().model_dump() == {"limit": 50, "after_id": None}
    assert CatalogInput(limit=100, after_id=0).after_id == 0
    assert CatalogInput(after_id=BIGINT_MAX).after_id == BIGINT_MAX

    for arguments in (
        {"limit": 0},
        {"limit": 101},
        {"limit": "10"},
        {"limit": True},
        {"after_id": -1},
        {"after_id": BIGINT_MAX + 1},
        {"after_id": "2"},
        {"department_id": "3"},
    ):
        with pytest.raises(ValidationError):
            CatalogInput.model_validate(arguments)


def test_read_catalog_reuses_active_authorization_and_returns_bounded_keyset_page():
    rows = [
        SimpleNamespace(id=11, name="公共库", type="public"),
        SimpleNamespace(id=14, name="本部门库", type="department"),
        SimpleNamespace(id=17, name="后一页", type="department"),
    ]
    scalar_result = Mock()
    scalar_result.all.return_value = rows
    session = Mock()
    session.scalars = AsyncMock(return_value=scalar_result)
    identity = CurrentIdentity(
        User(id=5, role="user", department_id=9, status="active"),
        Department(id=9, status="active"),
    )

    result = asyncio.run(read_catalog(session, identity, CatalogInput(limit=2, after_id=8)))

    assert result == {
        "items": [
            {"id": "11", "name": "公共库", "type": "public"},
            {"id": "14", "name": "本部门库", "type": "department"},
        ],
        "count": 2,
        "has_more": True,
        "next_after_id": 14,
    }
    statement = session.scalars.await_args.args[0]
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "deleted_at IS NULL" in sql
    assert "type = 'public'" in sql
    assert "department_id = 9" in sql
    assert "id > 8" in sql
    assert "LIMIT 3" in sql
    session.commit.assert_not_called()


def test_render_is_deterministic_escapes_markdown_source_markers_and_controls():
    result = {
        "items": [
            {
                "id": "12",
                "name": "Policy [source_1]\r\n**bold** <script>alert(1)</script>",
                "type": "public",
            }
        ],
        "count": 1,
        "has_more": True,
        "next_after_id": 12,
    }

    rendered = render(result)

    assert rendered == render(result)
    assert "［source_1］" in rendered
    assert "**bold**" in rendered
    assert "<script>" in rendered
    assert "\r" not in rendered and "\n\n" not in rendered
    assert "还有更多知识库，可继续询问：列出 ID 12 之后的知识库。" in rendered
    assert "[source_1]" not in rendered


def test_render_empty_page_and_rejects_inconsistent_paging():
    assert render({"items": [], "count": 0, "has_more": False, "next_after_id": None}) == (
        "当前范围没有可显示的知识库记录。"
    )
    with pytest.raises(ValueError, match="invalid knowledge catalog result"):
        render({"items": [], "count": 1, "has_more": False, "next_after_id": None})
    with pytest.raises(ValueError, match="invalid knowledge catalog result"):
        render({"items": [], "count": 0, "has_more": True, "next_after_id": None})


def test_tool_exposes_only_bounded_readonly_catalog_schema():
    catalog_tool = tool()
    schema = catalog_tool.args_schema
    assert catalog_tool.name == "knowledge_base_list"
    assert isinstance(schema, dict)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["limit"]["default"] == 50
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 100
    assert schema["properties"]["after_id"]["anyOf"][0]["minimum"] == 0
    assert "只读" in catalog_tool.description
    with pytest.raises(RuntimeError, match="authenticated Tool Gateway"):
        asyncio.run(catalog_tool.ainvoke({"limit": 5}))
