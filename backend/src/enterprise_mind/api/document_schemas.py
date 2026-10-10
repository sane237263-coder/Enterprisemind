from typing import Annotated, Literal

from pydantic import BaseModel, Field

ProcessingStatus = Literal["pending", "processing", "success", "partial_success", "failed"]
Stage = Literal[
    "parse",
    "ocr",
    "vision",
    "table",
    "code",
    "chunk",
    "embedding",
    "keyword_index",
    "preview",
    "activate",
]


class AcceptedUpload(BaseModel):
    index: int
    filename: str
    status: Literal["accepted"]
    document_id: str
    processing_task_id: str


class RejectedUpload(BaseModel):
    index: int
    filename: str
    status: Literal["rejected"]
    error_code: str
    message: str


class NameConflictUpload(BaseModel):
    index: int
    filename: str
    status: Literal["name_conflict"]
    document_id: str
    name: str
    error_code: Literal["DOCUMENT_NAME_CONFLICT"]


class UploadResult(BaseModel):
    items: list[
        Annotated[
            AcceptedUpload | RejectedUpload | NameConflictUpload, Field(discriminator="status")
        ]
    ]


class DocumentSummary(BaseModel):
    id: str
    knowledge_base_id: str
    folder_id: str | None
    name: str
    description: str | None
    created_by: str
    deleted_at: str | None = None
    purge_after: str | None = None
    deletion_batch_id: str | None = None
    is_deletion_root: bool = False
    processing_status: ProcessingStatus | Literal["recycle_bin"]
    file_extension: str
    mime_type: str
    size_bytes: int
    row_version: int
    created_at: str
    updated_at: str


class FailedUnitSummary(BaseModel):
    id: str
    stage: Stage
    source_unit_id: str | None
    error_code: str | None
    error_message: str | None


class UnitProgress(BaseModel):
    id: str
    stage: Stage
    status: Literal["pending", "processing", "success", "failed", "skipped"]
    current: int | None
    total: int | None


class ProcessingSummary(BaseModel):
    document_id: str
    processing_task_id: str
    processing_status: ProcessingStatus
    task_status: ProcessingStatus
    current_stage: Stage | None
    stage_current: int | None
    stage_total: int | None
    failed_unit_count: int
    failed_units: list[FailedUnitSummary]
    units: list[UnitProgress]
    updated_at: str


UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["files"],
                    "additionalProperties": False,
                    "properties": {
                        "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                        "folder_id": {"type": "string", "pattern": "^[1-9][0-9]*$"},
                        "name": {"type": "string", "maxLength": 255, "description": "仅单文件上传"},
                        "description": {"type": "string"},
                    },
                },
            },
        },
    },
}
