"""Running one agent once, against a sample input.

`03-API-AND-FRONTEND.md` §3 promises a test that "is a real execution
producing real events, not a preview", and this service is the whole of how
that is arranged. It arranges nothing new: it compiles the one-step graph,
finds or creates the agent's harness, and then hands the harness to
`ExecutionService.publish` and `ExecutionService.start` - the same two
methods the publish and run routes call.

**Four things happen and four things are recorded.** A first test creates a
workflow, publishes it and runs it; a later test only runs it. Each of those
is a privileged action in its own right - `workflow.publish` and
`workflow.run` are both in `SENSITIVE_ACTIONS` - so each writes its own audit
row under its own name, from the service that performs it, and `agent.test`
is one more row naming what the person actually asked for. Collapsing them
into a single `agent.test` row would hide two sensitive actions from the
audit digest; collapsing `agent.test` into them would lose the request. They
are tied together by `request_id`, which `audit.record` already carries.

**The lifecycle is resumable rather than transactional.** Creating the
harness commits before publishing it, because publishing is
`ExecutionService.publish`'s transaction and not this one. A publish that
fails - an agent that is `observed` by the time it runs, a graph a later
release refuses - therefore leaves an unpublished harness behind. That is
the recoverable state, not a leak: the next test finds the same row, sees it
still has no current version, and publishes that. What it must never do is
create a second harness, and the unique index on `test_for_agent_id` is what
makes that impossible rather than careful.

**What this service does not do.** It does not touch `advance`, the dispatch
tables, the worker or the runner; a test dispatch is created by the
orchestrator from a published version like every other. It does not decide
whether the agent has a usable credential - a managed agent with no
credential compiles, publishes, dispatches and fails as `misconfigured`,
which is C19 and must stay true until Phase 7 needs it. And it does not
return a result: the execution is asynchronous, and what the agent said
arrives on the execution's own `output`.
"""

from __future__ import annotations

from uuid import UUID

from agentworld.domain.entities import Agent, Workflow
from agentworld.domain.errors import AgentNotTestableError, ConflictError, NotFoundError
from agentworld.domain.orchestration import (
    AgentTestSubject,
    CompiledTestWorkflow,
    compile_agent_test,
)
from agentworld.domain.orchestration.agent_test import INPUT_FIELD
from agentworld.domain.policy import Action, Resource, authorize
from agentworld.domain.principals import UserPrincipal
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWork, UnitOfWorkFactory
from agentworld.services import audit
from agentworld.services.context import CallContext
from agentworld.services.executions import ExecutionService


class AgentTestService:
    """`agent.test`: compile, publish once, run, and record all of it."""

    def __init__(self, factory: UnitOfWorkFactory, *, executions: ExecutionService) -> None:
        self._factory = factory
        self._executions = executions

    async def run(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        agent_id: UUID,
        *,
        sample: str,
    ) -> UUID:
        """Start one test execution of this agent and return its id.

        202, not a result: the turn is performed by a worker, and the answer
        arrives on `GET /v1/projects/{id}/executions/{execution_id}` as the
        execution's `output`. There is no second read path for it.
        """
        harness, publish_first = await self._harness(principal, context, project_id, agent_id)

        if publish_first:
            # The ordinary publish, with its own authorization and its own
            # `workflow.publish` audit row. Its findings surface unchanged:
            # a graph this release cannot freeze must say so in the same
            # words it would for a workflow somebody drew.
            await self._executions.publish(principal, context, project_id, harness.id)

        return await self._executions.start(
            principal,
            context,
            project_id,
            harness.id,
            # `INPUT_FIELD` is the compiler's own constant, imported rather
            # than repeated: the step reads `$.input.input`, and the field
            # the execution is started with has to be the field that
            # expression names. Two spellings of it would agree until
            # somebody changed one, and the only thing that would notice is
            # an end-to-end test watching the provider receive the prompt.
            inputs={INPUT_FIELD: sample},
        )

    async def _harness(
        self,
        principal: UserPrincipal,
        context: CallContext,
        project_id: UUID,
        agent_id: UUID,
    ) -> tuple[Workflow, bool]:
        """The agent's harness, and whether it still needs publishing.

        One transaction: the agent that was read is the agent that was
        compiled, so an agent archived a moment later cannot leave a harness
        naming it.
        """
        async with self._factory.begin(visible_workspaces=set(principal.memberships)) as work:
            workspace_id = await _workspace_of(work, project_id)
            resource = Resource(workspace_id)
            # First, and alone. Testing an agent is what the caller asked
            # for; whether this agent exists is none of their business until
            # they are allowed to ask.
            authorize(principal, Action.AGENT_TEST, resource)

            scope = TenantScope(workspace_id, project_id)
            # `include_archived=True`, matching `AgentService.get`: an
            # archived agent is still readable by id, so the refusal can say
            # that it is archived instead of pretending it is not there.
            agent = await work.agents(scope).get(agent_id, include_archived=True)
            if agent is None:
                raise NotFoundError("Agent not found", agent_id=str(agent_id))

            compiled = _compiled_for(agent)
            workflows = work.workflows(scope)
            harness = await workflows.test_for(agent_id)

            created = False
            if harness is None:
                # Creating a workflow is a permission of its own, and this
                # one is only taken on the first test. `workflow.publish`
                # and `workflow.run` are checked by the services that
                # perform them; both are `member`, as this is, so there is
                # no role that gets this far and is refused later.
                authorize(principal, Action.WORKFLOW_CREATE, resource)
                harness, created = await workflows.find_or_create_test(
                    agent_id=agent_id,
                    slug=compiled.slug,
                    name=compiled.name,
                    description=compiled.description,
                    graph=compiled.graph,
                    created_by_user_id=principal.user_id,
                )
                if harness is None:
                    # The insert conflicted with something that is not this
                    # agent's harness, which leaves exactly one possibility.
                    raise ConflictError(
                        f"This project already has a workflow with the slug '{compiled.slug}', "
                        "so the test harness for this agent cannot be created",
                        slug=compiled.slug,
                        agent_id=str(agent_id),
                    )

            if created:
                await audit.record(
                    work,
                    principal,
                    context,
                    workspace_id=workspace_id,
                    action="workflow.create",
                    resource_type="workflow",
                    resource_id=harness.id,
                    metadata={
                        "slug": harness.slug,
                        # So a reader of this row can tell it from a workflow
                        # a person created without joining back to the table.
                        "test_for_agent_id": str(agent_id),
                    },
                )

            await audit.record(
                work,
                principal,
                context,
                workspace_id=workspace_id,
                action="agent.test",
                resource_type="agent",
                resource_id=agent_id,
                metadata={
                    "workflow_id": str(harness.id),
                    # The step key, because it is what the published version
                    # freezes and what the execution's output is keyed by.
                    "step_key": compiled.step_key,
                    "created_harness": created,
                },
            )
            # Asked of `workflow_versions`, not of `workflows`. The obvious
            # predicate is `current_version_id is None`, and it is wrong:
            # **nothing in this codebase ever writes that column.** It is
            # read by the entity, the API view and nothing else, so it is
            # NULL for every workflow that has ever been published, and a
            # test would republish on every run - version 3, 4, 5 - which is
            # how this was found. `published_version` is what
            # `ExecutionService.start` consults for the version it executes,
            # so it is the authority this decision has to share.
            published = await workflows.published_version(harness.id)
            return harness, published is None


def _compiled_for(agent: Agent) -> CompiledTestWorkflow:
    """The compiled harness for this agent, or a 422 carrying the reasons.

    The judgement is the pure compiler's. This function only turns its
    findings into the error the API already knows how to render, in the same
    shape a publish refusal uses.
    """
    compilation = compile_agent_test(
        AgentTestSubject(
            agent_id=agent.id,
            agent_key=agent.agent_key,
            interaction_mode=agent.interaction_mode,
            display_name=agent.display_name,
            is_archived=agent.is_archived,
        )
    )
    if compilation.workflow is None:
        raise AgentNotTestableError(
            "This agent cannot be tested",
            findings=[
                {
                    "pointer": finding.pointer,
                    "code": finding.code.value,
                    "detail": finding.detail,
                }
                for finding in compilation.findings
            ],
            agent_id=str(agent.id),
        )
    return compilation.workflow


async def _workspace_of(work: UnitOfWork, project_id: UUID) -> UUID:
    workspace_id = await work.project_lookup.workspace_of(project_id)
    if workspace_id is None:
        raise NotFoundError("Project not found", project_id=str(project_id))
    return workspace_id
