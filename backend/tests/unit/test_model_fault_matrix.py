# ruff: noqa: F811

"""Explicit retry ceilings and permanent error classification at the HTTP boundary."""

import json
import os

import httpx
import pytest
from langchain_core.messages import HumanMessage
from test_rag_providers import provider_config  # noqa: F401

from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.rag.config import LLMConfig
from enterprise_mind.rag.providers import ProviderFailure


@pytest.mark.parametrize(
    "failure,attempts", [(429, 4), (503, 4), ("timeout", 4), (400, 1), (401, 1)]
)
async def test_model_retry_ceiling_and_permanent_errors(
    provider_config,
    monkeypatch,
    failure,
    attempts,  # noqa: F811
):
    monkeypatch.setenv("EM_LLM_API_KEY_FILE", os.environ["EM_EMBEDDING_API_KEY_FILE"])
    values = {
        **provider_config,
        "max_input_tokens": 32000,
        "max_output_tokens": 128,
        "timeout_seconds": 30,
        "max_retries": 3,
    }
    model = ChatModel(LLMConfig(**values))
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic provider timeout", request=request)
        return httpx.Response(failure, text="PRIVATE_UPSTREAM_BODY credential=test-secret")

    model.http._transport = httpx.MockTransport(respond)
    try:
        expected = (
            "MODEL_PROVIDER_RATE_LIMITED"
            if failure == 429
            else "MODEL_PLANNING_TIMEOUT"
            if failure == "timeout"
            else "MODEL_PROVIDER_FAILED"
        )
        with pytest.raises(ProviderFailure, match=expected) as error:
            await model.plan([HumanMessage("写欢迎词")], [], "")
        assert len(calls) == attempts
        assert {call["model"] for call in calls} == {"fixture"}
        assert "test-secret" not in str(error.value) and "PRIVATE_UPSTREAM" not in str(error.value)
    finally:
        await model.close()
