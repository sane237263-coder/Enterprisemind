import httpx
import pytest
from pydantic import ValidationError

from enterprise_mind.agent.diagnostics import record_failure
from enterprise_mind.agent.schemas import RetrievalInput
from enterprise_mind.rag.providers import ProviderFailure


def test_failures_log_only_safe_metadata(caplog):
    private = "PRIVATE_UPSTREAM_TOKEN_AND_BODY"
    request = httpx.Request("POST", "https://private.invalid/secret?token=" + private)
    error = httpx.HTTPStatusError(private, request=request, response=httpx.Response(401))
    error.status_code = 401
    record_failure("provider.answer", error, run_id=42)
    with pytest.raises(ValidationError) as caught:
        RetrievalInput(question="内容", document_ids=[private])
    record_failure("plan", caught.value, run_id=42)
    record_failure("generate_answer", ProviderFailure("ANSWER_SOURCE_INVALID"), run_id=42)
    record_failure("plan", ValueError(private))
    assert "http_status=401" in caplog.text
    assert "exception_type=ValidationError" in caplog.text
    assert "code=ANSWER_SOURCE_INVALID" in caplog.text
    assert private not in caplog.text and "private.invalid" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
