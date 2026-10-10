import json

import pytest
from pydantic import ValidationError

from enterprise_mind.agent.client_actions import ACTION_PART
from enterprise_mind.agent.management_tools import SCHEMAS
from enterprise_mind.agent.schemas import Plan


@pytest.mark.parametrize("injected", ["password", "new_password", "row_version", "role_override"])
def test_admin_tool_cannot_accept_credentials_or_server_version(injected):
    with pytest.raises(ValidationError):
        SCHEMAS["user_update"].model_validate(
            {
                "target": {"id": "1"},
                "changes": {"status": "disabled", injected: "untrusted"},
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "open_url", "url": "https://example.invalid"},
        {"action": "open_admin_user_create", "prefill": {"password": "not-allowed"}},
        {"action": "open_admin_user_create", "route": "/arbitrary"},
        {"action": "open_admin_user_password_reset", "target": {"id": "1", "row_version": 2}},
        {"action": "open_admin_user_password_reset", "target": {}},
    ],
)
@pytest.mark.parametrize("encoded", [False, True])
def test_client_action_rejects_uncontrolled_destinations_credentials_and_versions(payload, encoded):
    with pytest.raises(ValidationError):
        Plan(
            intent_kind="general",
            requires_enterprise_facts=False,
            client_action=json.dumps(payload) if encoded else payload,
        )


def test_provider_encoded_navigation_is_decoded_once_and_validated():
    action = {"action": "open_admin_user_create", "prefill": {"username": "验收同事"}}
    plan = Plan.model_validate(
        {
            "intent_kind": "general",
            "requires_enterprise_facts": False,
            "client_action": json.dumps(action),
        }
    )
    assert plan.client_action.action == action["action"]
    assert plan.client_action.prefill.username == "验收同事"
    for invalid in ["broken-json", json.dumps(json.dumps(action)), " " * 4001, "[]"]:
        with pytest.raises(ValidationError):
            Plan(intent_kind="general", requires_enterprise_facts=False, client_action=invalid)


def test_navigation_cannot_mix_with_mutation_or_enterprise_answer():
    with pytest.raises(ValidationError):
        Plan(
            intent_kind="general",
            requires_enterprise_facts=False,
            client_action={"action": "open_admin_user_create"},
            tools=[
                {
                    "key": "write",
                    "tool_name": "department_create",
                    "arguments": {"name": "x"},
                }
            ],
        )
    with pytest.raises(ValidationError):
        Plan(
            intent_kind="knowledge",
            requires_enterprise_facts=True,
            client_action={"action": "open_admin_user_create"},
        )
    with pytest.raises(ValidationError):
        ACTION_PART.validate_python(
            {
                "type": "client_action",
                "action": "open_admin_user_password_reset",
                "user_id": "1",
                "display_name": "user",
                "row_version": 1,
                "url": "https://example.invalid",
            }
        )
