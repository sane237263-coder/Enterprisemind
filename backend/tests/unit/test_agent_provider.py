import asyncio
import json
import os

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import ValidationError
from test_rag_providers import provider_config  # noqa: F401

from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.agent.tools import retrieval_tool
from enterprise_mind.api.errors import ERRORS, ErrorCode
from enterprise_mind.rag.config import LLMConfig
from enterprise_mind.rag.providers import ProviderFailure


def test_plan_timeout_configuration_is_bounded(provider_config):  # noqa: F811
    values = {**provider_config, "max_input_tokens": 32000, "max_output_tokens": 500}
    config = LLMConfig(**values)
    assert config.plan_timeout_seconds == 45
    assert config.summary_timeout_seconds == 10
    assert config.answer_idle_timeout_seconds == 45
    for field, invalid_values in (
        ("plan_timeout_seconds", (0, 121)),
        ("summary_timeout_seconds", (0, 31)),
        ("answer_idle_timeout_seconds", (0, 121)),
    ):
        for timeout in invalid_values:
            with pytest.raises(ValidationError):
                LLMConfig(**{**values, field: timeout})


def test_model_response_timeout_has_a_clear_public_gateway_error():
    status, message = ERRORS[ErrorCode.MODEL_RESPONSE_TIMEOUT]
    assert status == 504
    assert "模型响应超时" in message and "模型服务当前繁忙" not in message


@pytest.fixture(params=["local_tokenizer", "utf8_bytes"])
def model(provider_config, monkeypatch, request):  # noqa: F811
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    values = dict(provider_config)
    if request.param == "utf8_bytes":
        values.pop("tokenizer_file")
        values.pop("tokenizer_sha256")
        values.update(token_counter="utf8_bytes", request_options={"enable_thinking": False})
    return ChatModel(
        LLMConfig(
            **{
                **values,
                "max_input_tokens": 32000,
                "max_output_tokens": 500,
                "max_retries": 0,
            }
        )
    )


async def test_langchain_plan_and_stream_use_explicit_protocol(model):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert body["model"] == "fixture"
        if model.model.extra_body:
            assert body["enable_thinking"] is False
            assert "thinking" not in body
        if body.get("stream"):
            lines = [
                {
                    "id": "test-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": '{"lines":[]}'
                                if body.get("response_format")
                                else "可见答案",
                                "reasoning_content": "不可输出的内部推理",
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "test-stream",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            content = "".join("data: " + json.dumps(line) + "\n\n" for line in lines)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=content + "data: [DONE]\n\n",
            )
        plan = {"intent_kind": "general", "requires_enterprise_facts": False, "tools": []}
        return httpx.Response(
            200,
            json={
                "id": "test-plan",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "Plan", "arguments": json.dumps(plan)},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    model.http._transport = httpx.MockTransport(respond)

    async def no_execute(body):
        raise AssertionError("planning cannot execute tools")

    try:
        write_tool = StructuredTool.from_function(
            name="folder_delete",
            description="删除文件夹，等待用户确认。",
            args_schema={
                "$id": "enterprise-mind://tools/folder_delete/input/v1",
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            coroutine=no_execute,
        )
        plan = await model.plan(
            [HumanMessage("写欢迎词")], [retrieval_tool(no_execute), write_tool], ""
        )
        assert plan.intent_kind == "general"
        parts = [part async for part in model.answer([HumanMessage("写欢迎词")])]
        assert "".join(parts) == "可见答案"
        assert "response_format" not in requests[-1]
        assert "temperature" not in requests[-1]
        encoded = "".join(
            [part async for part in model.answer([HumanMessage("Return JSON")], json_mode=True)]
        )
        assert encoded == '{"lines":[]}'
        assert requests[-1]["response_format"] == {"type": "json_object"}
        assert requests[-1]["temperature"] == 0
        assert (
            "".join([part async for part in model.answer([HumanMessage("写欢迎词")])]) == "可见答案"
        )
        assert "response_format" not in requests[-1]
        assert "temperature" not in requests[-1]
        assert len(requests) == 4 and requests[0]["tools"][0]["function"]["name"] == "Plan"
        assert "test-secret" not in str(requests)
        assert (
            "enterprise-mind://tools/folder_delete/input/v1"
            in requests[0]["messages"][0]["content"]
        )
    finally:
        await model.close()


async def test_provider_errors_are_redacted_and_budget_prevents_requests(model):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(401, text="PRIVATE_UPSTREAM_BODY credential=test-secret")

    model.http._transport = httpx.MockTransport(respond)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_FAILED") as caught:
            await model.plan([HumanMessage("一般问题")], [], "")
        assert "PRIVATE_UPSTREAM" not in str(caught.value) and len(calls) == 1
        model.max_input_tokens = 1000
        with pytest.raises(ProviderFailure, match="RUN_LIMIT_REACHED"):
            await model.plan([HumanMessage("一般问题")], [], "")
        assert len(calls) == 1
    finally:
        await model.close()


async def test_plan_timeout_cancels_inflight_model_call(model, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    model.plan_timeout_seconds = 1

    class HangingPlan:
        async def ainvoke(self, inputs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    def with_structured_output(self, *args, **kwargs):
        return HangingPlan()

    monkeypatch.setattr(type(model.model), "with_structured_output", with_structured_output)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PLANNING_TIMEOUT") as caught:
            await model.plan([HumanMessage("查询当前知识库")], [], "")
        assert started.is_set() and cancelled.is_set()
        assert "MODEL_PLANNING_TIMEOUT" in str(caught.value)
    finally:
        await model.close()


async def test_summary_timeout_cancels_optional_context_call_and_logs_safe_stage(
    model, monkeypatch, caplog
):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    model.summary_timeout_seconds = 0.3

    async def hanging(self, inputs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(type(model.model), "ainvoke", hanging)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_RESPONSE_TIMEOUT"):
            await model.summarize([HumanMessage("旧会话")])
        assert started.is_set() and cancelled.is_set()
        assert "stage=provider.summarize" in caplog.text
        assert "旧会话" not in caplog.text
    finally:
        await model.close()


@pytest.mark.parametrize("summary_text", ["", " \n\t"])
async def test_summary_rejects_empty_or_whitespace_result(model, monkeypatch, summary_text):
    async def summarize(self, inputs):
        return AIMessage(content=summary_text)

    monkeypatch.setattr(type(model.model), "ainvoke", summarize)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_FAILED"):
            await model.summarize([HumanMessage("旧会话")])
    finally:
        await model.close()


@pytest.mark.parametrize("finish_reason", ["stop", "length", "content_filter"])
async def test_answer_rejects_truncated_or_filtered_stream(model, monkeypatch, finish_reason):
    async def stream(self, messages):
        yield AIMessageChunk(content="partial answer")
        yield AIMessageChunk(content="", response_metadata={"finish_reason": finish_reason})

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        if finish_reason == "stop":
            assert "".join([part async for part in model.answer([HumanMessage("translate")])]) == (
                "partial answer"
            )
        else:
            with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_FAILED"):
                _ = [part async for part in model.answer([HumanMessage("translate")])]
    finally:
        await model.close()


async def test_answer_closes_upstream_stream_when_finish_reason_is_invalid(model, monkeypatch):
    closed = asyncio.Event()
    streams = []

    def stream(self, messages):
        async def chunks():
            try:
                yield AIMessageChunk(content="partial answer")
                yield AIMessageChunk(content="", response_metadata={"finish_reason": "length"})
            finally:
                closed.set()

        iterator = chunks()
        streams.append(iterator)
        return iterator

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_FAILED"):
            _ = [part async for part in model.answer([HumanMessage("写欢迎词")])]
        assert streams and closed.is_set()
    finally:
        await model.close()


async def test_answer_closes_upstream_stream_when_consumer_stops(model, monkeypatch):
    closed = asyncio.Event()

    async def stream(self, messages):
        try:
            yield AIMessageChunk(content="第一段")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        answer = model.answer([HumanMessage("写欢迎词")])
        assert await anext(answer) == "第一段"
        await answer.aclose()
        assert closed.is_set()
    finally:
        await model.close()


@pytest.mark.parametrize("mode", ["empty", "whitespace", "missing_stop"])
async def test_answer_rejects_empty_or_unterminated_stream(model, monkeypatch, mode):
    async def stream(self, messages):
        if mode == "empty":
            yield AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})
        elif mode == "whitespace":
            yield AIMessageChunk(content=" \n\t")
            yield AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})
        else:
            yield AIMessageChunk(content="partial answer")

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_FAILED"):
            _ = [part async for part in model.answer([HumanMessage("写欢迎词")])]
    finally:
        await model.close()


async def test_answer_metadata_only_chunks_do_not_extend_idle_deadline(model, monkeypatch):
    cancelled = asyncio.Event()
    model.answer_idle_timeout_seconds = 0.3

    async def stream(self, messages):
        try:
            while True:
                yield AIMessageChunk(content="", response_metadata={"keepalive": True})
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def collect():
        return [part async for part in model.answer([HumanMessage("写欢迎词")])]

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_RESPONSE_TIMEOUT"):
            await asyncio.wait_for(collect(), timeout=2)
        assert cancelled.is_set()
    finally:
        await model.close()


async def test_answer_idle_deadline_resets_after_visible_text(model, monkeypatch):
    model.answer_idle_timeout_seconds = 0.3

    async def stream(self, messages):
        await asyncio.sleep(0.18)
        yield AIMessageChunk(content="第一段")
        await asyncio.sleep(0.18)
        yield AIMessageChunk(content="第二段")
        yield AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        result = [part async for part in model.answer([HumanMessage("写欢迎词")])]
        assert "".join(result) == "第一段第二段"
    finally:
        await model.close()


async def test_answer_stalled_after_visible_text_times_out_and_closes(model, monkeypatch):
    closed = asyncio.Event()
    model.answer_idle_timeout_seconds = 0.3

    async def stream(self, messages):
        try:
            yield AIMessageChunk(content="第一段")
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def collect():
        return [part async for part in model.answer([HumanMessage("写欢迎词")])]

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_RESPONSE_TIMEOUT"):
            await asyncio.wait_for(collect(), timeout=2)
        assert closed.is_set()
    finally:
        await model.close()


async def test_answer_cancellation_is_not_reclassified_as_timeout(model, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stream(self, messages):
        started.set()
        try:
            await asyncio.Event().wait()
            yield AIMessageChunk(content="unreachable")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def collect():
        return [part async for part in model.answer([HumanMessage("写欢迎词")])]

    monkeypatch.setattr(type(model.model), "astream", stream)
    try:
        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    finally:
        await model.close()


async def test_answer_http_429_remains_rate_limited(model):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, text="PRIVATE_UPSTREAM_BODY")

    model.http._transport = httpx.MockTransport(respond)
    try:
        with pytest.raises(ProviderFailure, match="MODEL_PROVIDER_RATE_LIMITED") as caught:
            _ = [part async for part in model.answer([HumanMessage("写欢迎词")])]
        assert len(calls) == 1
        assert "PRIVATE_UPSTREAM" not in str(caught.value)
    finally:
        await model.close()
