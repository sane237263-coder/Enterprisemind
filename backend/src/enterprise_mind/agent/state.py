from typing import Annotated, Any, TypedDict


def merge_ids(left: list[int], right: list[int]) -> list[int]:
    if any(type(item) is not int for item in [*left, *right]):
        raise ValueError("graph identifiers must be integers")
    return sorted(set(left) | set(right))


class RunState(TypedDict, total=False):
    state_schema_version: int
    agent_run_id: int
    conversation_id: int
    actor_user_id: int
    user_message_id: int
    intent_kind: str | None
    requires_enterprise_facts: bool
    plan_steps: list[dict[str, Any]]
    current_wave: int
    successful_tool_call_ids: Annotated[list[int], merge_ids]
    failed_tool_call_ids: Annotated[list[int], merge_ids]
    knowledge_chunk_ids: list[int]
    citation_anchor_ids: list[int]
    database_replay_required: bool
    pending_action_id: int | None
    client_action_pending: bool
    decision: str
    terminal_code: str | None
    terminal_message: str | None


class ToolState(TypedDict):
    agent_run_id: int
    tool_call_id: int
    current_wave: int
