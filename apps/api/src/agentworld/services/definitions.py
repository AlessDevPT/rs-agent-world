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
from agentworld.domain.errors import (
    ConflictError,
    DelegationNotCompilableError,
    NotFoundError,
    ValidationFailedError,
)
from agentworld.domain.orchestration import (
    AgentFacts,
    AgentIdentity,
    DelegationFacts,
    EdgeDefinition,
    StepDefinition,
    ValidationReport,
    WorkflowDefinition,
    compile_delegation,
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

    async def compile_delegation(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        connection_id: UUID,
    ) -> tuple[Workflow, WorkflowDraft]:
        """Turn a delegate connection into an ordinary unpublished draft.

        `03-API-AND-FRONTEND.md` §7.1: a team is a `workflow_version`, not an
        entity and not an execution path. So this method ends where `create`
        ends - a workflow and its first draft - and everything after it is the
        existing lifecycle: `validate`, `publish`, `run`, the worker. Nothing
        here publishes, enables or starts anything, which is what keeps "the
        builder leaves workflows unpublished" true of this path too.

        The judgement is not here. `compile_delegation` in the domain decides
        what the graph is and whether this release supports the shape; this
        method gathers the rows, applies the authorization, and writes what
        it is told - the same division `validate` and `publish` use, for the
        same reason: a refusal has to be reproducible without a database.

        One transaction, so the connection that was read is the connection
        that was compiled. Two would let an agent be archived in between and
        produce a draft naming it.

        The eventual `services/builder.py` calls this. It gets no privileged
        path of its own: `CREATE_TEAM` will arrive here, through these two
        `authorize` calls and this audit row.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            resource = Resource(workspace_id)
            # Both, in this order. Compiling reads a standing grant and
            # creates a workflow; neither permission implies the other, and
            # a viewer who may read connections must not end up with a
            # workflow because the read was the only check.
            authorize(principal, Action.CONNECTION_READ, resource)
            authorize(principal, Action.WORKFLOW_CREATE, resource)

            scope = TenantScope(workspace_id, project_id)
            connections = work.connections(scope)
            selected = await connections.get(connection_id)
            if selected is None:
                raise NotFoundError("Connection not found", connection_id=str(connection_id))

            # Every delegation in the project, not just this one: whether a
            # pair is unambiguous is a question about the others.
            everything = await connections.list_all()
            # `list_all` excludes archived agents, which is the whole of the
            # rule - a delegation naming one is refused rather than compiled
            # into a step nothing can run. Stated here rather than filtered
            # again below: a second filter could only agree, and a reader
            # would take it for the protection when it is not.
            live = await work.agents(scope).list_all()

            compilation = compile_delegation(
                _delegation_facts(selected),
                delegations=[
                    _delegation_facts(connection)
                    for connection in everything
                    if connection.kind is ConnectionKind.DELEGATE
                ],
                agents=[
                    AgentIdentity(
                        agent_id=agent.id,
                        agent_key=agent.agent_key,
                        display_name=agent.display_name,
                    )
                    for agent in live
                ],
            )
            compiled = compilation.workflow
            if compiled is None:
                raise DelegationNotCompilableError(
                    "This delegation cannot be compiled into a workflow",
                    findings=[
                        {
                            "pointer": finding.pointer,
                            "code": finding.code.value,
                            "detail": finding.detail,
                        }
                        for finding in compilation.findings
                    ],
                    connection_id=str(connection_id),
                )

            workflows = work.workflows(scope)
            if await workflows.slug_taken(compiled.slug):
                # Compiling the same pair twice. A conflict rather than a
                # second workflow with a suffixed slug: the caller asked for
                # this delegation to become a workflow and it already is one.
                raise ConflictError(
                    f"This project already has a workflow with the slug '{compiled.slug}'",
                    slug=compiled.slug,
                )
            workflow, draft = await workflows.create(
                slug=compiled.slug,
                name=compiled.name,
                description=compiled.description,
                created_by_user_id=principal.user_id,
                graph=compiled.graph,
            )
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.compile_delegation",
                resource_type="workflow",
                resource_id=workflow.id,
                metadata={
                    "connection_id": str(connection_id),
                    "slug": compiled.slug,
                    # The step keys, because they are what a published
                    # version freezes and what every later expression names.
                    "steps": [compiled.upstream_step_key, compiled.downstream_step_key],
                },
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
            existing = await workflows.get(workflow_id)
            if existing is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            not_a_test_harness(existing, doing="edited")
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
            workflows = work.workflows(scope)
            existing = await workflows.get(workflow_id)
            if existing is not None:
                # Before the attempt, so that a refused harness is told apart
                # from a workflow that is not there. The repository refuses
                # it either way, but "not found" would be a lie.
                not_a_test_harness(existing, doing="archived")
            if not await workflows.archive(workflow_id):
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
            facts, effects, approvers = await facts_and_effects(work, scope, workspace_id)

        definition = definition_for_execution(draft, facts, effects, approvers)
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


def not_a_test_harness(workflow: Workflow, *, doing: str) -> None:
    """Refuse the ordinary workflow lifecycle verbs on a compiled harness.

    An agent's test workflow is created, published and run by `agent.test`
    and by nothing else. Editing its draft would change what the next test
    runs into something the compiler did not decide; archiving it would
    leave a row no later test can insert past, because the unique index on
    `test_for_agent_id` spans archived rows. Publishing it would freeze a
    third version behind the pointer - S4's version churn, arriving from
    outside the service S4 fixed - and running it would produce an
    execution of a workflow nobody drew, with no `agent.test` row to say
    who asked for it. All four are a 409 naming the agent, rather than a
    404 pretending the workflow is not there - it is, the caller may read
    it, and it belongs to something.

    Public, unlike the rest of this module's helpers, because the publish
    and run verbs live in `ExecutionService` and have to refuse by the same
    rule in the same words. One function and four call sites: a second
    spelling of "is this a harness" is how the two come to disagree.
    """
    if workflow.test_for_agent_id is None:
        return
    raise ConflictError(
        f"This workflow is the test harness for an agent and cannot be {doing}",
        workflow_id=str(workflow.id),
        agent_id=str(workflow.test_for_agent_id),
    )


def _delegation_facts(connection: Connection) -> DelegationFacts:
    """A connection row in the shape the pure compiler reads.

    A straight transcription, including `kind`: the compiler checks it rather
    than trusting the caller to have filtered, so "this is a grant_tool, not
    a delegation" is a finding a test can produce without a database.
    """
    return DelegationFacts(
        connection_id=connection.id,
        kind=connection.kind,
        source_kind=connection.source_kind,
        source_id=connection.source_id,
        target_kind=connection.target_kind,
        target_id=connection.target_id,
        enabled=connection.enabled,
    )


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


async def facts_and_effects(
    work: UnitOfWork,
    scope: TenantScope,
    workspace_id: UUID,
) -> tuple[Mapping[str, AgentFacts], Mapping[str, SideEffect], int]:
    """What a pure checker needs to know and cannot look up.

    Extracted so `validate` and `publish` and the execution path all ask the
    same four questions in the same order. Two callers assembling this
    separately is how a workflow validates at publish and then decides
    differently at run time, which is exactly the class of bug the pure
    functions exist to make impossible.

    Archived agents are excluded, so a step naming one produces
    `UNKNOWN_AGENT` rather than silently working - archiving an agent is a
    decision that its workflows should notice.
    """
    agents = await work.agents(scope).list_all()
    connections = work.connections(scope)
    grants = await connections.tool_grants()
    effects = await connections.tool_effects()
    approvers = await work.members(TenantScope(workspace_id)).count_at_least(MemberRole.MEMBER)
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
    return facts, effects, approvers


def definition_for_execution(
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
