from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    DEPARTMENT_DISABLED = "DEPARTMENT_DISABLED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ROW_VERSION_CONFLICT = "ROW_VERSION_CONFLICT"
    NAME_CONFLICT = "NAME_CONFLICT"
    DEPARTMENT_IN_USE = "DEPARTMENT_IN_USE"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_CONTENT_MISMATCH = "FILE_CONTENT_MISMATCH"
    FILE_GENERATION_TIMEOUT = "FILE_GENERATION_TIMEOUT"
    DOCUMENT_NAME_CONFLICT = "DOCUMENT_NAME_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    ATTACHMENT_UNAVAILABLE = "ATTACHMENT_UNAVAILABLE"
    RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
    RUN_NOT_STOPPABLE = "RUN_NOT_STOPPABLE"
    RUN_NOT_RESUMABLE = "RUN_NOT_RESUMABLE"
    RUN_LIMIT_REACHED = "RUN_LIMIT_REACHED"
    MODEL_PROVIDER_NOT_CONFIGURED = "MODEL_PROVIDER_NOT_CONFIGURED"
    MODEL_PROVIDER_FAILED = "MODEL_PROVIDER_FAILED"
    MODEL_PLANNING_TIMEOUT = "MODEL_PLANNING_TIMEOUT"
    MODEL_RESPONSE_TIMEOUT = "MODEL_RESPONSE_TIMEOUT"
    MODEL_PROVIDER_RATE_LIMITED = "MODEL_PROVIDER_RATE_LIMITED"
    ANSWER_SOURCE_INVALID = "ANSWER_SOURCE_INVALID"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    DATABASE_SOURCE_UNAVAILABLE = "DATABASE_SOURCE_UNAVAILABLE"
    DATABASE_SOURCE_ALREADY_CONFIGURED = "DATABASE_SOURCE_ALREADY_CONFIGURED"
    DATABASE_CONNECTION_AUTH_FAILED = "DATABASE_CONNECTION_AUTH_FAILED"
    DATABASE_CONNECTION_NETWORK_FAILED = "DATABASE_CONNECTION_NETWORK_FAILED"
    DATABASE_CONNECTION_PERMISSION_FAILED = "DATABASE_CONNECTION_PERMISSION_FAILED"
    SCHEMA_SNAPSHOT_UNAVAILABLE = "SCHEMA_SNAPSHOT_UNAVAILABLE"
    DATABASE_RELATION_FORBIDDEN = "DATABASE_RELATION_FORBIDDEN"
    DATABASE_QUERY_REJECTED = "DATABASE_QUERY_REJECTED"
    DATABASE_QUERY_TIMEOUT = "DATABASE_QUERY_TIMEOUT"
    QUERY_RESULT_TOO_LARGE = "QUERY_RESULT_TOO_LARGE"
    QUERY_CONTEXT_EXPIRED = "QUERY_CONTEXT_EXPIRED"
    RECYCLE_STATE_CONFLICT = "RECYCLE_STATE_CONFLICT"
    FOLDER_CYCLE = "FOLDER_CYCLE"
    PROCESSING_NOT_RETRYABLE = "PROCESSING_NOT_RETRYABLE"
    PENDING_ACTION_BINDING_CHANGED = "PENDING_ACTION_BINDING_CHANGED"
    PENDING_ACTION_RESOLVED = "PENDING_ACTION_RESOLVED"
    DOCUMENT_PROCESSING_IN_PROGRESS = "DOCUMENT_PROCESSING_IN_PROGRESS"
    CROSS_KNOWLEDGE_BASE_MOVE_FORBIDDEN = "CROSS_KNOWLEDGE_BASE_MOVE_FORBIDDEN"
    PENDING_ACTION_EXPIRED = "PENDING_ACTION_EXPIRED"
    PENDING_ACTION_RESPONSE_INVALID = "PENDING_ACTION_RESPONSE_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERRORS = {
    ErrorCode.ATTACHMENT_UNAVAILABLE: (409, "附件已过期、已使用或正在清理，请重新上传附件。"),
    ErrorCode.PENDING_ACTION_RESPONSE_INVALID: (422, "确认决策或候选目标无效。"),
    ErrorCode.PENDING_ACTION_EXPIRED: (409, "确认已过期，请重新发起。"),
    ErrorCode.CROSS_KNOWLEDGE_BASE_MOVE_FORBIDDEN: (422, "文档和文件夹只能在同一知识库内移动。"),
    ErrorCode.DOCUMENT_PROCESSING_IN_PROGRESS: (409, "文档仍有未完成的处理代次，请等待处理结束。"),
    ErrorCode.PENDING_ACTION_RESOLVED: (409, "此确认已结束，请刷新状态。"),
    ErrorCode.PENDING_ACTION_BINDING_CHANGED: (409, "目标或权限已变化，请重新发起确认。"),
    ErrorCode.PROCESSING_NOT_RETRYABLE: (409, "当前处理状态不支持覆盖或重试。"),
    ErrorCode.FOLDER_CYCLE: (409, "文件夹不能移动到自身或后代中。"),
    ErrorCode.RECYCLE_STATE_CONFLICT: (409, "资源状态不支持本次回收站操作。"),
    ErrorCode.RUN_ALREADY_ACTIVE: (409, "当前会话仍有正在执行的任务，请等待完成或先停止。"),
    ErrorCode.RUN_NOT_STOPPABLE: (409, "任务已结束，不能停止。"),
    ErrorCode.RUN_NOT_RESUMABLE: (409, "任务状态或检查点不支持继续。"),
    ErrorCode.RUN_LIMIT_REACHED: (409, "任务达到当前配置的处理限制。"),
    ErrorCode.MODEL_PROVIDER_NOT_CONFIGURED: (503, "模型尚未配置，请完成模型配置后发起新任务。"),
    ErrorCode.MODEL_PROVIDER_FAILED: (502, "模型服务暂时无法完成请求。"),
    ErrorCode.MODEL_PLANNING_TIMEOUT: (504, "模型规划超过时间限制，请稍后重试或简化问题。"),
    ErrorCode.MODEL_RESPONSE_TIMEOUT: (504, "模型响应超时，请稍后重试或简化问题。"),
    ErrorCode.MODEL_PROVIDER_RATE_LIMITED: (503, "模型服务当前繁忙或达到调用限制，请稍后重试。"),
    ErrorCode.ANSWER_SOURCE_INVALID: (502, "回答未能正确引用本轮来源，请重试。"),
    ErrorCode.TOOL_EXECUTION_FAILED: (502, "所需工具暂时无法完成请求。"),
    ErrorCode.SOURCE_UNAVAILABLE: (410, "当前来源不可用。"),
    ErrorCode.DATABASE_SOURCE_ALREADY_CONFIGURED: (409, "已配置单一企业数据源，请编辑已有配置。"),
    ErrorCode.DATABASE_SOURCE_UNAVAILABLE: (503, "企业数据源暂不可用，请联系管理员检查配置。"),
    ErrorCode.DATABASE_CONNECTION_AUTH_FAILED: (
        503,
        "数据库认证失败，请检查只读账号和 Secret 引用。",
    ),
    ErrorCode.DATABASE_CONNECTION_NETWORK_FAILED: (
        503,
        "数据库连接或 SSL 检查失败，请检查网络与连接参数。",
    ),
    ErrorCode.DATABASE_CONNECTION_PERMISSION_FAILED: (
        503,
        "数据库账号权限不符合只读访问要求，请检查账号角色与对象权限。",
    ),
    ErrorCode.SCHEMA_SNAPSHOT_UNAVAILABLE: (503, "没有可用的数据库结构快照，请管理员刷新。"),
    ErrorCode.DATABASE_RELATION_FORBIDDEN: (403, "当前权限不允许此数据库查询。"),
    ErrorCode.DATABASE_QUERY_REJECTED: (422, "查询不符合安全规则，请明确条件或缩小范围。"),
    ErrorCode.DATABASE_QUERY_TIMEOUT: (504, "数据库查询超时，请缩小范围或使用聚合口径。"),
    ErrorCode.QUERY_RESULT_TOO_LARGE: (
        422,
        "查询结果页超出上下文预算，请缩小字段范围或减少每页行数。",
    ),
    ErrorCode.QUERY_CONTEXT_EXPIRED: (409, "数据库查询上下文已过期或已变化，请重新发起查询。"),
    ErrorCode.INVALID_REQUEST: (400, "请求无法解析。"),
    ErrorCode.AUTH_REQUIRED: (401, "请重新登录。"),
    ErrorCode.AUTH_INVALID_CREDENTIALS: (401, "用户名或密码错误。"),
    ErrorCode.ACCOUNT_DISABLED: (403, "当前账号已停用。"),
    ErrorCode.DEPARTMENT_DISABLED: (403, "当前所属部门已停用。"),
    ErrorCode.PERMISSION_DENIED: (403, "当前角色不能执行此操作。"),
    ErrorCode.RESOURCE_NOT_FOUND: (404, "资源不存在或不可访问。"),
    ErrorCode.VALIDATION_ERROR: (422, "请求字段不符合要求。"),
    ErrorCode.ROW_VERSION_CONFLICT: (409, "资源已被修改，请刷新后重试。"),
    ErrorCode.NAME_CONFLICT: (409, "该范围内已存在同名资源。"),
    ErrorCode.DEPARTMENT_IN_USE: (409, "部门仍被用户或知识库引用。"),
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: (409, "幂等键已用于其他操作或不同请求，请使用新键。"),
    ErrorCode.INTERNAL_ERROR: (500, "服务暂时无法处理请求。"),
    ErrorCode.FILE_TOO_LARGE: (413, "文件超过允许大小。"),
    ErrorCode.UNSUPPORTED_FILE_TYPE: (415, "当前处理阶段不支持此文件格式。"),
    ErrorCode.FILE_CONTENT_MISMATCH: (422, "文件内容与声明格式不一致。"),
    ErrorCode.FILE_GENERATION_TIMEOUT: (504, "文件生成超过时间预算，请缩小内容后重试。"),
    ErrorCode.DOCUMENT_NAME_CONFLICT: (409, "目录中存在同名文档，请改名或发起覆盖确认。"),
    ErrorCode.STORAGE_UNAVAILABLE: (503, "文件存储暂时不可用。"),
}


class APIError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        details: dict[str, Any] | None = None,
        *,
        http_status: int | None = None,
    ) -> None:
        self.http_status = http_status
        self.code = code
        self.details = details or {}
        super().__init__(code)


def error_response(request: Request, error: APIError) -> JSONResponse:
    status, message = ERRORS[error.code]
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=error.http_status or status,
        content={
            "error": {
                "code": error.code,
                "message": message,
                "details": error.details,
                "request_id": request.state.request_id,
            }
        },
        headers=headers,
    )


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)
    return error_response(request, exc)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    code = (
        ErrorCode.INVALID_REQUEST
        if any(error["type"] == "json_invalid" for error in errors)
        else ErrorCode.VALIDATION_ERROR
    )
    # Never serialize Pydantic's input/context/message (including submitted passwords).
    fields = sorted(
        {
            str(error["loc"][-1])
            if error["loc"] and error["loc"][-1] in ("username", "password")
            else "request"
            for error in errors
        }
    )
    return error_response(request, APIError(code, {"fields": fields}))


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    code = ErrorCode.RESOURCE_NOT_FOUND if exc.status_code == 404 else ErrorCode.INVALID_REQUEST
    response = error_response(request, APIError(code))
    if exc.status_code == 405:
        response.status_code = 405
        if exc.headers and "Allow" in exc.headers:
            response.headers["Allow"] = exc.headers["Allow"]
    return response
