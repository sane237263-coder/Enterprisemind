from itertools import product

import pytest

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.knowledge.models import KnowledgeBase


@pytest.mark.parametrize(
    "role, kind, department_id, deleted",
    list(product(["admin", "user"], ["public", "department"], [1, 2], [False, True])),
)
def test_content_permissions_follow_base_and_current_identity(role, kind, department_id, deleted):
    identity = CurrentIdentity(
        User(id=1, role=role, department_id=1, status="active"), Department(id=1, status="active")
    )
    resource = KnowledgeBase(
        type=kind, department_id=department_id if kind == "department" else None
    )
    if deleted:
        from datetime import UTC, datetime

        resource.deleted_at = datetime.now(UTC)
    assert AuthorizationService.can_manage_content(identity, resource) is (
        not deleted and (role == "admin" or kind == "public" or department_id == 1)
    )


@pytest.mark.parametrize("role", ["admin", "user"])
def test_personal_resources_do_not_allow_admin_to_impersonate_owner(role):
    identity = CurrentIdentity(
        User(id=1, role=role, department_id=1, status="active"), Department(id=1, status="active")
    )
    AuthorizationService.require_owner(identity, 1)
    with pytest.raises(APIError) as error:
        AuthorizationService.require_owner(identity, 2)
    assert error.value.code == ErrorCode.RESOURCE_NOT_FOUND
