import copy
import json
import os

import httpx
import pytest
import yaml
from langchain_core.messages import HumanMessage
from pydantic import ValidationError
from test_rag_providers import install_transport, provider_config  # noqa: F401

from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.agent.schemas import Plan
from enterprise_mind.agent.tools import retrieval_tool
from enterprise_mind.database.sql_provider import ChatDatabaseModel, DatabaseSelection, SQLDraft
from enterprise_mind.infrastructure.model_transport import model_proxy_url
from enterprise_mind.infrastructure.settings import ConfigurationError
from enterprise_mind.ingestion.media_providers import HTTPVision
from enterprise_mind.ingestion.provider_factory import create_ocr_provider
from enterprise_mind.ingestion.tencent_ocr import TencentOCR, tc3_headers
from enterprise_mind.rag.config import (
    ChatRequestOptions,
    EmbeddingConfig,
    LLMConfig,
    ProvidersConfig,
    RAGSettings,
    TencentOCRConfig,
    VisionProviderConfig,
    load_rag,
)
from enterprise_mind.rag.providers import ProviderFailure, llm_token_counter


def test_plan_rejects_duplicate_database_queries():
    with pytest.raises(ValidationError, match="multiple database queries"):
        Plan.model_validate(
            {
                "intent_kind": "database",
                "requires_enterprise_facts": True,
                "tools": [
                    {
                        "key": f"database_{index}",
                        "tool_name": "database_query",
                        "arguments": {"question": "查询订单明细"},
                    }
                    for index in range(2)
                ],
            }
        )


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        (
            DatabaseSelection,
            {
                "relations": [{"schema_name": "business", "relation_name": "phase10_orders"}],
                "clarification": "none",
            },
        ),
        (
            SQLDraft,
            {
                "query_kind": "detail",
                "sql": 'SELECT "id" FROM "business"."phase10_orders" ORDER BY "id"',
                "parameters": [],
                "conditions_summary": "合成订单",
            },
        ),
    ],
)
async def test_database_json_mode_recovers_from_invalid_shape_and_single_wrapper(
    provider_config,  # noqa: F811
    monkeypatch,
    schema,
    value,
):
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    config = LLMConfig.model_validate(
        {
            **provider_config,
            "max_input_tokens": 32000,
            "max_output_tokens": 500,
            "max_retries": 0,
            "structured_output_method": "json_mode",
            "request_options": {"thinking": {"type": "disabled"}},
        }
    )
    model = ChatModel(config)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        content = {"invalid": "shape"} if len(calls) == 1 else {"result": value}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(content)},
                    }
                ]
            },
        )

    model.http._transport = httpx.MockTransport(handler)
    try:
        result = await ChatDatabaseModel(model).structured(
            schema, "Synthetic database check.", {"question": "查询合成订单"}
        )
        assert isinstance(result, schema)
        assert len(calls) == 2
        assert "上一份结果未通过结构校验" in calls[1]["messages"][0]["content"]
    finally:
        await model.close()


def polygon(x=1, y=2, w=30, h=10):
    return [dict(X=x, Y=y), dict(X=x + w, Y=y), dict(X=x + w, Y=y + h), dict(X=x, Y=y + h)]


def text_response():
    return {
        "Response": {
            "TextDetections": [{"DetectedText": "样本", "Confidence": 98, "Polygon": polygon()}]
        }
    }


def table_response():
    cells = [
        dict(RowTl=0, RowBr=1, ColTl=0, ColBr=2, Text="合并表头", Polygon=polygon()),
        dict(RowTl=1, RowBr=2, ColTl=0, ColBr=1, Text="项目", Polygon=polygon()),
        dict(RowTl=1, RowBr=2, ColTl=1, ColBr=2, Text="内容", Polygon=polygon()),
    ]
    return {
        "Response": {"TableDetections": [{"Type": 1, "Cells": cells, "TableCoordPoint": polygon()}]}
    }


@pytest.fixture
def ocr_config(tmp_path, monkeypatch):
    for env, value in (
        ("EM_OCR_SECRET_ID_FILE", "fixture-id"),
        ("EM_OCR_SECRET_KEY_FILE", "fixture-secret"),
    ):
        path = tmp_path / env
        path.write_text(value, encoding="utf-8")
        monkeypatch.setenv(env, str(path))
    monkeypatch.delenv("EM_MODEL_PROXY_URL", raising=False)
    return TencentOCRConfig(
        protocol="tencent_tc3_ocr",
        provider_key="fixture",
        model_key="fixture-v1",
        model="ReadText",
        table_action="ReadTable",
        base_url="https://ocr.test",
        api_version="2018-11-19",
        region="ap-guangzhou",
        max_image_base64_bytes=1024,
        timeout_seconds=1,
        max_retries=1,
    )


def test_only_explicit_model_proxy_is_used(monkeypatch):
    monkeypatch.delenv("EM_MODEL_PROXY_URL", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.test:9")
    assert model_proxy_url() is None
    monkeypatch.setenv("EM_MODEL_PROXY_URL", "http://127.0.0.1:12345")
    assert model_proxy_url() == "http://127.0.0.1:12345"
    monkeypatch.setenv("EM_MODEL_PROXY_URL", "http://user:private@proxy.test:123")
    with pytest.raises(ConfigurationError) as error:
        model_proxy_url()
    assert "private" not in str(error.value)


@pytest.mark.parametrize("field", ["model", "messages", "tools", "max_tokens", "base_url"])
def test_request_options_cannot_override_protected_fields(field):
    with pytest.raises(ValidationError):
        ChatRequestOptions.model_validate({field: "override"})


@pytest.mark.parametrize(
    "values",
    [
        {"enable_thinking": "false"},
        {"enable_thinking": 0},
        {"enable_thinking": False, "thinking": {"type": "disabled"}},
    ],
)
def test_thinking_extensions_reject_ambiguous_or_non_boolean_values(values):
    with pytest.raises(ValidationError):
        ChatRequestOptions.model_validate(values)


def test_llm_byte_budget_does_not_reuse_another_tokenizer(provider_config):  # noqa: F811
    values = {**provider_config, "max_input_tokens": 32000, "max_output_tokens": 500}
    with pytest.raises(ValidationError):
        LLMConfig.model_validate({**values, "token_counter": "utf8_bytes"})
    values.pop("tokenizer_file")
    values.pop("tokenizer_sha256")
    with pytest.raises(ValidationError):
        LLMConfig.model_validate(values)
    config = LLMConfig.model_validate({**values, "token_counter": "utf8_bytes"})
    counter = llm_token_counter(config)
    assert counter.count("企业A🙂") == 11
    assert counter.count("") == 0
    # Embedding still needs its actual tokenizer; a rough LLM budget cannot size chunks.
    with pytest.raises(ValidationError):
        EmbeddingConfig.model_validate(
            {**values, "dimensions": 1024, "token_counter": "utf8_bytes"}
        )


def test_ocr_protocol_and_endpoint_validation(ocr_config):
    configured = ProvidersConfig.model_validate(
        {"allowed_origins": ["https://ocr.test"], "ocr": ocr_config.model_dump()}
    )
    assert isinstance(configured.ocr, TencentOCRConfig)
    assert isinstance(create_ocr_provider(configured.ocr), TencentOCR)
    assert create_ocr_provider(None) is None
    with pytest.raises(ValidationError):
        TencentOCRConfig.model_validate(
            {**ocr_config.model_dump(), "base_url": "https://ocr.test/unexpected"}
        )
    with pytest.raises(ValidationError):
        ProvidersConfig(allowed_origins=[], ocr=ocr_config)


async def test_tc3_signed_requests_and_merged_cell_conversion(ocr_config, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/"
        assert json.loads(request.content) == {"ImageBase64": "cG5n"}
        assert request.headers["x-tc-version"] == "2018-11-19"
        assert "SignedHeaders=content-type;host;x-tc-action" in request.headers["authorization"]
        assert "fixture-secret" not in str(request.headers)
        return httpx.Response(200, json=text_response() if len(calls) == 1 else table_response())

    install_transport(monkeypatch, handler)
    result = await TencentOCR(ocr_config).recognize(b"png", 100, 100)
    assert [r.headers["x-tc-action"] for r in calls] == ["ReadText", "ReadTable"]
    assert result.blocks[0].confidence == 0.98
    assert result.tables[0].rows == [["合并表头", ""], ["项目", "内容"]]
    assert result.tables[0].merged_ranges == ["A1:B1"]

    headers = tc3_headers(
        ocr_config, "fixture-id", "fixture-secret", "ReadText", b"png", 1700000000
    )
    for action, body, stamp in (
        ("ReadTable", b"png", 1700000000),
        ("ReadText", b"different", 1700000000),
        ("ReadText", b"png", 1700000001),
    ):
        changed = tc3_headers(ocr_config, "fixture-id", "fixture-secret", action, body, stamp)
        assert changed["Authorization"] != headers["Authorization"]


async def test_ocr_can_disable_table_call_and_retries_transient_only(ocr_config, monkeypatch):
    config = ocr_config.model_copy(update={"table_action": None})
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "Response": {"Error": {"Code": "RequestLimitExceeded", "Message": "private"}}
                },
            )
        return httpx.Response(200, json=text_response())

    install_transport(monkeypatch, handler)
    result = await TencentOCR(config).recognize(b"png", 100, 100)
    assert len(calls) == 2 and result.tables == []
    assert all(r.headers["x-tc-action"] == "ReadText" for r in calls)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("AuthFailure.SignatureFailure", "OCR_AUTH_FAILED"),
        ("UnauthorizedOperation", "OCR_PERMISSION_DENIED"),
        ("FailedOperation", "OCR_PROVIDER_REJECTED"),
    ],
)
async def test_ocr_business_errors_are_redacted_and_not_retried(
    ocr_config, monkeypatch, code, expected
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"Response": {"Error": {"Code": code, "Message": "fixture-secret PRIVATE_TEXT"}}},
        )

    install_transport(monkeypatch, handler)
    with pytest.raises(ProviderFailure, match=expected) as error:
        await TencentOCR(ocr_config).recognize(b"png", 100, 100)
    assert len(calls) == 1 and "PRIVATE_TEXT" not in str(error.value)


@pytest.mark.parametrize("bad", ["bounds", "confidence", "overlap", "span", "huge", "index_type"])
async def test_ocr_invalid_coordinates_and_table_structure_are_rejected(
    ocr_config, monkeypatch, bad
):
    text, table = text_response(), table_response()
    if bad == "bounds":
        text["Response"]["TextDetections"][0]["Polygon"] = polygon(x=90)
    elif bad == "confidence":
        text["Response"]["TextDetections"][0]["Confidence"] = 101
    else:
        cells = table["Response"]["TableDetections"][0]["Cells"]
        if bad == "overlap":
            cells.append(copy.deepcopy(cells[0]))
        elif bad == "span":
            cells[0]["RowBr"] = 0
        elif bad == "huge":
            cells[0].update(RowBr=10000, ColBr=1000)
        else:
            cells[0]["RowBr"] = 1.5
    install_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200, json=text if r.headers["x-tc-action"] == "ReadText" else table
        ),
    )
    with pytest.raises(ProviderFailure, match="OCR_RESPONSE_INVALID"):
        await TencentOCR(ocr_config).recognize(b"png", 100, 100)


async def test_ocr_input_budget_and_redirect_prevent_extra_requests(ocr_config, monkeypatch):
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.test"})

    install_transport(monkeypatch, redirect)
    provider = TencentOCR(ocr_config)
    with pytest.raises(ProviderFailure, match="OCR_INPUT_INVALID"):
        await provider.recognize(b"x" * 1025, 100, 100)
    assert calls == []
    with pytest.raises(ProviderFailure, match="OCR_PROVIDER_REJECTED"):
        await provider.recognize(b"png", 100, 100)
    assert len(calls) == 1


@pytest.mark.parametrize("wrapped", [False, True])
async def test_json_mode_plan_is_schema_validated(provider_config, monkeypatch, wrapped):  # noqa: F811
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    config = LLMConfig.model_validate(
        {
            **provider_config,
            "max_input_tokens": 32000,
            "max_output_tokens": 500,
            "max_retries": 0,
            "structured_output_method": "json_mode",
            "request_options": {"thinking": {"type": "disabled"}},
        }
    )
    model = ChatModel(config)

    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body and "intent_kind" in body["messages"][0]["content"]
        plan = {"intent_kind": "general", "requires_enterprise_facts": False, "tools": []}
        content = json.dumps({"plan": plan} if wrapped else plan)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            },
        )

    model.http._transport = httpx.MockTransport(handler)
    try:
        assert (await model.plan([HumanMessage("欢迎词")], [], "")).intent_kind == "general"
    finally:
        await model.close()


@pytest.mark.parametrize("second_valid", [True, False])
async def test_json_mode_plan_retries_only_one_invalid_schema(
    provider_config,  # noqa: F811
    monkeypatch,
    second_valid,
):
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    config = LLMConfig.model_validate(
        {
            **provider_config,
            "max_input_tokens": 32000,
            "max_output_tokens": 500,
            "max_retries": 0,
            "structured_output_method": "json_mode",
            "request_options": {"thinking": {"type": "disabled"}},
        }
    )
    model = ChatModel(config)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        valid = len(calls) == 2 and second_valid
        content = json.dumps(
            {
                "intent_kind": "general" if valid else "invalid_intent",
                "requires_enterprise_facts": False,
                "tools": [],
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            },
        )

    model.http._transport = httpx.MockTransport(handler)
    try:
        if second_valid:
            assert (await model.plan([HumanMessage("欢迎词")], [], "")).intent_kind == "general"
        else:
            with pytest.raises(ValueError, match="invalid plan schema"):
                await model.plan([HumanMessage("欢迎词")], [], "")
        assert len(calls) == 2
        assert "上一份规划未通过结构或工具名称校验" in calls[1]["messages"][0]["content"]
    finally:
        await model.close()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_json_mode_plan_rejects_invented_tool_before_execution(
    provider_config,  # noqa: F811
    monkeypatch,
    wrapped,
):
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    config = LLMConfig.model_validate(
        {
            **provider_config,
            "max_input_tokens": 32000,
            "max_output_tokens": 500,
            "max_retries": 0,
            "structured_output_method": "json_mode",
            "request_options": {"thinking": {"type": "disabled"}},
        }
    )
    model = ChatModel(config)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        name = "invented_retrieval" if len(calls) == 1 else "knowledge_retrieve"
        plan = {
            "intent_kind": "knowledge",
            "requires_enterprise_facts": True,
            "tools": [{"key": "read", "tool_name": name, "arguments": {"question": "报销期限"}}],
        }
        content = json.dumps({"plan": plan} if wrapped else plan)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            },
        )

    async def never_execute(body):
        raise AssertionError("planning must not execute tools")

    model.http._transport = httpx.MockTransport(handler)
    try:
        plan = await model.plan(
            [HumanMessage("公司的出差报销期限是几天？")], [retrieval_tool(never_execute)], ""
        )
        assert len(calls) == 2
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert '"knowledge_retrieve"' in calls[0]["messages"][0]["content"]
        assert [tool.tool_name for tool in plan.tools] == ["knowledge_retrieve"]
    finally:
        await model.close()


async def test_vision_request_options_and_text_format(tmp_path, monkeypatch):
    secret = tmp_path / "vision"
    secret.write_text("fixture-secret", encoding="utf-8")
    monkeypatch.setenv("EM_VISION_API_KEY_FILE", str(secret))
    config = VisionProviderConfig.model_validate(
        {
            "provider_key": "fixture",
            "model_key": "fixture-v1",
            "model": "fixture",
            "base_url": "https://vision.test",
            "response_format": "text",
            "request_options": {"thinking": {"type": "disabled"}},
        }
    )
    provider = HTTPVision(config)

    async def respond(path, body):
        assert path == "/chat/completions" and "response_format" not in body
        assert body["thinking"] == {"type": "disabled"}
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"description": "红色方形", "image_type": "other", "uncertainties": []}
                        )
                    },
                }
            ]
        }

    monkeypatch.setattr(provider, "request", respond)
    assert (await provider.describe(b"png", 100, 100)).description == "红色方形"


def test_tokenizer_path_is_relative_to_configuration(provider_config, tmp_path):  # noqa: F811
    rag_path, provider_path = tmp_path / "rag.yaml", tmp_path / "providers.yaml"
    rag_path.write_text("rag: {}", encoding="utf-8")
    values = {
        **provider_config,
        "max_input_tokens": 32000,
        "max_output_tokens": 500,
        "tokenizer_file": "tokenizer.json",
    }
    provider_path.write_text(
        yaml.safe_dump({"allowed_origins": ["https://provider.test"], "llm": values}),
        encoding="utf-8",
    )
    _, config = load_rag(RAGSettings(providers_config=provider_path, rag_config=rag_path))
    assert config.llm.tokenizer_file == str((tmp_path / "tokenizer.json").resolve())


async def test_model_rate_limits_are_distinct_from_invalid_credentials(
    provider_config,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    model = ChatModel(
        LLMConfig.model_validate(
            {
                **provider_config,
                "max_input_tokens": 32000,
                "max_output_tokens": 500,
                "max_retries": 0,
            }
        )
    )
    model.http._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429, json={"error": {"code": "busy", "message": "PRIVATE_RESPONSE"}}
        )
    )
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_RATE_LIMITED"):
            await model.plan([HumanMessage("问候")], [], "")
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_RATE_LIMITED"):
            _ = [part async for part in model.answer([HumanMessage("问候")])]
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_RATE_LIMITED"):
            await model.summarize([HumanMessage("问候")])
    finally:
        await model.close()
