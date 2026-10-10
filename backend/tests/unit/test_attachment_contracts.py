import pytest
from pydantic import ValidationError

from enterprise_mind.agent.lifecycle_tools import DocumentOverwriteTool, DocumentUploadTool
from enterprise_mind.agent.schemas import MessageCreate


def test_message_attachment_ids_are_bounded_strings_and_unique():
    assert MessageCreate(content_text="hello").model_dump() == {"content_text": "hello"}
    for identifiers in (["1", "1"], list(map(str, range(1, 12))), [1], ["01"], ["0"]):
        with pytest.raises(ValidationError):
            MessageCreate(content_text="保存附件", staged_upload_ids=identifiers)
    with pytest.raises(ValidationError):
        MessageCreate(content_text="保存", content_parts=[{"url": "https://invalid.test/file"}])


@pytest.mark.parametrize(
    "field", ["object_key", "url", "path", "base64", "content_sha256", "mime_type"]
)
def test_file_tools_reject_model_supplied_storage_and_file_metadata(field):
    with pytest.raises(ValidationError):
        DocumentUploadTool(knowledge_base_id="1", files=[{"staged_upload_id": "1", field: "value"}])
    with pytest.raises(ValidationError):
        DocumentOverwriteTool.model_validate(
            {"target": {"id": "1"}, "staged_upload_id": "2", field: "value"}
        )


def test_file_tools_reject_duplicate_ids_and_empty_batches():
    for files in ([], [{"staged_upload_id": "1"}, {"staged_upload_id": "1", "name": "other"}]):
        with pytest.raises(ValidationError):
            DocumentUploadTool(knowledge_base_id="1", files=files)
