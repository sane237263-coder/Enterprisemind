from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity, state_error
from enterprise_mind.knowledge.models import KnowledgeBase


class AuthorizationService:
    @staticmethod
    def require_active(identity: CurrentIdentity) -> None:
        if error := state_error(identity):
            raise APIError(error)

    @classmethod
    def require_admin(cls, identity: CurrentIdentity) -> None:
        cls.require_active(identity)
        if identity.user.role != "admin":
            raise APIError(ErrorCode.PERMISSION_DENIED)

    @classmethod
    def knowledge_scope(cls, identity: CurrentIdentity) -> ColumnElement[bool]:
        cls.require_active(identity)
        active = KnowledgeBase.deleted_at.is_(None)
        if identity.user.role == "admin":
            return active
        return and_(
            active,
            or_(
                KnowledgeBase.type == "public",
                KnowledgeBase.department_id == identity.user.department_id,
            ),
        )

    @classmethod
    def can_manage_content(cls, identity: CurrentIdentity, knowledge_base: KnowledgeBase) -> bool:
        cls.require_active(identity)
        return knowledge_base.deleted_at is None and (
            identity.user.role == "admin"
            or knowledge_base.type == "public"
            or knowledge_base.department_id == identity.user.department_id
        )

    @classmethod
    def require_owner(cls, identity: CurrentIdentity, owner_user_id: int) -> None:
        cls.require_active(identity)
        if identity.user.id != owner_user_id:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)

    @staticmethod
    async def revalidate(session: AsyncSession, identity: CurrentIdentity) -> CurrentIdentity:
        user = (
            await session.scalars(
                select(User)
                .where(User.id == identity.user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if user is None or (
            identity.authenticated_credential_version is not None
            and user.credential_version != identity.authenticated_credential_version
        ):
            raise APIError(ErrorCode.AUTH_REQUIRED)
        department = (
            await session.get(Department, user.department_id, populate_existing=True)
            if user.department_id is not None
            else None
        )
        current = CurrentIdentity(user, department, identity.authenticated_credential_version)
        AuthorizationService.require_active(current)
        return current
