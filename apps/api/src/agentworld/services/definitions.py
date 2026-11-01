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

from agentworld.domain.entities import (
    Agent,
    Connection,
    ModelProfile,
    Workflow,
    WorkflowDraft,
)
from agentworld.domain.enums import (
    AgentInteractionMode,
    ConnectionKind,
    MemberRole,
    SideEffect,
    StepKind,
)
from agentworld.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from agentworld.domain.orchestration import (
    AgentFacts,
    EdgeDefinition,
    StepDefinition,
    ValidationReport,
    WorkflowDefinition,
)
from agentworld.domain.orchestration import validate as domain_validate
from agentworld.domain.policy import Action, Condition, Resource, authorize
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
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.AGENT_READ, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            return await work.agents(scope).list_all(include_archived=include_archived)

    async def get(self, principal: UserPrincipal, project_id: UUID, agent_id: UUID) -> Agent:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
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
        # An empty dict is not a configuration change. The request models
        # default `model_overrides` and `dispatch` to `{}`, so counting them
        # by presence made *every* create need `agent.configure` - which
        # meant a member could not create an agent at all. The role matrix
        # caught it; the rule is about what is being set, not which keys the
        # serialiser happened to emit.
        configuration = {
            name: value
            for name, value in fields.items()
            if name in CONFIGURATION_FIELDS and value not in (None, {}, [], UNSET)
        }
        key = validated_slug(agent_key, fallback=display_name)
        _check_model_mode(interaction_mode, configuration.get("model_profile_id"))

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
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
        # On update, a key the caller actually sent counts even when it is
        # empty: clearing an agent's overrides is a configuration change.
        # The asymmetry with `create` above is deliberate - there, an empty
        # dict is the serialiser's default rather than the caller's intent.
        touched_configuration = sorted(set(sent) & CONFIGURATION_FIELDS)

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
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
            workspace_id = await _workspace_of(work, project_id)
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


class ConnectionService:
    """Standing grants between components.

    The conditional elevation lives here: creating or updating a connection
    is a `member` action, unless the connection grants a tool whose effects
    reach outside AgentWorld and cannot be taken back - then it is `admin`.
    Granting a capability is at least as privileged as using it.
    """

    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def list_in(self, principal: UserPrincipal, project_id: UUID) -> Sequence[Connection]:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.CONNECTION_READ, Resource(workspace_id))
            return await work.connections(TenantScope(workspace_id, project_id)).list_all()

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        *,
        kind: ConnectionKind,
        source_kind: str,
        source_id: UUID,
        target_kind: str,
        target_id: UUID,
        config: Mapping[str, Any] | None = None,
        requires_approval: bool = False,
        enabled: bool = True,
    ) -> Connection:
        conditions = _elevation_for(kind, config)

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(
                principal,
                Action.CONNECTION_CREATE,
                Resource(workspace_id, conditions=conditions),
            )
            scope = TenantScope(workspace_id, project_id)
            connection = await work.connections(scope).create(
                kind=kind,
                source_kind=source_kind,
                source_id=source_id,
                target_kind=target_kind,
                target_id=target_id,
                config=config,
                requires_approval=requires_approval,
                enabled=enabled,
                created_by_user_id=principal.user_id,
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="connection.create",
                resource_type="connection",
                resource_id=connection.id,
                metadata={
                    "kind": kind.value,
                    "source": f"{source_kind}:{source_id}",
                    "target": f"{target_kind}:{target_id}",
                    "consequential": bool(conditions),
                },
            )
            return connection

    async def delete(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        connection_id: UUID,
    ) -> None:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.CONNECTION_DELETE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            if not await work.connections(scope).delete(connection_id):
                raise NotFoundError("Connection not found", connection_id=str(connection_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="connection.delete",
                resource_type="connection",
                resource_id=connection_id,
            )


class WorkflowService:
    """Workflows, their drafts, and whether a draft could run.

    `validate` is the method the product leans on. It assembles what the pure
    checker needs - the draft's graph, the project's agents, which tools each
    one can actually reach, and what those tools do - and hands it over. The
    assembly is here because it needs the database; the judgement is in
    `domain/orchestration/validate.py` because it must not.
    """

    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self._factory = factory

    async def list_in(self, principal: UserPrincipal, project_id: UUID) -> Sequence[Workflow]:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_READ, Resource(workspace_id))
            return await work.workflows(TenantScope(workspace_id, project_id)).list_all()

    async def create(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        *,
        name: str,
        slug: str | None = None,
        description: str | None = None,
        graph: Mapping[str, Any] | None = None,
    ) -> tuple[Workflow, WorkflowDraft]:
        resolved = validated_slug(slug, fallback=name)

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_CREATE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflows = work.workflows(scope)
            if await workflows.slug_taken(resolved):
                raise ConflictError(
                    f"This project already has a workflow with the slug '{resolved}'",
                    slug=resolved,
                )
            workflow, draft = await workflows.create(
                slug=resolved,
                name=name,
                description=description,
                created_by_user_id=principal.user_id,
                graph=graph,
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.create",
                resource_type="workflow",
                resource_id=workflow.id,
                metadata={"slug": resolved},
            )
            return workflow, draft

    async def get_draft(
        self, principal: UserPrincipal, project_id: UUID, workflow_id: UUID
    ) -> tuple[Workflow, WorkflowDraft]:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_READ, Resource(workspace_id))
            return await self._draft(work, TenantScope(workspace_id, project_id), workflow_id)

    async def save_draft(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        workflow_id: UUID,
        *,
        graph: Mapping[str, Any],
    ) -> WorkflowDraft:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_UPDATE_DRAFT, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflows = work.workflows(scope)
            if await workflows.get(workflow_id) is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            draft = await workflows.save_draft(workflow_id, graph=graph)
            if draft is None:
                raise NotFoundError(
                    "This workflow has no draft to edit", workflow_id=str(workflow_id)
                )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.update_draft",
                resource_type="workflow",
                resource_id=workflow_id,
                metadata={"version": draft.version},
            )
            return draft

    async def archive(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        workflow_id: UUID,
    ) -> None:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_ARCHIVE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            if not await work.workflows(scope).archive(workflow_id):
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.archive",
                resource_type="workflow",
                resource_id=workflow_id,
            )

    async def validate(
        self, principal: UserPrincipal, project_id: UUID, workflow_id: UUID
    ) -> ValidationReport:
        """Could this draft run? Structured findings, never a boolean.

        Everything the checker needs is gathered here, under one transaction
        and one tenant scope, and then handed to a pure function. Nothing in
        this method decides anything: if a finding is wrong, it is wrong in
        `domain/orchestration/validate.py`, where it can be reproduced
        without a database.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_VALIDATE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)

            _, draft = await self._draft(work, scope, workflow_id)
            agents = await work.agents(scope).list_all()
            connections = work.connections(scope)
            grants = await connections.tool_grants()
            effects = await connections.tool_effects()
            approvers = await work.members(TenantScope(workspace_id)).count_at_least(
                MemberRole.MEMBER
            )

        facts = {
            agent.agent_key: AgentFacts(
                agent_key=agent.agent_key,
                interaction_mode=agent.interaction_mode,
                display_name=agent.display_name,
                granted_tools=grants.get(agent.id, frozenset()),
            )
            for agent in agents
            if not agent.is_archived
        }
        definition = _definition_from(draft, facts, effects, approvers)
        return domain_validate(definition)

    @staticmethod
    async def _draft(
        work: UnitOfWork, scope: TenantScope, workflow_id: UUID
    ) -> tuple[Workflow, WorkflowDraft]:
        workflows = work.workflows(scope)
        workflow = await workflows.get(workflow_id)
        if workflow is None:
            raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
        draft = await workflows.draft_of(workflow_id)
        if draft is None:
            raise NotFoundError("This workflow has no draft", workflow_id=str(workflow_id))
        return workflow, draft


def _elevation_for(kind: ConnectionKind, config: Mapping[str, Any] | None) -> frozenset[Condition]:
    """Whether this connection grants something that cannot be taken back.

    Read from the grant's own `side_effect`, which is where a tool's class
    lives until Phase 6 gives tools a table. An unrecognised value is treated
    as consequential rather than ignored: the failure mode of guessing low is
    a member granting an agent the ability to send email.
    """
    if kind is not ConnectionKind.GRANT_TOOL:
        return frozenset()
    raw = (config or {}).get("side_effect")
    if raw is None:
        return frozenset()
    try:
        effect = SideEffect(raw)
    except ValueError:
        return frozenset({Condition.GRANTS_CONSEQUENTIAL_TOOL})
    if effect is SideEffect.CONSEQUENTIAL:
        return frozenset({Condition.GRANTS_CONSEQUENTIAL_TOOL})
    return frozenset()


def _definition_from(
    draft: WorkflowDraft,
    agents: Mapping[str, AgentFacts],
    effects: Mapping[str, SideEffect],
    approvers: int,
) -> WorkflowDefinition:
    """Turn a draft's jsonb graph into the shape the checker reads.

    Total over nonsense. A graph is whatever was last saved, including
    whatever a half-finished UI or a conversational builder put there, so
    anything unreadable becomes a step or edge the checker can produce a
    finding about rather than an exception nobody can act on.
    """
    raw_steps = draft.graph.get("steps")
    raw_edges = draft.graph.get("edges")

    steps: list[StepDefinition] = []
    for index, raw in enumerate(raw_steps if isinstance(raw_steps, list) else []):
        if not isinstance(raw, Mapping):
            continue
        try:
            kind = StepKind(raw.get("kind", ""))
        except ValueError:
            kind = StepKind.TRANSFORM
        mapping = raw.get("input_mapping")
        steps.append(
            StepDefinition(
                step_key=str(raw.get("step_key") or raw.get("key") or f"step-{index}"),
                kind=kind,
                agent_key=_text(raw.get("agent_key")),
                tool_name=_text(raw.get("tool_name")),
                condition=_text(raw.get("condition")),
                input_mapping={str(k): str(v) for k, v in mapping.items()}
                if isinstance(mapping, Mapping)
                else {},
                requires_approval=bool(raw.get("requires_approval")),
                approval_requires_different_user=bool(raw.get("approval_requires_different_user")),
            )
        )

    edges: list[EdgeDefinition] = []
    for index, raw in enumerate(raw_edges if isinstance(raw_edges, list) else []):
        if not isinstance(raw, Mapping):
            continue
        to_step = _text(raw.get("to")) or _text(raw.get("to_step"))
        if to_step is None:
            continue
        edges.append(
            EdgeDefinition(
                index=index,
                to_step=to_step,
                from_step=_text(raw.get("from")) or _text(raw.get("from_step")),
                condition=_text(raw.get("condition")),
            )
        )

    return WorkflowDefinition(
        slug=str(draft.graph.get("slug") or ""),
        steps=tuple(steps),
        edges=tuple(edges),
        agents=agents,
        tool_effects=effects,
        eligible_approvers=approvers,
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _workspace_of(work: UnitOfWork, project_id: UUID) -> UUID:
    """Which of the caller's workspaces holds this project.

    Works only because the session was opened over the caller's memberships:
    row-level security then confines the lookup to projects they can already
    see, so a stranger's project id has no row and 404s.
    """
    workspace_id = await work.project_lookup.workspace_of(project_id)
    if workspace_id is None:
        raise NotFoundError("Project not found", project_id=str(project_id))
    return workspace_id
