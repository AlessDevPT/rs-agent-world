"""Workspaces and their members.

Every method begins with `authorize`, which is also what produces the 404 for
a workspace the caller cannot see - so "not a member" and "does not exist"
leave by the same door without each route having to remember to make them.

Three rules live here rather than in the policy table, because they are about
the state of a particular workspace rather than about roles:

- An admin may not demote the owner.
- Promoting someone to owner is a *transfer*: the acting owner steps down in
  the same transaction. The partial unique index in migration `0003` would
  reject anything else, so this is the shape the database already requires.
- The owner cannot be removed. A workspace with no owner cannot be deleted by
  anyone, which would make it permanent by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from agentworld.domain import slugs
from agentworld.domain.entities import (
    MembershipView,
    Workspace,
    WorkspaceMembership,
)
from agentworld.domain.enums import MemberRole
from agentworld.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext


class WorkspaceService:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    # ----------------------------------------------------------- workspaces ---

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        *,
        name: str,
        slug: str | None = None,
    ) -> Workspace:
        """Create a workspace with the caller as its owner.

        No `authorize` call: any authenticated user may create a workspace,
        and there is no existing resource to check them against.

        The id is generated here, before the transaction opens, because the
        audit row for this very creation is written into a table that
        row-level security filters by workspace - so the new workspace has to
        be in the session's visibility set from the first statement, and that
        is only possible if we chose the id.
        """
        resolved = slugs.validated(slug, fallback=name)
        workspace_id = uuid4()

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            if await work.workspaces.slug_taken(resolved):
                raise ConflictError(f"The slug '{resolved}' is already in use", slug=resolved)
            workspace = await work.workspaces.create(
                slug=resolved,
                name=name,
                created_by=principal.user_id,
                workspace_id=workspace_id,
            )
            await work.memberships.add(workspace_id, principal.user_id, MemberRole.OWNER)
            await self._record(work, principal, context, workspace_id, "workspace.create")
            return workspace

    async def list_for(self, principal: UserPrincipal) -> list[WorkspaceMembership]:
        """Every workspace the caller belongs to, with their role in each."""
        async with self._factory.begin() as work:
            return await work.memberships.workspaces_for(principal.user_id)

    async def get(self, principal: UserPrincipal, workspace_id: UUID) -> Workspace:
        authorize(principal, Action.WORKSPACE_READ, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            workspace = await work.workspaces.get(workspace_id)
            if workspace is None:
                raise NotFoundError("Workspace not found", workspace_id=str(workspace_id))
            return workspace

    async def update(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        *,
        name: str | None = None,
        slug: str | None = None,
    ) -> Workspace:
        authorize(principal, Action.WORKSPACE_UPDATE, Resource(workspace_id))
        resolved = slugs.validated(slug) if slug is not None else None

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            if resolved is not None and await work.workspaces.slug_taken(resolved):
                current = await work.workspaces.get(workspace_id)
                # Renaming a workspace to the slug it already has is a no-op,
                # not a conflict. `citext` means the comparison is
                # case-insensitive, so changing only the capitalisation lands
                # here too and is likewise allowed through.
                if current is None or current.slug.lower() != resolved.lower():
                    raise ConflictError(f"The slug '{resolved}' is already in use", slug=resolved)
            workspace = await work.workspaces.update(workspace_id, name=name, slug=resolved)
            if workspace is None:
                raise NotFoundError("Workspace not found", workspace_id=str(workspace_id))
            await self._record(work, principal, context, workspace_id, "workspace.update")
            return workspace

    async def archive(
        self, principal: UserPrincipal, context: CallContext, workspace_id: UUID
    ) -> None:
        authorize(principal, Action.WORKSPACE_DELETE, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            # Audited before the archive, not after: once `archived_at` is set
            # the workspace is invisible to `workspaces.get`, and the audit
            # row still has to be written inside this transaction.
            await self._record(work, principal, context, workspace_id, "workspace.delete")
            if not await work.workspaces.archive(workspace_id):
                raise NotFoundError("Workspace not found", workspace_id=str(workspace_id))

    # -------------------------------------------------------------- members ---

    async def list_members(
        self, principal: UserPrincipal, workspace_id: UUID
    ) -> Sequence[MembershipView]:
        authorize(principal, Action.MEMBER_READ, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            return await work.members(TenantScope(workspace_id)).list_members()

    async def add_member(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        *,
        email: str,
        role: MemberRole,
    ) -> MembershipView:
        """Add an existing user to the workspace by email address.

        A caller may not grant `owner` here. Ownership is singular and moves
        only through `set_role`, which performs the transfer as one
        transaction; an `add_member` that could create an owner would either
        produce a second one or fail on the unique index, depending on timing.

        **Limitation, stated rather than hidden:** this adds a user who has
        already signed up. Inviting someone who has not yet got an account
        needs a pending-invitation table and an email path, neither of which
        exists yet - see the note in `docs/05-ROADMAP.md`. Until then an
        unknown address is a 404 rather than a silently stored invitation that
        no one will ever receive.
        """
        actor_role = authorize(principal, Action.MEMBER_INVITE, Resource(workspace_id))
        if role is MemberRole.OWNER:
            raise ValidationFailedError(
                "Ownership is transferred, not granted; use a role change instead",
                role=role.value,
            )
        if role.rank > actor_role.rank:
            # Otherwise an admin could mint another admin with rights they do
            # not themselves hold, which is privilege escalation by proxy.
            raise ValidationFailedError(
                f"You cannot grant the '{role.value}' role, which outranks your own",
                role=role.value,
                actor_role=actor_role.value,
            )

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            user = await work.users.find_by_email(email)
            if user is None:
                raise NotFoundError(
                    "No user with that email address has signed up yet", email=email
                )
            if await work.memberships.get(workspace_id, user.id) is not None:
                raise ConflictError("That user is already a member", email=email)

            membership = await work.memberships.add(
                workspace_id, user.id, role, invited_by=principal.user_id
            )
            await self._record(
                work,
                principal,
                context,
                workspace_id,
                "member.add",
                resource_type="user",
                resource_id=user.id,
                metadata={"role": role.value},
            )
            return MembershipView(membership=membership, user=user)

    async def set_member_role(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        user_id: UUID,
        *,
        role: MemberRole,
    ) -> MembershipView:
        actor_role = authorize(
            principal, Action.MEMBER_UPDATE_ROLE, Resource(workspace_id, subject_user_id=user_id)
        )

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            target = await work.memberships.get(workspace_id, user_id)
            if target is None:
                raise NotFoundError("That user is not a member of this workspace")

            if target.role is MemberRole.OWNER and actor_role is not MemberRole.OWNER:
                raise ConflictError(
                    "Only the owner can change the owner's role; ask them to transfer ownership"
                )
            if role is MemberRole.OWNER:
                if actor_role is not MemberRole.OWNER:
                    raise ConflictError("Only the current owner can transfer ownership")
                if user_id == principal.user_id:
                    raise ConflictError("You are already the owner")
                # Order matters: the partial unique index permits exactly one
                # owner per workspace, so the outgoing owner steps down first.
                # Both statements are in this transaction, so there is no
                # moment another request could observe two owners or none.
                await work.memberships.set_role(workspace_id, principal.user_id, MemberRole.ADMIN)
            elif target.role is MemberRole.OWNER:
                raise ConflictError(
                    "The owner cannot be demoted directly; transfer ownership instead"
                )
            elif role.rank > actor_role.rank:
                raise ValidationFailedError(
                    f"You cannot grant the '{role.value}' role, which outranks your own",
                    role=role.value,
                    actor_role=actor_role.value,
                )

            membership = await work.memberships.set_role(workspace_id, user_id, role)
            if membership is None:  # pragma: no cover - the read above found it
                raise NotFoundError("That user is not a member of this workspace")
            user = await work.users.get(user_id)
            if user is None:  # pragma: no cover - a membership implies a user row
                raise NotFoundError("That user no longer exists")

            await self._record(
                work,
                principal,
                context,
                workspace_id,
                "member.set_role",
                resource_type="user",
                resource_id=user_id,
                metadata={"role": role.value, "previous_role": target.role.value},
            )
            return MembershipView(membership=membership, user=user)

    async def remove_member(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        user_id: UUID,
    ) -> None:
        """Remove a member, or leave the workspace yourself.

        `MEMBER_REMOVE` is in `SELF_SERVICE_ACTIONS`, so a viewer can leave
        without needing an admin to evict them.
        """
        authorize(principal, Action.MEMBER_REMOVE, Resource(workspace_id, subject_user_id=user_id))

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            target = await work.memberships.get(workspace_id, user_id)
            if target is None:
                raise NotFoundError("That user is not a member of this workspace")
            if target.role is MemberRole.OWNER:
                # A workspace with no owner can never be deleted, since
                # `workspace.delete` requires the owner role. That is a
                # resource nobody can remove, created by accident.
                raise ConflictError(
                    "The owner cannot leave or be removed; transfer ownership first"
                )

            await self._record(
                work,
                principal,
                context,
                workspace_id,
                "member.remove",
                resource_type="user",
                resource_id=user_id,
                metadata={"role": target.role.value},
            )
            await work.memberships.remove(workspace_id, user_id)

    # --------------------------------------------------------------- helpers ---

    @staticmethod
    async def _record(
        work: UnitOfWork,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        action: str,
        *,
        resource_type: str = "workspace",
        resource_id: UUID | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Thin wrapper over `services.audit.record` that defaults the
        resource type to the workspace, which most actions here act on."""
        await audit.record(
            work,
            principal,
            context,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )
