"""Agents and model profiles: the control plane for what an ecosystem *is*.

The interesting part of this module is one rule, applied in `AgentService`:

> A request that touches the model profile, its overrides or dispatch needs
> `agent.configure` **in addition to** `agent.update`.

`agent.update` is `member` and covers everything a builder edits - name,
role, description, instructions, labels. `agent.configure` is `admin` and
covers the narrow remainder that spends money and reaches outside. One
endpoint serves both, because splitting the HTTP surface would make the UI
decide which call to send and get it wrong; the authorization is split
instead, which is the thing that actually has to be right.

Both checks run before anything is written, so a request that may change the
name and may not change the model profile changes neither. Partially
applying it would be worse than refusing: the caller would have to diff the
response to discover which half landed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from agentworld.domain.entities import Agent, ModelProfile
from agentworld.domain.enums import AgentInteractionMode
from agentworld.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.domain.slugs import validated as validated_slug
from agentworld.repositories.base import TenantScope
from agentworld.repositories.definitions import UNSET
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext

#: The fields `agent.configure` guards. Named once, here, so the check and
#: the audit metadata cannot disagree about what counts as configuration.
CONFIGURATION_FIELDS: frozenset[str] = frozenset(
    {"model_profile_id", "model_overrides", "dispatch", "heartbeat_ttl_seconds"}
)


class ModelProfileService:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def list_in(self, principal: UserPrincipal, workspace_id: UUID) -> Sequence[ModelProfile]:
        authorize(principal, Action.MODEL_PROFILE_READ, Resource(workspace_id))
        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            return await work.model_profiles(TenantScope(workspace_id)).list_all()

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        *,
        name: str,
        provider_slug: str,
        model_id: str,
        parameters: Mapping[str, Any] | None = None,
        credential_id: UUID | None = None,
        is_default: bool = False,
    ) -> ModelProfile:
        """`admin`, because a profile names a credential and a spend ceiling."""
        authorize(principal, Action.MODEL_PROFILE_CREATE, Resource(workspace_id))

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            scope = TenantScope(workspace_id)
            profiles = work.model_profiles(scope)
            if await profiles.name_taken(name):
                raise ConflictError(
                    f"This workspace already has a model profile called '{name}'", name=name
                )
            if is_default:
                # Before the insert: `one_default_model_profile` is a unique
                # index, so there is no moment at which two may exist.
                await profiles.clear_default()
            profile = await profiles.create(
                name=name,
                provider_slug=provider_slug,
                model_id=model_id,
                parameters=parameters,
                credential_id=credential_id,
                is_default=is_default,
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="model_profile.create",
                resource_type="model_profile",
                resource_id=profile.id,
                # The provider and model, never the credential's contents -
                # there are none to have, and the id is enough to answer
                # "which key was this wired to?" during an incident.
                metadata={"provider": provider_slug, "model": model_id},
            )
            return profile

    async def delete(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        profile_id: UUID,
    ) -> None:
        authorize(principal, Action.MODEL_PROFILE_DELETE, Resource(workspace_id))

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            scope = TenantScope(workspace_id)
            profiles = work.model_profiles(scope)
            if await profiles.get(profile_id) is None:
                raise NotFoundError("Model profile not found", profile_id=str(profile_id))
            # Refused rather than cascaded: silently un-configuring every
            # agent that used it would leave a workspace full of managed
            # agents with no model and no explanation.
            if await work.agents(scope).profile_in_use(profile_id):
                raise ConflictError(
                    "This model profile is still used by at least one agent",
                    profile_id=str(profile_id),
                )
            await profiles.delete(profile_id)
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="model_profile.delete",
                resource_type="model_profile",
                resource_id=profile_id,
            )


class AgentService:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def list_in(
        self,
        principal: UserPrincipal,
        project_id: UUID,
        *,
        include_archived: bool = False,
    ) -> Sequence[Agent]:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._workspace_of(work, project_id)
            authorize(principal, Action.AGENT_READ, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            return await work.agents(scope).list_all(include_archived=include_archived)

    async def get(self, principal: UserPrincipal, project_id: UUID, agent_id: UUID) -> Agent:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._workspace_of(work, project_id)
            authorize(principal, Action.AGENT_READ, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            agent = await work.agents(scope).get(agent_id, include_archived=True)
            if agent is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            return agent

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        *,
        display_name: str,
        interaction_mode: AgentInteractionMode,
        agent_key: str | None = None,
        **fields: Any,
    ) -> Agent:
        configuration = {
            name: value for name, value in fields.items() if name in CONFIGURATION_FIELDS
        }
        key = validated_slug(agent_key, fallback=display_name)
        _check_model_mode(interaction_mode, configuration.get("model_profile_id"))

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._workspace_of(work, project_id)
            resource = Resource(workspace_id)
            authorize(principal, Action.AGENT_CREATE, resource)
            if configuration:
                # Creating an agent *with* a model is configuring it. Letting
                # create bypass the check would make `agent.configure` a rule
                # about editing rather than about capability.
                authorize(principal, Action.AGENT_CONFIGURE, resource)

            scope = TenantScope(workspace_id, project_id)
            agents = work.agents(scope)
            if await agents.key_taken(key):
                raise ConflictError(
                    f"This project already has an agent with the key '{key}'", agent_key=key
                )
            agent = await agents.create(
                agent_key=key,
                display_name=display_name,
                interaction_mode=interaction_mode,
                created_by_user_id=principal.user_id,
                **fields,
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="agent.create",
                resource_type="agent",
                resource_id=agent.id,
                metadata={
                    "agent_key": key,
                    "interaction_mode": interaction_mode.value,
                    "configured": sorted(configuration),
                },
            )
            return agent

    async def update(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        agent_id: UUID,
        **fields: Any,
    ) -> Agent:
        """One endpoint, two permissions. See the module docstring."""
        sent = {name: value for name, value in fields.items() if value is not UNSET}
        touched_configuration = sorted(set(sent) & CONFIGURATION_FIELDS)

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._workspace_of(work, project_id)
            resource = Resource(workspace_id)
            authorize(principal, Action.AGENT_UPDATE, resource)
            if touched_configuration:
                # Both checks before any write: a request that may change the
                # name and may not change the model profile changes neither.
                authorize(principal, Action.AGENT_CONFIGURE, resource)

            scope = TenantScope(workspace_id, project_id)
            agents = work.agents(scope)
            existing = await agents.get(agent_id)
            if existing is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))

            mode = sent.get("interaction_mode", existing.interaction_mode)
            profile = sent.get("model_profile_id", existing.model_profile_id)
            _check_model_mode(mode, profile)

            agent = await agents.update(agent_id, **sent)
            if agent is None:  # pragma: no cover - the row was read above
                raise NotFoundError("Agent not found", agent_id=str(agent_id))

            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="agent.update",
                resource_type="agent",
                resource_id=agent_id,
                # Field *names*, not values: an instruction can be long and
                # an override can name a model, and the audit log answers
                # "who changed what" rather than "what did it say".
                metadata={
                    "fields": sorted(sent),
                    "configuration": touched_configuration,
                },
            )
            return agent

    async def archive(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        agent_id: UUID,
    ) -> None:
        """Archive, never delete.

        An archived agent keeps its runs, its history and its place on the
        island, where it renders shuttered. Deleting it would rewrite what
        happened, and the island would empty itself exactly when a user most
        wants to look at it.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await self._workspace_of(work, project_id)
            authorize(principal, Action.AGENT_ARCHIVE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            if not await work.agents(scope).archive(agent_id):
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="agent.archive",
                resource_type="agent",
                resource_id=agent_id,
            )

    @staticmethod
    async def _workspace_of(work: UnitOfWork, project_id: UUID) -> UUID:
        """Resolve the project's workspace before authorizing anything.

        The same shape as `ProjectService`, and it works only because every
        caller above opens the session over the caller's own memberships.
        Row-level security then confines this lookup to projects they can
        already see, so a project id belonging to a stranger has no row and
        404s without any check having to notice. Opened with no visibility
        set - as the first draft of this did - it would see nothing and 404
        on everything, which is a failure mode that looks exactly like
        correct behaviour until someone tries their own project.
        """
        workspace_id = await work.project_lookup.workspace_of(project_id)
        if workspace_id is None:
            raise NotFoundError("Project not found", project_id=str(project_id))
        return workspace_id


def _check_model_mode(mode: AgentInteractionMode, model_profile_id: UUID | None) -> None:
    """Only a managed agent has a model.

    The database says the same thing with a check constraint. Saying it here
    too turns an integrity error into a sentence a beginner can act on, and
    the constraint remains the thing that makes it true.
    """
    if model_profile_id is not None and mode is not AgentInteractionMode.MANAGED:
        raise ValidationFailedError(
            "Only a managed agent has a model: AgentWorld does not call one for an "
            "agent it merely watches or hands work to",
            field="model_profile_id",
            interaction_mode=mode.value,
        )
