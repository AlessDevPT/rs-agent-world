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

`ModelProfileService` has a rule of its own, and it is the same shape:

> A profile may name only a provider this build can serve, and only a
> credential issued for that same provider.

Both halves are checked by `create` and by `update`, before either writes
anything, and `update` checks them against the row the request would leave
behind rather than the fields it happens to mention. Nothing downstream
repeats either check - `DispatchService._plan` reads `provider_slug` and
`credential_id` off the one row and uses both as they stand - so without
this, a profile naming one vendor while holding another's key would send
that key to the wrong endpoint, and a slug no build serves would only be
caught by `registry.build`, which the runner reaches after a run has
started and settles as a failed step rather than a message beside a field.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from agentworld.domain.entities import (
    Agent,
    AgentStatusChange,
    Connection,
    ModelProfile,
    Run,
    RunActivity,
    Workflow,
    WorkflowDraft,
    WorkflowVersion,
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
from agentworld.domain.protocol import PROTOCOL_VERSION, parse_envelope
from agentworld.domain.slugs import validated as validated_slug
from agentworld.infra.providers import available as available_providers
from agentworld.repositories.base import TenantScope
from agentworld.repositories.credentials import CredentialRepository
from agentworld.repositories.definitions import UNSET, ModelProfileRepository
from agentworld.repositories.events import ResolvedRefs
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
        """`admin`, because a profile names a credential and a spend ceiling.

        **The provider and the credential are checked here, before the
        insert.** `update` makes the same two checks through the same two
        helpers, because a rule that held on one path and not the other
        would be a rule about which request you sent rather than about what
        a profile may be.

        The credential is resolved through this workspace's own repository
        rather than trusted - the check `update` has had since Stage B2c and
        this path did not. A bare foreign key cannot stand in for it: it
        admits any credential row in the database, including another
        tenant's.
        """
        authorize(principal, Action.MODEL_PROFILE_CREATE, Resource(workspace_id))
        _servable(provider_slug)

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            scope = TenantScope(workspace_id)
            profiles = work.model_profiles(scope)
            if await profiles.name_taken(name):
                raise ConflictError(
                    f"This workspace already has a model profile called '{name}'", name=name
                )
            if credential_id is not None:
                await _compatible(work.credentials(scope), credential_id, provider_slug)
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

    async def update(
        self,
        principal: UserPrincipal,
        context: CallContext,
        workspace_id: UUID,
        profile_id: UUID,
        **fields: Any,
    ) -> ModelProfile:
        """Re-point a profile: its name, its model, its key, its default-ness.

        `admin` and sensitive, for the reason `create` is: a profile names a
        credential and the model every managed agent using it will call.
        Changing the provider is how an operator migrates a workspace rather
        than a mistake to prevent, so it is editable - and the audit row
        names the fields, which is what makes such a migration reviewable
        afterwards.

        **The credential is resolved, never trusted.** A `credential_id` the
        caller sends is read back through this workspace's own credential
        repository, which filters on `workspace_id` and excludes deleted
        rows - so an id from another workspace, or one whose material has
        been destroyed, is a 422 naming the field rather than a pointer that
        fails at dispatch with "has no credential". A revoked credential is
        accepted, because the repository still returns one and an operator
        mid-rotation legitimately points at it; the runtime is where a
        revoked key stops working. Nothing here reads, decrypts or logs
        material: `Credential` has no field for it.

        **Provider and credential are checked against each other**, and
        against the row this request would leave behind rather than the
        fields it mentions. A `provider_slug` the caller sends is checked
        against the registry; the pairing is checked whenever either half
        moves, and skipped when neither does.

        That last clause is deliberate. A profile stored before this rule
        may name a provider no build serves, and re-checking it on every
        PATCH would make such a row unrenamable and undetachable - the two
        edits an operator needs in order to fix it. Sending either half is
        what asks for the pair to be valid.

        One transaction, so `clear_default` and the write that follows it
        cannot be separated: the partial unique index
        `one_default_model_profile` remains the backstop for two callers
        racing, and a loser gets the 409 the integrity handler already
        produces rather than a second default.
        """
        authorize(principal, Action.MODEL_PROFILE_UPDATE, Resource(workspace_id))

        sent = {name: value for name, value in fields.items() if value is not UNSET}
        _not_null(sent, "name", "provider_slug", "model_id", "parameters", "is_default")
        if "provider_slug" in sent:
            _servable(sent["provider_slug"])

        async with self._factory.begin(visible_workspaces={workspace_id}) as work:
            scope = TenantScope(workspace_id)
            profiles = work.model_profiles(scope)
            existing = await profiles.get(profile_id)
            if existing is None:
                raise NotFoundError("Model profile not found", profile_id=str(profile_id))

            name = sent.get("name")
            if name is not None and await profiles.name_taken(name, excluding=profile_id):
                # `excluding`, so renaming a profile to the name it already
                # has is not a conflict with itself.
                raise ConflictError(
                    f"This workspace already has a model profile called '{name}'", name=name
                )

            # The row this request would leave behind. The current value is
            # the default rather than the fallback for a null: an explicit
            # null is *in* `sent` and detaches the credential, and only an
            # absent field falls through to what the profile already holds.
            credential_id = sent.get("credential_id", existing.credential_id)
            provider_slug = sent.get("provider_slug", existing.provider_slug)
            moved = "credential_id" in sent or "provider_slug" in sent
            if credential_id is not None and moved:
                await _compatible(work.credentials(scope), credential_id, provider_slug)

            if sent.get("is_default"):
                # Before the write, as `create` does: the unique index means
                # there is no moment at which two may exist.
                await profiles.clear_default()

            profile = await profiles.update(profile_id, **sent)
            if profile is None:  # pragma: no cover - the row was read above
                raise NotFoundError("Model profile not found", profile_id=str(profile_id))

            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="model_profile.update",
                resource_type="model_profile",
                resource_id=profile_id,
                # Field names, and - when they changed - the provider and
                # model, which `create` also records and which are what an
                # incident asks about. Never `parameters`, whose values are
                # operator configuration, and never anything about the
                # credential beyond the fact that it was repointed.
                metadata={
                    "fields": sorted(sent),
                    **({"provider": profile.provider_slug} if "provider_slug" in sent else {}),
                    **({"model": profile.model_id} if "model_id" in sent else {}),
                },
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

    async def status_history(
        self,
        principal: UserPrincipal,
        project_id: UUID,
        agent_id: UUID,
        *,
        limit: int = 50,
    ) -> Sequence[AgentStatusChange]:
        """When this agent's status changed, and what it changed from.

        `agent.read_history`, not `agent.read_runs`: a transition is not a
        run, and the two tables have different producers - history is folded
        from what an agent says about itself, runs carry what it did. One
        action for both would mean nobody could be granted one without the
        other.

        The agent is resolved first, for its own sake. An unknown id, or one
        from another project, would otherwise come back as an empty list -
        "this agent has never changed status" - which is a different and
        wrong answer. Archived agents are included, as `get` includes them:
        this is a read, and what an agent did before it was archived is
        exactly what a reader is asking about.

        **An agent AgentWorld runs itself has no history, and that is
        correct.** Transitions are folded from ingested events, and a managed
        agent sends none - its work is in `workflow_executions`. An empty
        list here is an honest answer about where to look, and no row is
        invented to fill it.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.AGENT_READ_HISTORY, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            agents = work.agents(scope)
            if await agents.get(agent_id, include_archived=True) is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            return await agents.status_history(agent_id, limit=limit)

    async def runs(
        self,
        principal: UserPrincipal,
        project_id: UUID,
        agent_id: UUID,
        *,
        limit: int = 50,
    ) -> Sequence[Run]:
        """What this agent did, as it reported it.

        `agent.read_runs`, which has existed unused since the permission
        matrix was written and this is the first thing to ask for it. Not
        `agent.read_history`: a run is work, a transition is a status, and
        the two tables have different producers - which is the argument
        `status_history` makes in the other direction.

        The agent is resolved first, for its own sake, exactly as
        `status_history` does: an unknown id or one from another project
        would otherwise come back as an empty list - "this agent has never
        run" - which is a different and wrong answer. Archived agents are
        included, because what an agent did before it was archived is the
        question being asked.

        **An agent AgentWorld runs itself has no runs, and that is
        correct.** A run is folded from events an external agent sends about
        its own work, and a managed agent sends none - its work is in
        `workflow_executions`. An empty list here is an answer about where
        to look rather than a missing record, and the route's description
        says so.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.AGENT_READ_RUNS, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            agents = work.agents(scope)
            if await agents.get(agent_id, include_archived=True) is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            return await agents.runs(agent_id, limit=limit)

    async def quiet_runs(
        self,
        principal: UserPrincipal,
        project_id: UUID,
        *,
        quiet_for_seconds: int,
        now: dt.datetime,
        limit: int = 50,
    ) -> Sequence[RunActivity]:
        """Runs still `running` that have reported nothing for a while.

        `agent.read_runs`, the same action the per-agent listing uses: this
        is the same data asked about differently, and a reader who may see
        one agent's runs may see the project's.

        **Detection, and nothing else.** No status is written, no event is
        emitted, no audit row is kept - there is nothing to record, because
        AgentWorld never observes an external agent directly and a quiet run
        is an absence of evidence rather than a failure. Marking one failed
        would also be unrecoverable: `_SETTLED_RUNS` makes a settled status
        permanent, so the agent's own later `run.finished` would be refused.
        That is the argument for this being a read.

        `now` is passed in rather than read here, so the cut-off and every
        age in the response come from one instant. The threshold has no
        default on purpose: `agents.heartbeat_ttl_seconds` is a heartbeat
        interval rather than a patience, and where a run's belongs has not
        been decided.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.AGENT_READ_RUNS, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            quiet_before = now - dt.timedelta(seconds=quiet_for_seconds)
            return await work.runs(scope).quiet_in_project(quiet_before=quiet_before, limit=limit)

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

            # After both authorization checks and before any write: a member
            # who may not configure an agent is told that, rather than being
            # told something about this workspace's model profiles.
            if (profile_id := configuration.get("model_profile_id")) is not None:
                await _owned_profile(work.model_profiles(TenantScope(workspace_id)), profile_id)

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

            # The six columns `AgentUpdate` types as nullable and migration
            # 0007 declares `NOT NULL`. A null the caller actually sent is a
            # statement about the request body and needs no agent to judge,
            # but it is checked *after* both authorizations: a member who
            # sends `{"model_overrides": null}` is told about the role,
            # which is the thing standing between them and this request.
            #
            # Before `_check_model_mode` for a sharper reason than tidiness.
            # An explicit `interaction_mode: null` arrives there as "not
            # managed", so for an agent that already has a profile it raises
            # about `model_profile_id` - a field the request never mentioned
            # - while interpolating `mode.value` on `None`, which is an
            # `AttributeError` and a 500. The other five reach the statement
            # and return as `23502`, which is deliberately not in
            # `CONFLICT_SQLSTATES` and so is a 500 too. Two routes to the
            # same wrong answer, closed by one guard; nothing about the
            # integrity mapping changes.
            _not_null(
                sent,
                "display_name",
                "interaction_mode",
                "labels",
                "model_overrides",
                "dispatch",
                "heartbeat_ttl_seconds",
            )

            scope = TenantScope(workspace_id, project_id)
            agents = work.agents(scope)
            # `include_archived=True`, so that an archived agent is refused
            # as archived rather than reported missing. The repository's
            # `update` still excludes archived rows, so the refusal below is
            # the message and the statement is the enforcement.
            existing = await agents.get(agent_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            not_archived(existing, kind="agent", doing="edited")

            mode = sent.get("interaction_mode", existing.interaction_mode)
            profile = sent.get("model_profile_id", existing.model_profile_id)
            _check_model_mode(mode, profile)

            # Only when this request moves it. An agent whose stored profile
            # was legitimate stays editable, and re-resolving a column the
            # caller did not touch would refuse a rename because somebody
            # else deleted something.
            #
            # Here as well as in `create` because of what it buys the error
            # handler: `agents_model_profile_id_fkey` can only be violated
            # after a row this service has already resolved has gone, which
            # is what lets `CONFLICT_FOREIGN_KEYS` call that one violation a
            # conflict instead of a 500. A writer of that column that did not
            # resolve first would make the status dishonest again.
            if "model_profile_id" in sent and profile is not None:
                await _owned_profile(work.model_profiles(TenantScope(workspace_id)), profile)

            # The first definition writer. See `_reserve_definition_seq` for
            # why the reservation is here rather than beside the append, and
            # why moving it below the write is a deadlock rather than a
            # refactor.
            #
            # `if sent` is P12.2, and it is the same condition the repository
            # already applies: `AgentRepository.update` filters `UNSET` and
            # returns the row untouched when nothing is left, so an empty
            # `PATCH` writes no row. The event log follows the mutation, which
            # is why no event and no `seq` are spent on one - while the audit
            # row below is still written, because the audit log answers "who
            # asked" and this one answers "what changed".
            seq = await _reserve_definition_seq(work, scope) if sent else None

            agent = await agents.update(agent_id, **sent)
            if agent is None:  # pragma: no cover - the row was read above
                raise NotFoundError("Agent not found", agent_id=str(agent_id))

            if seq is not None:
                await _append_definition_event(
                    work,
                    scope,
                    seq=seq,
                    entity="agent",
                    entity_id=agent_id,
                    operation="updated",
                )

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

    async def restore(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        agent_id: UUID,
    ) -> Agent:
        """Bring an archived agent back. `admin`, like archiving it.

        What this fixes is not only the listing. An archived agent is absent
        from `facts_and_effects`, so every published workflow naming it
        fails at decision time with `unknown_agent`, and `agent.test`
        refuses it with `agent_archived` - a refusal whose own remedy says
        "Restore the agent, then test it", which until now named something
        no endpoint could do. Restoring makes those workflows runnable
        again; it does not run them.

        Nothing derived is written, which is the whole of decision 3: the
        agent comes back with the status and heartbeat it had, and a reader
        judges staleness from `last_heartbeat_at` exactly as they must for
        an agent that stopped reporting without being archived.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.AGENT_RESTORE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            agents = work.agents(scope)
            if not await agents.restore(agent_id):
                # Not archived, not in this project, or not there at all -
                # one 404, because "restore this" about a live agent is a
                # caller who believes something untrue either way.
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            agent = await agents.get(agent_id)
            if agent is None:  # pragma: no cover - the update returned a row
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="agent.restore",
                resource_type="agent",
                resource_id=agent_id,
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
            agents = work.agents(scope)
            existing = await agents.get(agent_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))
            already_archived(existing, kind="agent")
            if not await agents.archive(agent_id):  # pragma: no cover - read above
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


def _not_null(sent: Mapping[str, Any], *names: str) -> None:
    """Refuse an explicit `null` for a column that has none.

    An absent field means "leave it alone" and is already gone by the time
    this runs; a `null` the caller actually sent is a different request, and
    for `NOT NULL` columns it is one the database would refuse. Refused here
    instead, as a 422 naming the field, because a caller who asked to clear
    a model id deserves a sentence rather than the 409 an integrity error
    would turn into.
    """
    for name in names:
        if name in sent and sent[name] is None:
            raise ValidationFailedError(f"'{name}' cannot be null", field=name)


def _servable(provider_slug: str) -> None:
    """Refuse a provider this build has no implementation for.

    The set comes from the registry that `registry.build` consults when a
    step is dispatched, and that `GET /v1/meta` publishes - one list, so
    what a client is offered, what a profile may store and what the server
    can actually call cannot drift apart. A build that gains a provider
    accepts it with no edit here.

    Compared exactly, with no folding or normalisation. A slug that nearly
    matches is a typo, and quietly turning it into the one it resembles
    would store something the caller did not write - on the row that
    decides which vendor a workspace's key is sent to.
    """
    servable = available_providers()
    if provider_slug not in servable:
        raise ValidationFailedError(
            "This server cannot serve that model provider. It serves: "
            + ", ".join(sorted(servable)),
            field="provider_slug",
            provider_slug=provider_slug,
        )


async def _compatible(
    credentials: CredentialRepository, credential_id: UUID, provider_slug: str
) -> None:
    """Resolve the credential in this workspace, then check whose key it is.

    The order is the point. The row is read back through this workspace's
    own repository - which filters on `workspace_id` and excludes
    tombstones - *before* the two slugs are compared, so a mismatch can only
    ever be reported about a credential the caller can already list.
    Comparing first would make the error an oracle: "that key is for another
    provider" confirms a row exists, which is exactly what one `NotFoundError`
    for both causes is there to prevent.

    A revoked credential still resolves, and is still accepted, for the
    reason it always was: an operator mid-rotation legitimately points at
    one, and the runtime is where a revoked key stops working.

    Nothing here reads, decrypts or logs material. `Credential` has no field
    for it - `provider_slug` is who the key is *for*, which is metadata.
    """
    credential = await credentials.get(credential_id)
    if credential is None:
        raise ValidationFailedError(
            "This workspace has no such credential",
            field="credential_id",
            credential_id=str(credential_id),
        )
    if credential.provider_slug != provider_slug:
        raise ValidationFailedError(
            f"That credential is for '{credential.provider_slug}' and this profile "
            f"names '{provider_slug}'. A profile sends its credential to the provider "
            "it names, so the two have to agree.",
            field="credential_id",
            credential_id=str(credential_id),
        )


async def _owned_profile(profiles: ModelProfileRepository, model_profile_id: UUID) -> None:
    """Resolve the profile in this workspace before an agent may name it.

    The foreign key behind `agents.model_profile_id` names `model_profiles.id`
    and nothing else, and referential integrity checks bypass row-level
    security by design - so the database alone would accept a profile
    belonging to another workspace. Nothing downstream would then work:
    `dispatch._plan` looks the profile up under the *agent's* workspace scope,
    so the row would never resolve and every step would settle as
    `misconfigured`. A pointer that can only ever fail is not worth storing,
    and an agent in one workspace referencing another's configuration is not
    worth storing either.

    **One sentence for both causes, on purpose.** "No such profile here" is
    returned whether the id is unknown everywhere or belongs to a workspace
    the caller cannot see, exactly as `_compatible` does for credentials:
    distinguishing them would turn the error into an oracle for ids in other
    tenants, and the caller's next move - pick one from this workspace's list
    - is the same either way.

    A `ValidationFailedError` rather than a 404, because the id arrived in a
    request body as a choice among a list the caller can read: the request is
    answerable and this field of it is wrong. It carries the locator, so a
    form can put the message on the select that caused it.
    """
    if await profiles.get(model_profile_id) is None:
        raise ValidationFailedError(
            "This workspace has no such model profile",
            field="model_profile_id",
            model_profile_id=str(model_profile_id),
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

    async def update(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        connection_id: UUID,
        **fields: Any,
    ) -> Connection:
        """Change what a grant permits, how it is gated, or whether it is on.

        **Not where it points.** `kind`, `source_*` and `target_*` are
        together `connections_edge_key`, so a connection with a different
        end is a different connection - repointing one is a delete and a
        create, each with its own authorization and its own audit row.

        **The elevation holds in both directions.** `_elevation_for` is
        applied to the incoming config *and* to the stored one, and the
        higher of the two decides: granting a consequential tool is an
        `admin` act, and so is editing a grant that already is one. A member
        who could turn a consequential grant into a harmless-looking one by
        rewriting its config would be editing a capability they may not
        grant - the config is the whole of what the grant permits.

        Authorized twice, both times before any write. The first check uses
        only what the caller sent, so a caller who may not touch
        connections here learns nothing about which ones exist; the second
        runs once the row is read, and only when the stored grant raises the
        bar. `AgentService.update` checks its two permissions the same way
        and for the same reason.
        """
        sent = {name: value for name, value in fields.items() if value is not UNSET}
        incoming = _elevation_for(ConnectionKind.GRANT_TOOL, sent.get("config"))

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            resource = Resource(workspace_id, conditions=incoming)
            authorize(principal, Action.CONNECTION_UPDATE, resource)

            scope = TenantScope(workspace_id, project_id)
            connections = work.connections(scope)
            existing = await connections.get(connection_id)
            if existing is None:
                raise NotFoundError("Connection not found", connection_id=str(connection_id))

            stored = _elevation_for(existing.kind, existing.config)
            conditions = incoming | stored
            if conditions != incoming:
                authorize(
                    principal,
                    Action.CONNECTION_UPDATE,
                    Resource(workspace_id, conditions=conditions),
                )

            connection = await connections.update(connection_id, **sent)
            if connection is None:  # pragma: no cover - the row was read above
                raise NotFoundError("Connection not found", connection_id=str(connection_id))

            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="connection.update",
                resource_type="connection",
                resource_id=connection_id,
                metadata={
                    "fields": sorted(sent),
                    # True when either side of the edit is consequential,
                    # which is what the authorization used - so a reader of
                    # this row can tell why it needed an admin.
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

    async def list_in(
        self,
        principal: UserPrincipal,
        project_id: UUID,
        *,
        include_archived: bool = False,
    ) -> Sequence[Workflow]:
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_READ, Resource(workspace_id))
            return await work.workflows(TenantScope(workspace_id, project_id)).list_all(
                include_archived=include_archived
            )

    async def get(self, principal: UserPrincipal, project_id: UUID, workflow_id: UUID) -> Workflow:
        """One workflow by id, archived ones included.

        `include_archived=True`, matching `AgentService.get`: the row is
        readable, so the answer can say that it is archived rather than that
        it is missing. The lifecycle verbs keep the repository's default and
        still refuse an archived workflow with a 404 - this read is the only
        caller that asks to see one.

        A harness is readable here too, and says so. It is excluded from
        `list_in` because nobody drew it, but an execution names it, and a
        client holding that `workflow_id` has to be able to find out what it
        is holding without provoking a 409.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_READ, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflow = await work.workflows(scope).get(workflow_id, include_archived=True)
            if workflow is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            return workflow

    async def versions(
        self, principal: UserPrincipal, project_id: UUID, workflow_id: UUID
    ) -> tuple[Workflow, Sequence[WorkflowVersion]]:
        """What this workflow has published, and the workflow that names one.

        Both, from one transaction. "Is this the version a run would use" is
        a fact about `workflows.current_version_id`, and reading the pointer
        in a second transaction would let a publish land in between - after
        which the answer could mark two versions current, or none.

        The workflow is read first so that an unknown id is a 404 rather
        than an empty history, which would say "this workflow has published
        nothing" about a workflow that does not exist.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_READ, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflows = work.workflows(scope)
            workflow = await workflows.get(workflow_id, include_archived=True)
            if workflow is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            return workflow, await workflows.published_versions(workflow_id)

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

    async def update(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        workflow_id: UUID,
        **fields: Any,
    ) -> Workflow:
        """Rename or re-describe a workflow. The graph is `save_draft`'s.

        `workflow.update`, not `workflow.update_draft`: this writes the
        `workflows` row and nothing else, so no published version changes
        and no running execution's definition moves - the frozen tables are
        not reachable from here, and 0018's triggers remain the backstop if
        that ever stops being true.

        The slug is not editable and the request model has no field for it.
        It is half of `(project_id, slug)` and of a URL, and `agent_key` -
        the same kind of identity - is absent from `AgentUpdate` for the
        same reason.

        A harness is refused with the same 409 the draft editor gives: its
        name came from the compiler, and a renamed harness would be a
        workflow claiming to be something `agent.test` did not compile.
        """
        sent = {name: value for name, value in fields.items() if value is not UNSET}

        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_UPDATE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflows = work.workflows(scope)

            existing = await workflows.get(workflow_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            not_a_test_harness(existing, doing="edited")
            not_archived(existing, kind="workflow", doing="edited")

            workflow = await workflows.update(workflow_id, **sent)
            if workflow is None:  # pragma: no cover - the row was read above
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))

            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.update",
                resource_type="workflow",
                resource_id=workflow_id,
                # Field names, not values: a description can be long, and the
                # audit log answers "who changed what" rather than "what did
                # it say". `agent.update` records the same shape.
                metadata={"fields": sorted(sent)},
            )
            return workflow

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
            existing = await workflows.get(workflow_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            not_a_test_harness(existing, doing="edited")
            not_archived(existing, kind="workflow", doing="edited")
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

    async def restore(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        workflow_id: UUID,
    ) -> Workflow:
        """Bring an archived workflow back. `admin`, like archiving it.

        It returns runnable, because archiving never made it less so: the
        pointer, the published versions and the open draft were untouched,
        and only the row's visibility and its write verbs changed. Nothing
        here publishes, advances or starts anything.

        A harness is refused by name rather than by accident. One can never
        be archived, so the repository's predicate would already decline it -
        but as a 404, which would tell a client that the harness it can read
        by id is not there. Read first, refuse with the 409 the other three
        harness verbs give, and the sentence is the one already in use.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            authorize(principal, Action.WORKFLOW_RESTORE, Resource(workspace_id))
            scope = TenantScope(workspace_id, project_id)
            workflows = work.workflows(scope)

            existing = await workflows.get(workflow_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            not_a_test_harness(existing, doing="restored")

            if not await workflows.restore(workflow_id):
                # Readable, not a harness, and not archived: "restore this"
                # about a live workflow is the same 404 a second `archive`
                # of an archived one gets.
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            workflow = await workflows.get(workflow_id)
            if workflow is None:  # pragma: no cover - the update returned a row
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="workflow.restore",
                resource_type="workflow",
                resource_id=workflow_id,
            )
            return workflow

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
            existing = await workflows.get(workflow_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            # Before the attempt, so that a refused harness is told apart
            # from a workflow that is not there, and an archived one from
            # both. The repository refuses all three either way, but "not
            # found" would be a lie about two of them.
            not_a_test_harness(existing, doing="archived")
            already_archived(existing, kind="workflow")
            if not await workflows.archive(workflow_id):  # pragma: no cover - read above
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

            # Archived workflows are refused here too, although this writes
            # nothing. `validate` answers "could this draft run", and an
            # archived workflow cannot run at all - a report full of green
            # ticks would be true of the graph and misleading about the
            # workflow. The state is readable through `GET /workflows/{id}`,
            # which is where a client learns why.
            existing = await work.workflows(scope).get(workflow_id, include_archived=True)
            if existing is None:
                raise NotFoundError("Workflow not found", workflow_id=str(workflow_id))
            not_archived(existing, kind="workflow", doing="validated")

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


def not_archived(row: Agent | Workflow, *, kind: str, doing: str) -> None:
    """Refuse a write to an archived row, and say which it is.

    Until stage C there was nothing a caller could do about an archived
    resource, so every write answered 404 and "archived" was
    indistinguishable from "never existed". Restoring exists now, which
    makes the distinction worth drawing: a 409 naming the state and the
    remedy is a sentence a client can act on, and `agent.test` has been
    answering in those terms since it was written - "'x' is archived, so it
    is not an agent this project still runs", remedy "Restore the agent,
    then test it".

    A 409 rather than a 404 for the same reason `not_a_test_harness` gives:
    the row is there, the caller may read it - `GET` answers for an archived
    agent, and for an archived workflow since stage A - and it belongs to
    something. An unknown id is still a 404, which is what keeps the pair
    meaningful.

    The id goes in the error's context, which is the log; the body carries
    `LOCATOR_KEYS` and nothing else, so no caller learns anything here it
    did not already have.
    """
    if row.archived_at is None:
        return
    raise ConflictError(
        f"This {kind} is archived and must be restored before it can be {doing}",
        **{f"{kind}_id": str(row.id)},
    )


def already_archived(row: Agent | Workflow, *, kind: str) -> None:
    """Refuse archiving something that is already archived.

    A separate sentence, because the remedy is not "restore it first" - the
    caller asked for the state the row is already in. 409 rather than the
    404 this used to be: "not found" about a row the caller can read is the
    answer stage C exists to stop giving.
    """
    if row.archived_at is None:
        return
    raise ConflictError(
        f"This {kind} is already archived",
        **{f"{kind}_id": str(row.id)},
    )


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


async def _reserve_definition_seq(work: UnitOfWork, scope: TenantScope) -> int:
    """Take this project's cursor lock and reserve this change's sequence number.

    **Call this before writing the definition row, never after.** The position
    is the lock order, not a convenience, and it is the reason this is a
    separate function from `_append_definition_event` rather than one call at
    the end.

    `next_seq` opens with `SELECT … FOR UPDATE` on the project's cursor row
    (`EventRepository._ensure_cursor`), held until the transaction ends. So
    whichever statement comes first decides the order in which a definition
    write takes the project cursor and the entity's own row, and:

    - **Cursor first** is the order `services/ingest.py` already uses - it
      locks the cursor, then folds events into `agents`, `tasks`, `runs` and
      the rest. A definition write that does the same adds no new edge to the
      lock graph.
    - **Cursor last** - reserving the number just before the append, after the
      row has been written - would be `agents → cursor`, the reverse of
      ingest's `cursor → agents`, and an ordinary heartbeat batch landing at
      the same moment would deadlock against an agent edit. PostgreSQL would
      abort one of them with `40P01`, which is absent from
      `api.errors.CONFLICT_SQLSTATES`, raises `OperationalError` for which no
      handler is registered, and so reaches the caller as a 500. Nothing
      retries it.

    `test_definition_events.py` pins the order by capturing statements, and the
    mutation that must fail it is moving this call below the write.

    A rollback does not consume the number: `next_seq` advances the cursor with
    an ordinary `UPDATE` of a table row rather than a sequence object, so the
    allocation is undone with everything else. That is what keeps the per-
    project sequence gapless, which is the whole premise of P9's shared cursor.
    """
    return await work.events(scope).next_seq()


async def _append_definition_event(
    work: UnitOfWork,
    scope: TenantScope,
    *,
    seq: int,
    entity: str,
    entity_id: UUID,
    operation: str,
) -> None:
    """Record one definition change, in the caller's transaction. P9, P11, P12.

    The event is the durable publication record: Option A of the P9 audit, so
    there is no outbox and no second table. Publication happens after this
    transaction commits, reading committed rows from the shared `events.seq` -
    which is why the number comes from `_reserve_definition_seq` above and not
    from here.

    Three conventions, none of them chosen here:

    - **`definition.{entity}.{operation}`** with `producer=CONTROL_PLANE` and
      `folds_into=()` (P11). `fold()` dispatches on the namespace and
      `definition` is not a reducer key, so replay cannot re-create a
      definition. The names are deliberately *not* `agent.updated` - that is
      the client *frame*, and those namespaces have reducers.
    - **Subject `project`, identity in the payload** (P11.3). `SubjectKind` has
      no member for a workflow definition or a connection, and
      `SubjectKind.AGENT`'s `subject_ref` is the `agent_key` rather than a
      uuid. The payload carries the entity type and id and nothing else:
      `instructions`, `labels`, `model_overrides` and `dispatch` are free-form
      and may hold an endpoint or a token, so the whole entity is assembled at
      publish time from the authorized read model (P10.3).
    - **`dedup_key` carries `seq`** (P12). Not decoration. Uniqueness is
      `(project_id, dedup_key)` on each monthly partition, so a key like
      `def:agent:{id}:updated` would insert once and then raise `23505` for
      every later edit that month - a 409 telling the caller to try again,
      which would never come true. And because the index covers `dedup_key`
      alone rather than `(type, dedup_key)`, and `append` never consults
      `seen()`, a predictable key is pre-claimable: an agent ingest key could
      post a heartbeat carrying this exact string and make the agent
      permanently uneditable. `seq` is allocated under the cursor lock
      immediately above, so it is unique per project by construction and
      cannot be guessed in advance.

    `refs` is empty rather than resolved. `EventRepository.resolve` matches only
    the `agent`, `task`, `run` and `workflow_execution` subject kinds, so for a
    `project` subject it returns an all-`None` result after four lookups that
    cannot match; passing it directly is equivalent and cheaper. It also keeps
    these rows off `events_agent_idx`, which is the direction P10.3 wants.
    """
    envelope = parse_envelope(
        {
            "dedup_key": f"def:{entity}:{entity_id}:{operation}:{seq}",
            "type": f"definition.{entity}.{operation}",
            "protocol_version": PROTOCOL_VERSION,
            "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
            "subject": {"kind": "project", "ref": str(scope.require_project())},
            "payload": {"entity": entity, "id": str(entity_id)},
        }
    )
    await work.events(scope).append(envelope, seq=seq, refs=ResolvedRefs())


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
